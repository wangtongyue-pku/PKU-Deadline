"""Stage 7C: classification, importance, daily digest, feedback, AI gate.

Local rules come first and every judgement carries its reasons (requirement 5):
a low-importance verdict is an explanation, never a licence to hide or delete
mail — the digest keeps every message visible somewhere.

The AI path is a **gate, not a sender**: `build_ai_payload` reduces a message to
subject + metadata + already-extracted times, runs the existing privacy scanner
over it, and refuses to include anything unless the caller passes
``confirmed=True``. Actual dispatch to DeepSeek/MiMo is wired in 7D through the
existing parse_service; nothing in this module performs network calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pku_deadline_agent.email_models import (
    ADVERTISEMENT,
    COMPETITION,
    COURSE_ADMIN,
    EmailAnalysis,
    EmailDeadline,
    EVENT_REGISTRATION,
    GENERAL_NEWS,
    IMPORTANCE_HIGH,
    IMPORTANCE_LEVELS,
    IMPORTANCE_LOW,
    IMPORTANCE_MEDIUM,
    LECTURE,
    NEEDS_REVIEW,
    RawEmailRecord,
    RESEARCH_OPPORTUNITY,
    SCHOOL_NOTICE,
    UNCLEAR_TIME,
)
from pku_deadline_agent.email_parse import extract_email_deadlines
from pku_deadline_agent.privacy import scan_privacy
from pku_deadline_agent.time_utils import beijing_now

_PKU_SENDER = "pku.edu.cn"

# ------------------------------------------------------------- classification

_AD_SIGNALS = ("退订", "unsubscribe", "优惠", "促销", "中奖", "点击链接领取", "限时抢购", "广告")
_COURSE_WORDS = ("教务", "课程", "选课", "考试", "成绩", "作业", "教学", "补考", "缓考", "开学", "注册费")
_LECTURE_WORDS = ("讲座", "学术报告", "论坛", "研讨会", "沙龙", "报告会")
_RESEARCH_WORDS = ("科研", "实验室", "课题组", "立项", "招募", "保研", "夏令营", "实习", "博士生", "研究助理")
_COMPETITION_WORDS = ("竞赛", "大赛", "比赛", "挑战杯", "ACM", "ICPC")
_EVENT_WORDS = ("报名", "活动", "名额", "预约", "申请")
_NOTICE_WORDS = ("通知", "重要", "紧急", "放假", "安排", "校历", "公告")


def classify_email(record: RawEmailRecord) -> tuple[str, str]:
    """Rules are additive and first match wins — the order is the contract.

    Anything the rules cannot name lands in 待确认邮件 (when the sender is
    outside 北大域名) or 普通资讯 (campus sender), so nothing is silently
    dropped and the user can correct it via the feedback store.
    """
    haystack = f"{record.sender}\n{record.subject}\n{record.body_text[:2000]}"
    from_campus = record.sender.endswith(_PKU_SENDER)

    if any(word in haystack for word in _AD_SIGNALS) and not from_campus:
        return ADVERTISEMENT, "含推广/退订特征且发件人不是北大域名。"
    if from_campus and any(w in haystack for w in _COURSE_WORDS):
        return COURSE_ADMIN, "北大域名发件人且含教务/课程关键词。"
    if any(w in haystack for w in _COMPETITION_WORDS):
        return COMPETITION, "含竞赛/大赛关键词。"
    if any(w in haystack for w in _RESEARCH_WORDS):
        return RESEARCH_OPPORTUNITY, "含科研/招募/保研关键词。"
    if any(w in haystack for w in _LECTURE_WORDS):
        return LECTURE, "含讲座/论坛/报告关键词。"
    if any(w in haystack for w in _EVENT_WORDS):
        return EVENT_REGISTRATION, "含报名/活动关键词。"
    if from_campus and any(w in haystack for w in _NOTICE_WORDS):
        return SCHOOL_NOTICE, "北大域名发件人的通知类邮件。"
    if from_campus:
        return GENERAL_NEWS, "北大域名发件人，未命中更具体规则。"
    return NEEDS_REVIEW, "校外发件人且未命中任何规则，需人工确认。"


# ----------------------------------------------------------------- importance

_ACTION_WORDS = ("请", "务必", "尽快", "及时", "请于", "需在", "按要求", "逾期", "过期不候")
_SOON_WINDOW = timedelta(days=3)


def _nearest_deadline(analysis: EmailAnalysis, now: datetime) -> EmailDeadline | None:
    timed = [d for d in analysis.deadlines if d.due_at is not None]
    return min(timed, key=lambda d: d.due_at) if timed else None


def assess_importance(analysis: EmailAnalysis, now: datetime) -> tuple[str, tuple[str, ...]]:
    """Explainable importance. Reasons cite the evidence, in reading order."""
    reasons: list[str] = []
    level = IMPORTANCE_LOW

    nearest = _nearest_deadline(analysis, now)
    if nearest is not None and nearest.due_at is not None:
        delta = nearest.due_at - now
        if delta <= _SOON_WINDOW:
            reasons.append(f"存在 {nearest.due_at.strftime('%m-%d %H:%M')} 的{nearest.kind}，已进入 3 天窗口。")
            level = IMPORTANCE_HIGH
        else:
            reasons.append(f"存在 {nearest.due_at.strftime('%m-%d')} 的{nearest.kind}。")
            level = max(level, IMPORTANCE_MEDIUM, key=IMPORTANCE_LEVELS.index)
    elif any(d.kind == UNCLEAR_TIME for d in analysis.deadlines):
        reasons.append("提到时间但语义不明确，需人工确认。")
        level = max(level, IMPORTANCE_MEDIUM, key=IMPORTANCE_LEVELS.index)

    if analysis.category in (COURSE_ADMIN, SCHOOL_NOTICE):
        reasons.append(f"分类为{analysis.category}（{analysis.category_reason}）。")
        level = max(level, IMPORTANCE_MEDIUM, key=IMPORTANCE_LEVELS.index)
    if analysis.category in (LECTURE, COMPETITION, RESEARCH_OPPORTUNITY, EVENT_REGISTRATION):
        reasons.append(f"分类为{analysis.category}，可能值得关注。")
        level = max(level, IMPORTANCE_MEDIUM, key=IMPORTANCE_LEVELS.index)

    if analysis.record.sender.endswith(_PKU_SENDER):
        reasons.append("发件人为北大域名。")

    if analysis.category == ADVERTISEMENT:
        reasons.append("广告/推广邮件。")
        level = IMPORTANCE_LOW
    return level, tuple(reasons)


def detect_actions(analysis: EmailAnalysis) -> tuple[bool, tuple[str, ...]]:
    """Does this mail ask the reader to *do* something, and why we think so."""
    reasons: list[str] = []
    haystack = f"{analysis.record.subject}\n{analysis.record.body_text[:2000]}"

    if analysis.deadlines:
        kinds = "、".join(dict.fromkeys(d.kind for d in analysis.deadlines))
        reasons.append(f"包含{kinds}。")
    hits = [word for word in _ACTION_WORDS if word in haystack]
    if hits:
        reasons.append(f"含行动请求用语：{'、'.join(hits[:4])}。")
    if analysis.category == EVENT_REGISTRATION:
        reasons.append("活动/报名类邮件通常需要回复或填写表单。")
    return bool(reasons), tuple(reasons)


def analyze_email(record: RawEmailRecord, *, now: datetime | None = None) -> EmailAnalysis:
    """One-call local rule pass. ``now`` defaults to real Beijing time; tests
    pin it for determinism."""
    now = now or beijing_now()
    category, reason = classify_email(record)
    analysis = EmailAnalysis(
        record=record,
        category=category,
        category_reason=reason,
        deadlines=extract_email_deadlines(record),
    )
    importance, importance_reasons = assess_importance(analysis, now)
    action_needed, action_reasons = detect_actions(analysis)
    return EmailAnalysis(
        record=record,
        category=category,
        category_reason=reason,
        deadlines=analysis.deadlines,
        importance=importance,
        importance_reasons=importance_reasons,
        action_needed=action_needed,
        action_reasons=action_reasons,
    )


# ------------------------------------------------------------------ feedback


@dataclass(frozen=True)
class ClassificationFeedback:
    """The user's correction of a rule verdict, kept for accuracy work later."""

    stable_key: str
    subject: str
    predicted_category: str
    corrected_category: str
    note: str
    created_at: datetime = field(default_factory=beijing_now)


class EmailFeedbackStore:
    """In-memory correction log; persistence arrives with the 7D page."""

    def __init__(self) -> None:
        self._entries: list[ClassificationFeedback] = []

    def record(self, feedback: ClassificationFeedback) -> None:
        if feedback.predicted_category == feedback.corrected_category:
            raise ValueError("纠正后的分类与原分类相同，无需记录。")
        self._entries.append(feedback)

    def all(self) -> tuple[ClassificationFeedback, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


# --------------------------------------------------------------- daily digest


@dataclass(frozen=True)
class DigestEntry:
    """One mail inside a digest section — always traceable to its source."""

    stable_key: str
    subject: str
    sender: str
    sent_at: datetime | None
    category: str
    importance: str
    reasons: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class DigestDeadline:
    kind: str
    due_at: datetime | None
    needs_confirmation: bool
    stable_key: str
    subject: str


@dataclass(frozen=True)
class DailyDigest:
    day: date
    generated_at: datetime
    received_today: int
    older_than_today: int
    undated: int
    priority_reads: tuple[DigestEntry, ...]
    deadlines: tuple[DigestDeadline, ...]
    events: tuple[DigestEntry, ...]
    incomplete: tuple[DigestEntry, ...]
    rest: tuple[DigestEntry, ...]

    @property
    def total(self) -> int:
        return self.received_today + self.older_than_today + self.undated

    def summary_line(self) -> str:
        parts = [f"今日收到 {self.received_today} 封"]
        if self.older_than_today:
            parts.append(f"另有更早邮件 {self.older_than_today} 封")
        if self.undated:
            parts.append(f"无日期邮件 {self.undated} 封")
        if self.deadlines:
            parts.append(f"时间点 {len(self.deadlines)} 个")
        if self.incomplete:
            parts.append(f"待确认 {len(self.incomplete)} 封")
        return "；".join(parts) + "。"


def _entry(analysis: EmailAnalysis, note: str = "") -> DigestEntry:
    reasons = (*analysis.importance_reasons, *analysis.action_reasons)
    return DigestEntry(
        stable_key=analysis.record.stable_key,
        subject=analysis.record.subject or "(无主题)",
        sender=analysis.record.sender,
        sent_at=analysis.record.sent_at,
        category=analysis.category,
        importance=analysis.importance,
        reasons=reasons,
        note=note,
    )


def build_daily_digest(
    analyses,
    *,
    day: date | None = None,
    now: datetime | None = None,
) -> DailyDigest:
    """The 「今日校园情报」 view. Every mail lands in exactly one section and
    low-importance mail is *grouped later*, never dropped (requirement 5)."""
    now = now or beijing_now()
    day = day or now.date()
    now = normalize_to_beijing(now)

    received_today = older = undated = 0
    priority: list[DigestEntry] = []
    events: list[DigestEntry] = []
    incomplete: list[DigestEntry] = []
    rest: list[DigestEntry] = []
    deadlines: list[DigestDeadline] = []
    seen_entries: set[str] = set()

    for analysis in analyses:
        record = analysis.record
        if record.sent_at is None:
            undated += 1
        elif record.sent_at.date() == day:
            received_today += 1
        else:
            older += 1

        note = ""
        if record.body_truncated:
            note = "正文被截断：本邮件**可能存在未提取到的 Deadline**，需要完整阅读。"
        elif analysis.category == NEEDS_REVIEW:
            note = "分类待确认。"
        elif any(d.needs_confirmation for d in analysis.deadlines):
            note = "含语义不明的时间点。"

        if record.body_truncated or analysis.category == NEEDS_REVIEW or any(
            d.needs_confirmation for d in analysis.deadlines
        ):
            incomplete.append(_entry(analysis, note))

        for deadline in analysis.deadlines:
            deadlines.append(DigestDeadline(
                kind=deadline.kind,
                due_at=deadline.due_at,
                needs_confirmation=deadline.needs_confirmation,
                stable_key=record.stable_key,
                subject=record.subject or "(无主题)",
            ))

        if analysis.importance == IMPORTANCE_HIGH:
            priority.append(_entry(analysis, note))
        elif analysis.category in (LECTURE, COMPETITION, RESEARCH_OPPORTUNITY, EVENT_REGISTRATION):
            events.append(_entry(analysis, note))
        else:
            rest.append(_entry(analysis, note))
        seen_entries.add(record.stable_key)

    def sort_key(entry: DigestEntry):
        return entry.sent_at or datetime.min.replace(tzinfo=now.tzinfo)

    timed = [d for d in deadlines if d.due_at is not None]
    unconfirmed = [d for d in deadlines if d.due_at is None or d.needs_confirmation]
    deadlines_sorted = (
        *sorted(timed, key=lambda d: d.due_at),
        *sorted(unconfirmed, key=lambda d: d.subject),
    )

    return DailyDigest(
        day=day,
        generated_at=now,
        received_today=received_today,
        older_than_today=older,
        undated=undated,
        priority_reads=tuple(sorted(priority, key=sort_key)),
        deadlines=deadlines_sorted,
        events=tuple(sorted(events, key=sort_key)),
        incomplete=tuple(sorted(incomplete, key=sort_key)),
        rest=tuple(sorted(rest, key=sort_key)),
    )


def normalize_to_beijing(value: datetime) -> datetime:
    from pku_deadline_agent.time_utils import normalize_datetime

    return normalize_datetime(value)


# ------------------------------------------------------------------- AI gate

AI_SNIPPET_CHARS = 160


@dataclass(frozen=True)
class AIPayloadItem:
    """The only shape of an email that may ever leave the machine.

    Deliberately excludes body text and attachments: the model sees the subject,
    the sender's domain, the rule verdict and the already-extracted time
    snippets — the minimum needed to refine a classification, nothing more.
    """

    stable_key: str
    subject: str
    sender_domain: str
    category: str
    deadlines: tuple[str, ...]
    snippet: str          # short cued clause, privacy-scanned
    privacy_kinds: tuple[str, ...]


@dataclass(frozen=True)
class AIPayload:
    items: tuple[AIPayloadItem, ...]
    confirmed: bool


def build_ai_payload(analyses, *, confirmed: bool = False) -> AIPayload:
    """Prepare the minimal, privacy-scanned payload for an optional AI refinement.

    Refuses to run while ``confirmed`` is False — the caller (7D page) must show
    the payload and obtain an explicit per-batch confirmation. Full bodies and
    attachments are structurally excluded: they never enter an AIPayloadItem.
    """
    if not confirmed:
        raise PermissionError(
            "外部 AI 调用需要先展示最小化载荷并获得你的逐批确认；当前未确认。"
        )
    items: list[AIPayloadItem] = []
    for analysis in analyses:
        record = analysis.record
        snippets = " ".join(d.source_snippet for d in analysis.deadlines)[:AI_SNIPPET_CHARS]
        report = scan_privacy(f"{record.subject}\n{snippets}")
        if report.blocked:
            # Something credential-like sits in the text: excluded entirely.
            continue
        domain = record.sender.split("@")[-1] if "@" in record.sender else record.sender
        items.append(AIPayloadItem(
            stable_key=record.stable_key,
            subject=record.subject,
            sender_domain=domain,
            category=analysis.category,
            deadlines=tuple(f"{d.kind}:{d.raw_text}" for d in analysis.deadlines),
            snippet=report.sanitized_text[:AI_SNIPPET_CHARS],
            privacy_kinds=report.kinds,
        ))
    return AIPayload(items=tuple(items), confirmed=True)
