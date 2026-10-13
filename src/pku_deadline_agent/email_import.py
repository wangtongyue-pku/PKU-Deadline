"""Stage 7D: turn analysed mail into reviewed local tasks.

This layer reuses the stage 6 planning core (``sync.plan_records``) instead of
re-implementing identity, duplicate detection, revision checks or the write path.
What it adds is the mail-specific reading of an analysis:

* **Only cued, settled times become tasks.** 报名截止 / 课程作业截止 / 材料提交截止 /
  任务截止 with a real ``due_at`` are imported; 活动举办时间 is shown but never
  auto-imported (the user asked for that explicitly), and 待确认时间 has no date to
  import with, so it stays a review item.
* **An untrustworthy mail is an explicit skip**, reported with its UID, never
  silently dropped. Other mail in the same batch is unaffected: one broken
  message must not block the deadlines around it, and nothing is ever deleted.
* **Identity is the message, not the subject.** The stable key comes from the
  source record's Message-ID (or the UID while UIDVALIDITY holds) plus which
  deadline of that message it is, so re-reading the same mail cannot create a
  second task.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from pku_deadline_agent.email_models import (
    EVENT_TIME,
    HOMEWORK_DEADLINE,
    MATERIAL_SUBMISSION_DEADLINE,
    REGISTRATION_DEADLINE,
    TASK_DEADLINE,
    TIME_MENTION_WORDS,
    UNCLEAR_TIME,
)
from pku_deadline_agent.email_sync import EmailSyncReport
from pku_deadline_agent.sources import FetchResult, SourceRecord
from pku_deadline_agent.sync import (
    DEADLINE_CHANGED,
    NEW,
    POSSIBLE_DUPLICATE,
    Candidate,
    plan_records,
)
from pku_deadline_agent.task_management import ImportDecision, NoticeOrigin
from pku_deadline_agent.time_utils import beijing_now

EMAIL_SOURCE = "email"

# Mail deadline kind -> the task-type enum the database accepts. Kinds that never
# become tasks are absent on purpose (see the module docstring).
EMAIL_TYPE_MAP = {
    REGISTRATION_DEADLINE: "活动报名",
    HOMEWORK_DEADLINE: "作业",
    MATERIAL_SUBMISSION_DEADLINE: "其他",
    TASK_DEADLINE: "其他",
}

# Kinds the user reviews but that are never auto-imported.
EVENT_ONLY_KINDS = (EVENT_TIME,)

# The readable identity inside the task database is a hash of these; the message
# id itself is never stored (it is a real personal identifier).
_KEY_HASH_CHARS = 32
MAX_TITLE_CHARS = 200
MAX_CATEGORY_CHARS = 100

_ANGLE_NAME = re.compile(r"^\s*(?P<name>[^<]*?)\s*<(?P<address>[^>]+)>\s*$")


class EmailImportError(Exception):
    """The batch cannot become an import plan at all."""


class EmailNotTrustworthy(EmailImportError):
    """The batch has rejections or warnings, so no plan may be built from it."""


def map_email_task_type(kind: str, *, unmapped: tuple[str, ...] = ()) -> tuple[str, str | None]:
    """Mail deadline kind -> local task type, remembering an unmapped kind.

    Returns ``(local_type, original_or_None)``: the shape ``sync.Candidate``
    expects from a type mapper, so an unrecognised kind lands in 其他 **and** its
    original wording is kept in the review notes instead of disappearing.
    """
    local = EMAIL_TYPE_MAP.get(kind)
    if local is None:
        return "其他", kind
    return local, None


def sender_label(sender: str, sender_name: str = "") -> str:
    """A short, human source label for the task's course/activity field.

    Prefers the display name the user recognises in their inbox, falls back to
    the domain, and only then to a generic label — never to nothing.
    """
    name = " ".join((sender_name or "").split())
    if name:
        return name[:MAX_CATEGORY_CHARS]
    text = (sender or "").strip()
    if not text:
        return "学校邮箱"
    match = _ANGLE_NAME.match(text)
    if match and match["name"]:
        return match["name"].strip()[:MAX_CATEGORY_CHARS]
    if match:
        text = match["address"]
    text = text.split("@")[-1] if "@" in text else text
    return (text.strip() or "学校邮箱")[:MAX_CATEGORY_CHARS]


def deadline_message_key(stable_key: str, index: int) -> str:
    """Opaque identity for one deadline of one message, accepted by notice_key()."""
    digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:_KEY_HASH_CHARS]
    return f"msg{digest}d{index:03d}"


def task_title(subject: str, kind: str, *, multiple: bool) -> str:
    """Task title for one deadline.

    A subject with several dates would otherwise produce several identical task
    titles, which the duplicate matcher then reports as the same thing. Naming
    the kind keeps them distinguishable while staying close to the original.
    """
    base = (subject or "").strip() or "(无主题邮件)"
    if multiple:
        base = f"{base}（{kind}）"
    return base[:MAX_TITLE_CHARS]


@dataclass(frozen=True)
class EmailTaskRecord:
    """One proposed local task, with the mail context the user needs to judge it."""

    source: SourceRecord
    kind: str
    subject: str
    sender: str
    sent_at: object | None
    stable_key: str
    category: str
    importance: str
    needs_confirmation: bool
    raw_text: str
    snippet: str
    reason: str
    record_uid: int
    action_needed: bool = False


@dataclass(frozen=True)
class UndatedDeadline:
    """A time the mail mentions without a usable date — shown, never imported."""

    kind: str
    raw_text: str
    subject: str
    stable_key: str
    uid: int


@dataclass(frozen=True)
class SkippedEmail:
    """A message that could not be trusted, kept as a record so it can be retried."""

    uid: int
    subject: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class EmailImportPlan:
    """Everything the page needs: the shared sync plan plus mail-only context."""

    sync_plan: object
    records: tuple[EmailTaskRecord, ...] = ()
    undated: tuple[UndatedDeadline, ...] = ()
    event_only: tuple[EmailTaskRecord, ...] = ()
    skipped: tuple[SkippedEmail, ...] = ()
    analysis_count: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def proposals(self):
        return self.sync_plan.proposals

    @property
    def trustworthy(self) -> bool:
        return self.sync_plan.trustworthy

    @property
    def importable_count(self) -> int:
        return len(self.records)

    def context_for(self, source_key: str) -> EmailTaskRecord | None:
        """Mail context for one proposal.

        A proposal's ``source_key`` is the readable "source:id" form, so the
        prefix is dropped before matching: the mail-side records key on the bare
        id, and comparing the two forms directly would silently match nothing.
        """
        bare = source_key.split(":", 1)[-1]
        for item in self.records:
            if item.source.source_id == bare:
                return item
        return None

    def describe(self) -> str:
        from pku_deadline_agent.sync import describe_plan

        parts = [describe_plan(self.sync_plan)]
        if self.event_only:
            parts.append(f"仅展示活动时间 {len(self.event_only)}")
        if self.undated:
            parts.append(f"待确认时间 {len(self.undated)}")
        if self.skipped:
            parts.append(f"无法解析 {len(self.skipped)} 封")
        return "，".join(parts)


def plan_email_import(report: EmailSyncReport, repository, *,
                      allow_truncated: bool = True) -> EmailImportPlan:
    """Build a reviewed plan from one sync report. Never writes, never deletes.

    Two levels of "cannot be trusted" are handled differently on purpose:

    * a **batch** condition (rejections or warnings) stops the plan entirely —
      nothing is offered for writing, and no local task is touched;
    * a **single message** condition (a body cut at the size limit) is recorded
      as a skipped message with its UID, so the other mail in the same batch can
      still be reviewed and the failure can be retried. Pass
      ``allow_truncated=False`` to treat it as a batch problem instead.

    The plan always contains every message that was read, so the user keeps
    seeing what arrived; what to actually write is decided later, per proposal.
    """
    if report.rejections:
        raise EmailNotTrustworthy(
            "本次有无法确认的内容，不能生成可写入的方案；数据库未被修改，"
            "已有任务不会被删除。原因：" + "；".join(report.rejections)
        )

    # A truncated-but-parsed message is a *per-message* problem: it is reported as
    # skipped and the rest of the batch stays reviewable. Its own truncation
    # warning therefore must not abort the whole batch, or one long mail would
    # block every other deadline. Every other warning still does.
    skipped_uids = {a.record.uid for a in report.analyses if a.record.body_truncated}
    if not allow_truncated:
        skipped_uids = set()
    batch_warnings = tuple(
        warning for warning in report.warnings
        if not any(f"UID {uid}:" in warning for uid in skipped_uids)
    )
    if batch_warnings:
        raise EmailNotTrustworthy(
            "本次有无法确认的内容，不能生成可写入的方案；数据库未被修改，"
            "已有任务不会被删除。原因：" + "；".join(batch_warnings)
        )

    records: list[SourceRecord] = []
    contexts: dict[str, EmailTaskRecord] = {}
    undated: list[UndatedDeadline] = []
    event_only: list[EmailTaskRecord] = []
    skipped: list[SkippedEmail] = []

    for analysis in report.analyses:
        message = analysis.record
        if message.body_truncated:
            # A truncated body proves nothing about the dates it may have held, so
            # this mail is recorded as skipped (and retried later) instead of
            # importing whatever happened to survive the size limit.
            skipped.append(SkippedEmail(
                message.uid, message.subject,
                ("正文被截断：可能有未提取到的 Deadline，需完整阅读后再导入。",),
            ))
            continue
        # The kind is only part of the title when one message yields several
        # tasks; otherwise the subject alone reads better.
        dated = [d for d in analysis.deadlines if d.due_at is not None]
        multiple = len(dated) > 1
        for position, deadline in enumerate(analysis.deadlines, start=1):
            if deadline.due_at is None:
                undated.append(UndatedDeadline(deadline.kind, deadline.raw_text,
                                               message.subject, message.stable_key, message.uid))
                continue
            source = SourceRecord(
                source=EMAIL_SOURCE,
                source_id=deadline_message_key(message.stable_key, position),
                task_title=task_title(message.subject, deadline.kind, multiple=multiple),
                course=sender_label(message.sender, message.sender_name),
                task_type=deadline.kind,
                published_at=None,
                deadline=deadline.due_at,
            )
            context = EmailTaskRecord(
                source=source,
                kind=deadline.kind,
                subject=message.subject,
                sender=message.sender,
                sent_at=message.sent_at,
                stable_key=message.stable_key,
                category=analysis.category,
                importance=analysis.importance,
                needs_confirmation=deadline.needs_confirmation,
                raw_text=deadline.raw_text,
                snippet=deadline.source_snippet,
                reason=analysis.category_reason,
                record_uid=message.uid,
                action_needed=analysis.action_needed,
            )
            if deadline.kind in EVENT_ONLY_KINDS:
                event_only.append(context)
                continue
            records.append(source)
            contexts[source.source_id] = context

        if not analysis.deadlines:
            # A cue with no date at all ("作业提交截止，具体日期待定") has no date for
            # the scanner to attach to, so it would otherwise vanish silently.
            # It is reported as 待确认 instead: never imported, never hidden.
            phrasing = " ".join((message.subject, message.body_text))
            hit = next((w for w in TIME_MENTION_WORDS if w in phrasing), None)
            if hit is not None:
                undated.append(UndatedDeadline(
                    UNCLEAR_TIME, hit, message.subject, message.stable_key, message.uid))

    result = FetchResult(
        source=EMAIL_SOURCE,
        records=tuple(records),
        rejections=(),
        warnings=(),
        fetched_at=beijing_now(),
    )
    plan = plan_records(
        result.records, result, repository,
        source=EMAIL_SOURCE,
        new_reason="邮件里读取到的新 Deadline",
        changed_reason="邮件里的截止时间与本地不同，经核对后更新",
        unchanged_reason="邮件截止时间与本地一致",
        duplicate_reason="邮件里的日期与 #{task_id}「{title}」可能重复：{detail}",
        notes=("学校邮箱导入",),
        task_type_mapper=map_email_task_type,
    )
    ordered = tuple(contexts[item.source_key.split(":", 1)[-1]] for item in plan.proposals
                    if item.source_key.split(":", 1)[-1] in contexts)
    return EmailImportPlan(
        sync_plan=plan,
        records=ordered,
        undated=tuple(undated),
        event_only=tuple(event_only),
        skipped=tuple(skipped),
        analysis_count=len(report.analyses),
        warnings=tuple(plan.warnings),
    )


def default_decisions(plan: EmailImportPlan):
    """Proposed handling for every proposal: new mail tasks in, changes reviewed.

    New records are checked by default; a deadline change is *not* preselected,
    because updating an existing task is the decision that needs the user's eyes.
    """
    decisions = []
    for item in plan.proposals:
        if item.kind == NEW:
            decisions.append(ImportDecision("new", allow_distinct=True,
                                            reason="经页面核对后从学校邮箱导入"))
        elif item.kind == POSSIBLE_DUPLICATE:
            decisions.append(ImportDecision("new", allow_distinct=True,
                                            reason="邮件与本地任务可能重复，经核对后作为独立任务新增"))
        elif item.kind == DEADLINE_CHANGED:
            decisions.append(ImportDecision("skip", target_id=item.task_id,
                                            reason="截止时间有变化，本次未选择更新"))
        else:
            decisions.append(ImportDecision("skip", target_id=item.task_id,
                                            reason="已是最新，无需改动"))
    return tuple(decisions)


def apply_email_plan(repository, plan: EmailImportPlan, decisions, *, confirmed: bool = False):
    """Write the reviewed plan through the shared, all-or-nothing import path.

    Nothing here deletes: a task the mail does not mention is simply not in the
    plan, and an untrustworthy batch never reaches this function at all.
    """
    if not confirmed:
        raise ValueError("请确认邮件导入方案后再写入。")
    if not plan.trustworthy:
        raise ValueError("本次结果未通过可信检查，已拒绝写入；数据库未被修改。")
    proposals = plan.proposals
    if len(decisions) != len(proposals):
        raise ValueError("方案与处理决定数量不一致，已中止。")

    candidates, chosen, origins = [], [], []
    for item, decision in zip(proposals, decisions):
        if decision.action == "skip":
            continue
        if decision.action == "update":
            if not decision.reason.strip():
                raise ValueError("截止时间变更必须填写修改原因。")
            if decision.target_id is None:
                raise ValueError("更新需要明确的目标任务编号。")
        candidates.append(_candidate_for(item))
        chosen.append(decision)
        origins.append(_origin_for(plan, item))
    if not candidates:
        return []
    return repository.apply_reviewed_tasks(tuple(candidates), tuple(chosen), tuple(origins), confirmed=True)


def _candidate_for(proposal):
    return Candidate(proposal.record, notes=("学校邮箱导入",),
                     task_type_mapper=map_email_task_type)


def _origin_for(plan: EmailImportPlan, proposal):
    context = plan.context_for(getattr(proposal, "source_key", ""))
    return NoticeOrigin(EMAIL_SOURCE, proposal.stored_key,
                        context.sent_at if context is not None else None)
