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
    COMPETITION_RESEARCH,
    COURSE_ADMIN,
    EmailDeadline,
    EmailAnalysis,
    EVENT_REGISTRATION,
    EVENT_TIME,
    GENERAL_NEWS,
    IMPORTANT_NOTICE,
    LECTURE,
    RawEmailRecord,
    REGISTRATION_DEADLINE,
    SUSPECTED_SPAM,
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
        recipients=recipients,
        subject=str(message.get("Subject") or "").strip(),
        sent_at=sent_at,
        body_text=body,
        body_truncated=truncated,
        has_attachments=has_attachments,
        raw_size=len(raw),
    )
    return record, tuple(warnings)


# -------------------------------------------------------------- classification

_SPAM_SIGNALS = ("退订", "unsubscribe", "优惠", "促销", "中奖", "点击链接领取", "限时抢购")
_PKU_SENDER = "pku.edu.cn"

_COURSE_WORDS = ("教务", "课程", "选课", "考试", "成绩", "作业", "教学", "补考", "缓考")
_LECTURE_WORDS = ("讲座", "学术报告", "论坛", "研讨会", "沙龙", "报告会")
_RESEARCH_WORDS = ("竞赛", "大赛", "比赛", "科研", "实验室", "课题组", "立项", "招募", "保研", "夏令营")
_EVENT_WORDS = ("报名", "活动", "名额", "预约", "申请")


def classify_email(record: RawEmailRecord) -> tuple[str, str]:
    """Rule-based category. Returns ``(category, reason)``; rules are additive
    and the first match wins, so the order is part of the contract."""
    haystack = f"{record.sender}\n{record.subject}\n{record.body_text[:2000]}"

    if any(word in haystack for word in _SPAM_SIGNALS) and not record.sender.endswith(_PKU_SENDER):
        return SUSPECTED_SPAM, "含典型推广/退订特征且发件人不是北大域名。"
    if record.sender.endswith(_PKU_SENDER) and any(w in haystack for w in _COURSE_WORDS):
        return COURSE_ADMIN, "北大域名发件人且含教务/课程关键词。"
    if any(w in haystack for w in _RESEARCH_WORDS):
        return COMPETITION_RESEARCH, "含竞赛/科研/招募关键词。"
    if any(w in haystack for w in _LECTURE_WORDS):
        return LECTURE, "含讲座/论坛/报告关键词。"
    if any(w in haystack for w in _EVENT_WORDS):
        return EVENT_REGISTRATION, "含报名/活动关键词。"
    if record.sender.endswith(_PKU_SENDER) and any(w in haystack for w in ("通知", "重要", "紧急")):
        return IMPORTANT_NOTICE, "北大域名发件人的通知类邮件。"
    return GENERAL_NEWS, "未命中任何规则，归入普通资讯。"


# ------------------------------------------------------------ time extraction

_DATE_PATTERNS = (
    # 2026年11月13日 / 2026年11月13日 15:00
    re.compile(r"(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日(?:\s*(?P<h>\d{1,2}):(?P<mi>\d{2}))?"),
    # 2026-11-13 / 2026/11/13, optional time
    re.compile(r"(?P<y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})(?:\s+(?P<h>\d{1,2}):(?P<mi>\d{2}))?"),
    # 11月13日 (no year) / 11月13日 15:00
    re.compile(r"(?P<m>\d{1,2})月(?P<d>\d{1,2})日(?:\s*(?P<h>\d{1,2}):(?P<mi>\d{2}))?"),
)

_REGISTRATION_WORDS = ("报名", "申请", "注册", "回执", "提交材料", "报名表")
_EVENT_WORDS_NEAR = ("举办", "举行", "召开", "开始", "活动时间", "会议时间", "讲座时间", "地点")
_DEADLINE_WORDS = ("截止", "之前", "前完成", "deadline", "Deadline", "DEADLINE")


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


def _kind_for_clauses(clauses: list[tuple[int, int, str]], match_start: int, match_end: int) -> str:
    """Classify a date by its own clause, extending only if that clause is mute.

    「报名截止2026年11月13日15:00，讲座将于2026年11月20日举行」 therefore yields
    one 报名截止 and one 活动举办时间 instead of two registration dates. When no
    clause in reach names anything, the value is kept but marked 待确认.
    """
    def cues_in(piece: str) -> bool:
        return (
            any(w in piece for w in _REGISTRATION_WORDS)
            or any(w in piece for w in _EVENT_WORDS_NEAR)
            or any(w in piece for w in _DEADLINE_WORDS)
        )

    containing = [i for i, (s, e, _) in enumerate(clauses) if s <= match_start and match_end <= e]
    if not containing:
        return UNCLEAR_TIME
    index = containing[0]

    for candidate in (index, index - 1, index + 1):
        if 0 <= candidate < len(clauses) and cues_in(clauses[candidate][2]):
            piece = clauses[candidate][2]
            has_deadline = any(w in piece for w in _DEADLINE_WORDS)
            if any(w in piece for w in _REGISTRATION_WORDS):
                return REGISTRATION_DEADLINE if has_deadline else UNCLEAR_TIME
            if any(w in piece for w in _EVENT_WORDS_NEAR) and not has_deadline:
                return EVENT_TIME
            if has_deadline:
                return TASK_DEADLINE
            return UNCLEAR_TIME
    return UNCLEAR_TIME


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
    seen: set[tuple[str, datetime | None, str]] = set()

    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            year = groups.get("y")
            snippet_start = max(0, match.start() - 40)
            snippet_source = text[snippet_start:min(len(text), match.end() + SNIPPET_CHARS)].strip()

            if year is None:
                if record.sent_at is None:
                    found.append(EmailDeadline(
                        kind=UNCLEAR_TIME, due_at=None,
                        raw_text=match.group(0).strip(),
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
            key = (kind, due, match.group(0).strip())
            if key in seen:
                continue
            seen.add(key)
            found.append(EmailDeadline(
                kind=kind,
                due_at=due,
                raw_text=match.group(0).strip(),
                source_snippet=snippet_source,
                needs_confirmation=(kind == UNCLEAR_TIME),
            ))
    return tuple(found)


def analyze_email(record: RawEmailRecord) -> EmailAnalysis:
    """One-call rule pass: category plus every explicit time the mail mentions."""
    category, reason = classify_email(record)
    return EmailAnalysis(
        record=record,
        category=category,
        category_reason=reason,
        deadlines=extract_email_deadlines(record),
    )
