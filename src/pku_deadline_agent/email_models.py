"""Stage 7 data model for student-mail integration.

Design rules (AGENTS.md, stage-7 boundaries):

- Read-only. Nothing in this model or the adapter can mark mail read, delete,
  move, or send. The types have no mutating operations and the adapter contract
  exposes no such method.
- Empty stays empty. A missing date is ``None`` plus a 待确认 marker, never a
  guessed value. Missing identifiers produce a warning, not silence.
- Mail content is untrusted input. It is stored as bounded text and never
  interpreted; nothing here executes or follows instructions found in a message.
- Every record carries its source identity (folder, uid validity, uid,
  Message-ID) so tasks generated from it stay traceable and de-duplicable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pku_deadline_agent.time_utils import BEIJING

# ---------------------------------------------------------------- categories
#
# Stage-7C taxonomy (requirement 1). 每一封邮件必有且只有一个分类；规则说不出
# 理由的进「待确认邮件」，由人工核对——分类错误的邮件永远不会消失，只会降级展示。

COURSE_ADMIN = "课程教务"
SCHOOL_NOTICE = "学校通知"
LECTURE = "讲座"
RESEARCH_OPPORTUNITY = "科研机会"
COMPETITION = "竞赛"
EVENT_REGISTRATION = "活动报名"
GENERAL_NEWS = "普通资讯"
ADVERTISEMENT = "广告"
NEEDS_REVIEW = "待确认邮件"

EMAIL_CATEGORIES = (
    COURSE_ADMIN,
    SCHOOL_NOTICE,
    LECTURE,
    RESEARCH_OPPORTUNITY,
    COMPETITION,
    EVENT_REGISTRATION,
    GENERAL_NEWS,
    ADVERTISEMENT,
    NEEDS_REVIEW,
)

# --------------------------------------------------------------- deadline kinds

REGISTRATION_DEADLINE = "报名截止"
MATERIAL_SUBMISSION_DEADLINE = "材料提交截止"
HOMEWORK_DEADLINE = "课程作业截止"
EVENT_TIME = "活动举办时间"
TASK_DEADLINE = "任务截止"
UNCLEAR_TIME = "待确认时间"

DEADLINE_KINDS = (
    REGISTRATION_DEADLINE,
    MATERIAL_SUBMISSION_DEADLINE,
    HOMEWORK_DEADLINE,
    EVENT_TIME,
    TASK_DEADLINE,
    UNCLEAR_TIME,
)

# A deadline cue with no date at all (「作业提交截止，日期待定」) yields 待确认时间 with
# due_at=None. The date scanner cannot produce it — it only walks dates — so the
# wording lives beside the kind, where the import layer can look for it too.
TIME_MENTION_WORDS = ("截止", "deadline", "Deadline", "DEADLINE", "之前", "前完成", "报名")

# ---------------------------------------------------------------- importance

IMPORTANCE_HIGH = "高"
IMPORTANCE_MEDIUM = "中"
IMPORTANCE_LOW = "低"
# Ordered weakest → strongest so ``max(..., key=IMPORTANCE_LEVELS.index)`` raises
# importance instead of lowering it. (Listing 高 first once made a mail whose
# deadline was inside the 3-day window come out as 中: the ordering is load-bearing.)
IMPORTANCE_LEVELS = (IMPORTANCE_LOW, IMPORTANCE_MEDIUM, IMPORTANCE_HIGH)

# --------------------------------------------------------------------- records


@dataclass(frozen=True)
class RawEmailRecord:
    """One message as fetched: header facts plus bounded body text.

    ``uid`` is unique only together with ``(folder, uidvalidity)``; the stable
    cross-session identity is ``message_id`` when the sender provided one.
    """

    uid: int
    folder: str
    uidvalidity: int
    message_id: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    sent_at: datetime | None          # tz-aware, converted to Asia/Shanghai
    body_text: str                    # bounded plain text, untrusted, never executed
    body_truncated: bool
    has_attachments: bool
    raw_size: int
    sender_name: str = ""             # display name only; the address stays in sender

    @property
    def stable_key(self) -> str:
        """Identity for de-duplication across sessions.

        Uses the sender-supplied Message-ID when present; otherwise a synthetic
        key that stays valid only while UIDVALIDITY is unchanged (recorded as a
        warning by the parser, and reported to the user).
        """
        if self.message_id:
            return f"msgid:{self.message_id}"
        return f"uid:{self.uidvalidity}:{self.folder}:{self.uid}"


@dataclass(frozen=True)
class EmailDeadline:
    """One deadline-ish time found in a message.

    ``kind`` separates 活动举办时间 from 报名截止 — conflating the two would put
    the wrong date on the task board. ``due_at=None`` means the text mentioned a
    time without a usable value (e.g. no year, or only a weekday): reported as
    待确认, never guessed.
    """

    kind: str
    due_at: datetime | None
    raw_text: str
    source_snippet: str
    needs_confirmation: bool

    def __post_init__(self) -> None:
        if self.kind not in DEADLINE_KINDS:
            raise ValueError(f"未知的邮件时间类型: {self.kind!r}")
        if self.due_at is not None:
            if self.due_at.tzinfo is None or self.due_at.utcoffset() is None:
                raise ValueError("邮件时间必须带时区；缺时区的值应保持为 None 并标记待确认。")
            object.__setattr__(self, "due_at", self.due_at.astimezone(BEIJING))


@dataclass(frozen=True)
class EmailAnalysis:
    """One message after the local rule pass: category, times, importance, actions.

    All judgements carry reasons (``category_reason`` / ``importance_reasons`` /
    ``action_reasons``) — a low-importance verdict is an explanation, not a
    licence to hide the mail: the digest keeps every message visible.
    """

    record: RawEmailRecord
    category: str
    category_reason: str
    deadlines: tuple[EmailDeadline, ...] = field(default_factory=tuple)
    importance: str = IMPORTANCE_LOW
    importance_reasons: tuple[str, ...] = field(default_factory=tuple)
    action_needed: bool = False
    action_reasons: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.category not in EMAIL_CATEGORIES:
            raise ValueError(f"未知的邮件分类: {self.category!r}")
        if self.importance not in IMPORTANCE_LEVELS:
            raise ValueError(f"未知的重要程度: {self.importance!r}")

    @property
    def incomplete(self) -> bool:
        """The body was truncated: absence of deadlines here proves nothing."""
        return self.record.body_truncated


# ---------------------------------------------------------------- fetch result


@dataclass(frozen=True)
class EmailFetchResult:
    """One incremental fetch batch.

    ``rejections`` counts messages that could not be parsed at all;
    ``rejected_uids`` carries the same failures as UIDs so the sync service can
    remember and retry them (a rejected message is *recorded*, never silently
    dropped). ``warnings`` counts conditions the user should know about
    (missing Message-ID, missing date, truncated body). Following the stage-5
    discipline: an empty batch with warnings is not evidence that no mail
    exists.
    """

    folder: str
    uidvalidity: int
    records: tuple[RawEmailRecord, ...]
    highest_uid: int
    has_more: bool
    rejections: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    rejected_uids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def trustworthy(self) -> bool:
        """True only when every message in the batch is fully accounted for."""
        return not self.rejections and not self.warnings
