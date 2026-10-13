"""Stage 7 parsing, classification and rule-based time extraction.

Everything here treats message content as **untrusted data**: it is decoded,
bounded and stored, never interpreted or followed. Parsing uses the standard
library's ``email`` package; no third-party component and no network access.

Extraction discipline (mirrors the stage-3 rules):

- Only explicit calendar dates are extracted in 7A. Relative phrases such as
  明天 / 下周三 are left for the later AI-assisted pass, never guessed here.
- A date without a year defaults to the year the message was sent — the same
  documented default the notice pipeline uses, so it is not presented as if the
  sender had written it.
- 活动举办时间 and 报名截止 are different kinds and must never be merged; when
  the context does not say which one a date is, it lands in 待确认时间.
"""

from __future__ import annotations

import re
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from pku_deadline_agent.email_models import (
    EmailDeadline,
    EVENT_TIME,
    RawEmailRecord,
    HOMEWORK_DEADLINE,
    MATERIAL_SUBMISSION_DEADLINE,
    REGISTRATION_DEADLINE,
    TASK_DEADLINE,
    UNCLEAR_TIME,
)
from pku_deadline_agent.time_utils import normalize_datetime

# --------------------------------------------------------------------- bounds

MAX_RAW_BYTES = 2 * 1024 * 1024      # refuse to parse absurdly large raw mail
MAX_BODY_CHARS = 20_000              # bound what we keep from any single body
SNIPPET_CHARS = 80                   # context kept around an extracted time


class _TextFromHTML(HTMLParser):
    """Reduce an HTML part to its text; markup is dropped, never rendered."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self._chunks.append(data.strip())


def _html_to_text(html: str) -> str:
    parser = _TextFromHTML()
    try:
        parser.feed(html)
    except Exception:  # malformed markup: keep what was parsed, as data
        pass
    return "\n".join(parser._chunks)


def _part_text(part: EmailMessage) -> str:
    """Decode one message part to bounded text, tolerating bad charsets."""
    try:
        text = part.get_content()
        if isinstance(text, bytes):  # unusual content types decode to bytes
            text = text.decode("utf-8", "replace")
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        for candidate in (charset, "utf-8", "gbk"):
            try:
                text = payload.decode(candidate, "replace")
                break
            except (LookupError, UnicodeDecodeError):
                continue
        else:
            text = payload.decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        text = _html_to_text(text)
    return " ".join(text.split())


def parse_raw_email(
    raw: bytes,
    *,
    uid: int,
    folder: str,
    uidvalidity: int,
) -> tuple[RawEmailRecord, tuple[str, ...]]:
    """Parse one raw RFC-822 message.

    Returns ``(record, warnings)``. Parsing never raises for content problems —
    bad input degrades to a record plus warnings, because one broken message
    must not abort the batch (and must never look like "no mail").
    """
    warnings: list[str] = []
    if len(raw) > MAX_RAW_BYTES:
        raise ValueError(f"邮件原始数据超过上限（{len(raw)} 字节），已拒绝解析。")

    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as error:  # malformed MIME
        raise ValueError(f"邮件无法解析（{error.__class__.__name__}）。") from None

    message_id = (message.get("Message-ID") or "").strip()
    if not message_id:
        warnings.append(f"UID {uid}: 缺少 Message-ID，跨会话去重将依赖 UID。")

    sender = parseaddr(str(message.get("From") or ""))[1]
    # The display name is kept separately: it is what the user recognises in the
    # task list ("教务处" rather than "pku.edu.cn"), while the address itself is
    # what identity and privacy rules work on.
    sender_name = " ".join(parseaddr(str(message.get("From") or ""))[0].split())[:100]
    recipients = tuple(
        parseaddr(str(value or ""))[1]
        for value in (message.get("To"), message.get("Cc"))
        if value
    )

    sent_at = None
    raw_date = message.get("Date")
    if raw_date:
        try:
            parsed = parsedate_to_datetime(str(raw_date))
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            warnings.append(f"UID {uid}: Date 头无法解析。")
        elif parsed.tzinfo is None or parsed.utcoffset() is None:
            # "-0000" means the sender omitted their zone; we do not guess it.
            warnings.append(f"UID {uid}: Date 头没有时区，未采用。")
        else:
            sent_at = normalize_datetime(parsed)
    else:
        warnings.append(f"UID {uid}: 缺少 Date 头。")

    body_chunks: list[str] = []
    has_attachments = False
    try:
        parts = list(message.walk())
    except Exception:
        parts = []
    for part in parts:
        if part.is_multipart():
            continue
        if part.get_filename() or part.get_content_disposition() == "attachment":
            has_attachments = True
            continue
        if part.get_content_type() in ("text/plain", "text/html"):
            body_chunks.append(_part_text(part))
    body = "\n".join(chunk for chunk in body_chunks if chunk)
    truncated = len(body) > MAX_BODY_CHARS
    if truncated:
        body = body[:MAX_BODY_CHARS]
        warnings.append(f"UID {uid}: 正文超过 {MAX_BODY_CHARS} 字符，已截断保存。")

    record = RawEmailRecord(
        uid=uid,
        folder=folder,
        uidvalidity=uidvalidity,
        message_id=message_id,
        sender=sender,
        sender_name=sender_name,
        recipients=recipients,
        subject=str(message.get("Subject") or "").strip(),
        sent_at=sent_at,
        body_text=body,
        body_truncated=truncated,
        has_attachments=has_attachments,
        raw_size=len(raw),
    )
    return record, tuple(warnings)


# ----------------------------------------------- deadline-kind classification
#
# Category classification lives in email_intel.py (stage 7C); this module keeps
# only the *time* semantics: what a date inside a clause means.

# 「申请」 alone is deliberately *not* a registration cue: 「提交申请材料」 is a
# material submission, not an event sign-up. Material and homework cues are
# therefore tested before the registration cue, and 「申请」 only counts when a
# real registration cue (报名/报名表/回执) is absent.
_REGISTRATION_WORDS = ("报名", "注册", "选课", "回执", "报名表")
_APPLICATION_WORDS = ("申请",)
# 「发布于 / 举行于」name when something happened or will happen; they are not
# deadline cues, so 「本通知发布于2026年11月1日」 stays an ordinary mentioned date.
_PUBLICATION_WORDS = ("发布于", "发布", "公布于", "公布")
_HOMEWORK_WORDS = ("作业", "习题", "练习", "编程作业", "实验报告", "problem set")
_MATERIAL_WORDS = ("材料", "简历", "文档", "论文", "摘要", "申报书", "作品", "海报")
_EVENT_WORDS_NEAR = ("举办", "举行", "召开", "活动时间", "会议时间", "讲座时间", "地点")
_EVENT_WORDS_WEAK = ("开始",)
_DEADLINE_WORDS = ("截止", "之前", "前完成", "deadline", "Deadline", "DEADLINE")
_SUBMIT_WORDS = ("提交", "递交", "发送至", "发至")


# ------------------------------------------------------------ time extraction

_DATE_PATTERNS = (
    # 2026年11月13日 / 2026年11月13日 15:00
    re.compile(r"(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日(?:\s*(?P<h>\d{1,2}):(?P<mi>\d{2}))?"),
    # 2026-11-13 / 2026/11/13, optional time
    re.compile(r"(?P<y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})(?:\s+(?P<h>\d{1,2}):(?P<mi>\d{2}))?"),
    # 11月13日 (no year) / 11月13日 15:00
    re.compile(r"(?P<m>\d{1,2})月(?P<d>\d{1,2})日(?:\s*(?P<h>\d{1,2}):(?P<mi>\d{2}))?"),
)


_CLAUSE_BREAKS = "。；！？!?\n，,"


def _clauses(text: str) -> list[tuple[int, int, str]]:
    """Split into clauses (sentence ends AND commas) with their offsets.

    Chinese commas often separate two independent statements, each with its own
    date and its own meaning — 「报名截止…，讲座将于…举行」 — so a date must be
    judged at clause level first, with sentence level only as fallback.
    """
    clauses: list[tuple[int, int, str]] = []
    start = 0
    for index, char in enumerate(text):
        if char in _CLAUSE_BREAKS:
            piece = text[start:index].strip()
            if piece:
                clauses.append((start, index, piece))
            start = index + 1
    tail = text[start:].strip()
    if tail:
        clauses.append((start, len(text), tail))
    return clauses


def _kind_for_clauses(clauses: list[tuple[int, int, str]], match_start: int, match_end: int) -> str | None:
    """Classify a date by its own clause, extending only if that clause is mute.

    「报名截止2026年11月13日15:00，讲座将于2026年11月20日举行」 therefore yields
    one 报名截止 and one 活动举办时间 instead of two registration dates. A date
    whose clause carries no cue at all is **an ordinary mentioned date** and gets
    ``None`` — the strict distinction requirement 3 asks for: only cued times
    become deadlines.

    Neighbouring clauses are consulted only when the date's own clause is mute,
    and only for the *adjacent* clause inside the same sentence. A cue from any
    other sentence is not evidence for this date: 「本通知发布于2026年11月1日。报名
    截止11月30日」 must not turn the publication date into a deadline. When cues
    half-match (a cue exists, but not enough to name the kind), the value is
    kept but marked 待确认.
    """
    def publication_only(piece: str) -> bool:
        """A published/announced date is a fact about the mail, not a deadline."""
        return (
            any(w in piece for w in _PUBLICATION_WORDS)
            and not any(w in piece for w in _DEADLINE_WORDS)
            and not any(w in piece for w in _SUBMIT_WORDS)
        )

    def event_kind(piece: str, *, weak: bool) -> str | None:
        """Event cues, the only ones strong enough to borrow from a neighbour.

        「讲座将于11月20日举行」 keeps its meaning when 「讲座」 sits in the
        previous clause. Registration, homework and material cues do not: a
        deadline belongs to the clause that states it, so borrowing one from a
        neighbour invents a deadline the date never had.
        """
        if any(w in piece for w in _DEADLINE_WORDS):
            return None
        if any(w in piece for w in _EVENT_WORDS_NEAR):
            return EVENT_TIME
        if weak and any(w in piece for w in _EVENT_WORDS_WEAK):
            return EVENT_TIME
        return None

    def full_kind(piece: str, *, weak: bool) -> str | None:
        if publication_only(piece):
            return None
        has_deadline = any(w in piece for w in _DEADLINE_WORDS)
        submitted = any(w in piece for w in _SUBMIT_WORDS)
        # 材料 / 作业 先于报名判断：「提交申请材料」属于材料提交，不是活动报名。
        if any(w in piece for w in _MATERIAL_WORDS) and (has_deadline or submitted):
            return MATERIAL_SUBMISSION_DEADLINE
        if any(w in piece for w in _HOMEWORK_WORDS) and (has_deadline or submitted):
            return HOMEWORK_DEADLINE
        if any(w in piece for w in _REGISTRATION_WORDS):
            return REGISTRATION_DEADLINE if has_deadline else UNCLEAR_TIME
        if any(w in piece for w in _APPLICATION_WORDS):
            return REGISTRATION_DEADLINE if has_deadline else UNCLEAR_TIME
        event = event_kind(piece, weak=weak)
        if event is not None:
            return event
        if has_deadline:
            return TASK_DEADLINE
        return None

    containing = [i for i, (s, e, _) in enumerate(clauses) if s <= match_start and match_end <= e]
    if not containing:
        return None
    index = containing[0]

    # The date's own clause decides, strong cues first and weak ones (「开始」) after.
    own = full_kind(clauses[index][2], weak=False)
    if own is not None:
        return own
    weak_own = full_kind(clauses[index][2], weak=True)
    if weak_own is not None:
        return weak_own

    # Otherwise borrow from the adjacent clause — but only within the same
    # sentence, and only an event cue. A cue from another sentence is not
    # evidence for this date: 「本通知发布于2026年11月1日。报名截止11月30日」 must
    # not turn the publication date into a registration deadline.
    def breaks_sentence(piece: str) -> bool:
        return "\n" in piece or any(stop in piece for stop in "。！？!?；;")

    for candidate in (index - 1, index + 1):
        if not (0 <= candidate < len(clauses)):
            continue
        left, right = min(index, candidate), max(index, candidate)
        if breaks_sentence(clauses[left][2]) or breaks_sentence(clauses[right][2]):
            continue
        borrowed = event_kind(clauses[candidate][2], weak=False)
        if borrowed is not None:
            return borrowed
    return None


def extract_email_deadlines(record: RawEmailRecord) -> tuple[EmailDeadline, ...]:
    """Explicit calendar times mentioned in subject or body.

    Only literal dates are taken. A date without a year uses the message's own
    year as the documented default (never shown as if the sender wrote it).
    Ambiguous context produces 待确认时间 with ``due_at=None`` — the text is kept
    so the user (or the later AI pass) can decide.
    """
    text = f"{record.subject}\n{record.body_text}"
    clauses = _clauses(text)
    found: list[EmailDeadline] = []
    # One date matches more than one pattern: 「2026年11月1日」 also contains
    # 「11月1日」. A match that lies entirely inside a date already reported is
    # the same fact, so it is skipped rather than listed twice.
    reported_spans: list[tuple[int, int]] = []
    seen: set[tuple[str, datetime | None, str, int]] = set()

    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            if any(s <= match.start() and match.end() <= e for s, e in reported_spans):
                continue
            groups = match.groupdict()
            year = groups.get("y")
            snippet_start = max(0, match.start() - 40)
            snippet_source = text[snippet_start:min(len(text), match.end() + SNIPPET_CHARS)].strip()
            clause_index = next(
                (i for i, (s, e, _) in enumerate(clauses)
                 if s <= match.start() and match.end() <= e),
                -1,
            )
            raw_text = match.group(0).strip()

            if year is None:
                if record.sent_at is None:
                    kind = _kind_for_clauses(clauses, match.start(), match.end())
                    if kind is None:
                        continue
                    reported_spans.append((match.start(), match.end()))
                    # No reference year: the month/day is kept for the user, but the
                    # kind cannot be settled any more than the date can, so it stays
                    # 待确认 rather than claiming an event date the mail never dated.
                    key = (UNCLEAR_TIME, None, raw_text, clause_index)
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(EmailDeadline(
                        kind=UNCLEAR_TIME, due_at=None,
                        raw_text=raw_text,
                        source_snippet=snippet_source, needs_confirmation=True,
                    ))
                    continue
                year = str(record.sent_at.year)

            try:
                due = datetime(
                    int(year), int(groups["m"]), int(groups["d"]),
                    int(groups.get("h") or 0), int(groups.get("mi") or 0),
                    tzinfo=record.sent_at.tzinfo if record.sent_at else None,
                )
            except ValueError:
                continue  # impossible date (2月30日) — data, not an error
            due = normalize_datetime(due)

            kind = _kind_for_clauses(clauses, match.start(), match.end())
            if kind is None:
                # An ordinary mentioned date: not a deadline, not待确认 — simply
                # not promoted. The text itself stays in the body/snippet.
                continue
            reported_spans.append((match.start(), match.end()))
            key = (kind, due, raw_text, clause_index)
            if key in seen:
                continue
            seen.add(key)
            found.append(EmailDeadline(
                kind=kind,
                due_at=due,
                raw_text=raw_text,
                source_snippet=snippet_source,
                needs_confirmation=(kind == UNCLEAR_TIME),
            ))
    return tuple(found)
