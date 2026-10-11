"""Stage 6A/6B: turn platform records into a reviewed change preview.

Separated on purpose:

    fetch (adapter)  ->  plan (this module, read-only)  ->  apply (user confirms)

The planning half never writes. It compares platform records with local tasks
using the stable platform id only, so no task is merged because its title looks
similar. Applying is delegated to the existing reviewed-import path, which keeps
revision checks, history and all-or-nothing behaviour in one place.

Hard rules carried over from stage 5:

 - a failed, empty or partial response never deletes or completes a local task;
 - ``status == "OK"`` with zero sets is not evidence that a course is empty;
 - every record whose platform id is already mapped is idempotent by that id.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pku_deadline_agent.sources import (
    FetchResult,
    SourceRejected,
    external_key,
    notice_key,
)
from pku_deadline_agent.task_management import ImportDecision, NoticeOrigin, find_matches
from pku_deadline_agent.time_utils import beijing_now

# A local file is user-provided input, so it must not be unbounded.
MAX_IMPORT_BYTES = 4 * 1024 * 1024

NEW = "new"
UNCHANGED = "unchanged"
DEADLINE_CHANGED = "deadline_changed"
POSSIBLE_DUPLICATE = "possible_duplicate"
# Reported, never acted on: absence from one response is not proof of deletion.
MISSING_FROM_RESPONSE = "missing_from_response"

# Platform wording -> the task-type enum the database accepts. Every observed
# value was "Homework"; anything else falls back to 其他 and the original string
# is kept in the review notes, so an unmapped type is visible instead of silent.
TASK_TYPE_MAP = {"Homework": "作业", "Exam": "考试", "Test": "考试", "Activity": "活动报名"}
DEFAULT_TASK_TYPE = "其他"


def map_task_type(value: str) -> tuple[str, str | None]:
    """Return (local task type, unmapped original or None)."""
    text = (value or "").strip()
    if text in TASK_TYPE_MAP:
        return TASK_TYPE_MAP[text], None
    if text in {"作业", "考试", "活动报名", "其他"}:
        return text, None
    return DEFAULT_TASK_TYPE, text or None


class ImportFileError(ValueError):
    """The local file could not be read as a programming-grid response."""


class Candidate:
    """The shape the reviewed-import path and matcher expect for one task."""

    __slots__ = ("title", "category", "task_type", "due_at", "deadline_changed",
                 "source_index", "review_notes", "deadline_text")

    def __init__(self, record, source_index: int = 1):
        local_type, unmapped = map_task_type(record.task_type)
        self.title = record.task_title
        self.category = record.course
        self.task_type = local_type
        self.due_at = record.deadline
        self.deadline_changed = False
        self.source_index = source_index
        notes = ["编程网格导入"]
        if unmapped:
            notes.append(f"平台任务类型未映射：{unmapped}")
        self.review_notes = tuple(notes)
        self.deadline_text = None


def decode_grid_bytes(raw: bytes) -> dict:
    """Decode and parse raw course JSON that an uploader handed us.

    Shared by the file path reader and the Streamlit uploader so both report the
    same reasons and neither invents an empty result from unusable input.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise ImportFileError("输入不是字节内容，已中止导入。")
    if len(raw) == 0:
        raise ImportFileError("内容为空，已中止导入而不是当作没有题集。")
    if len(raw) > MAX_IMPORT_BYTES:
        raise ImportFileError(f"内容超过 {MAX_IMPORT_BYTES // 1024 // 1024} MB，已中止导入。")
    raw = bytes(raw)
    if b"\xff\xfe" in raw or b"\xfe\xff" in raw:
        # UTF-16 byte order marks also occur as ordinary byte pairs in GBK text.
        raise ImportFileError("内容似乎是 UTF-16 编码，请另存为 UTF-8 后重试。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportFileError("内容不是有效的 UTF-8 文本（可能是其他编码），已中止导入。") from None
    try:
        payload = json.loads(text)
    except ValueError:
        raise ImportFileError("内容不是有效的 JSON，已中止导入；数据库未被修改。") from None
    if not isinstance(payload, dict):
        raise ImportFileError("JSON 顶层不是对象，已中止导入；数据库未被修改。")
    return payload


def load_grid_file(path) -> dict:
    """Read a user-supplied course JSON file. Read-only: nothing is written back."""
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError as error:
        raise ImportFileError(f"无法读取文件（{error.__class__.__name__}）；请检查路径和权限。") from None
    if size == 0:
        raise ImportFileError("文件为空，已中止导入而不是当作没有题集。")
    try:
        raw = target.read_bytes()
    except OSError as error:
        raise ImportFileError(f"无法读取文件（{error.__class__.__name__}）。") from None
    return decode_grid_bytes(raw)


def parse_grid_payload(payload, adapter_class):
    """Run an already-read payload through a grid adapter; performs no I/O."""
    return adapter_class(_PayloadTransport(payload)).fetch()


class _PayloadTransport:
    """Hands an already-read payload to an adapter; performs no I/O at all."""

    def __init__(self, payload):
        self.payload = payload

    def request(self, *, since, source):
        return self.payload


@dataclass(frozen=True)
class SyncProposal:
    """One platform record and what we suggest doing about it.

    ``source_key`` is the readable platform identity ("source:id"). The database
    stores only its ``notice_key`` hash, so both forms are kept explicit here
    instead of one being silently derived at each use site.
    """

    record: object
    kind: str
    task_id: int | None = None
    expected_revision: int | None = None
    current_deadline: datetime | None = None
    reason: str = ""
    source_key: str = ""

    @property
    def stored_key(self) -> str:
        """What goes into the task database for this platform object."""
        return notice_key(self.source_key)

    @property
    def is_change(self) -> bool:
        return self.kind == DEADLINE_CHANGED

    @property
    def is_new(self) -> bool:
        return self.kind == NEW


@dataclass(frozen=True)
class SyncPlan:
    """Read-only result of comparing one platform response with local tasks."""

    source: str
    course: str = ""
    proposals: tuple[SyncProposal, ...] = ()
    rejections: tuple = ()
    warnings: tuple[str, ...] = ()
    fetched_at: datetime | None = None
    trustworthy: bool = False

    def __post_init__(self):
        if not self.trustworthy and not self.rejections and not self.warnings:
            # A plan that is neither trustworthy nor explains itself would invite
            # the caller to treat an unexplained empty result as "nothing to do".
            raise ValueError("不可信的同步方案必须说明原因（rejections 或 warnings）。")

    @property
    def new_count(self) -> int:
        return sum(1 for item in self.proposals if item.kind == NEW)

    @property
    def change_count(self) -> int:
        return sum(1 for item in self.proposals if item.kind == DEADLINE_CHANGED)

    @property
    def unchanged_count(self) -> int:
        return sum(1 for item in self.proposals if item.kind == UNCHANGED)

    @property
    def duplicate_count(self) -> int:
        return sum(1 for item in self.proposals if item.kind == POSSIBLE_DUPLICATE)

    def actions(self) -> tuple[tuple[SyncProposal, ImportDecision], ...]:
        """Default proposals. Deadline changes still need the user's confirmation.

        New records carry ``allow_distinct``: the platform id is what decides
        identity here, and a real course may legitimately hold several sets with
        identical titles (observed: two "期中试题" with the same deadline). Two
        rows sharing one platform id are refused earlier, so this cannot merge or
        duplicate the same object.
        """
        decisions = []
        for item in self.proposals:
            if item.kind == NEW:
                decisions.append((item, ImportDecision(NEW, allow_distinct=True,
                                                       reason="从编程网格导入新题集（以平台标识判定为新对象）")))
            elif item.kind == POSSIBLE_DUPLICATE:
                decisions.append((item, ImportDecision(NEW, allow_distinct=True, reason="平台 ID 未关联，经核对后作为独立任务新增")))
            elif item.kind == DEADLINE_CHANGED:
                decisions.append((item, ImportDecision(
                    "update", target_id=item.task_id, expected_revision=item.expected_revision,
                    reason="编程网格题集截止时间变化，经核对后更新")))
            else:
                decisions.append((item, ImportDecision("skip", target_id=item.task_id, reason="已是最新，无需改动")))
        return tuple(decisions)


def plan_sync(records, result: FetchResult, repository, *, source="programming_grid") -> SyncPlan:
    """Compare platform records with local tasks. Never writes, never deletes."""
    if not result.trustworthy:
        # Refuse to build an actionable plan from a batch we cannot vouch for.
        result.require_trustworthy()
    if source != result.source:
        raise SourceRejected(f"批次来源 {result.source!r} 与预期 {source!r} 不一致。")

    tasks = list(repository.list_tasks(include_completed=True, include_deleted=True))
    by_id = {task.id: task for task in tasks}
    hashes: dict[int, set[str]] = {}
    for task in tasks:
        hashes[task.id] = repository.source_keys(source, (task.id,))
    mappings = repository.source_mappings(source)

    course = records[0].course if records else ""
    proposals, warnings = [], list(result.warnings)
    candidates = [Candidate(record, index) for index, record in enumerate(records, start=1)]
    seen_keys: set[str] = set()
    for record, candidate in zip(records, candidates):
        key = external_key(record)
        hashed = notice_key(key)
        if hashed in seen_keys:
            # The same platform object twice in one response is a platform-side
            # problem; importing both would create a duplicate we cannot undo.
            raise SourceRejected(f"同一响应里出现重复的平台标识（{record.source_id}），已中止导入。")
        seen_keys.add(hashed)
        linked_id = mappings.get(hashed)
        if linked_id is not None and linked_id in by_id:
            task = by_id[linked_id]
            if task.due_at == record.deadline:
                proposals.append(SyncProposal(record, UNCHANGED, task.id, task.revision, task.due_at,
                                              "平台 ID 已关联且截止时间一致", key))
            else:
                proposals.append(SyncProposal(record, DEADLINE_CHANGED, task.id, task.revision, task.due_at,
                                              _change_reason(task.due_at, record.deadline), key))
            continue
        if linked_id is not None:
            # The id points at a task that is gone from the table we just read;
            # treat it as a new task instead of silently repointing the mapping.
            warnings.append(f"{record.task_title} 的来源映射指向不存在的任务 #{linked_id}，将作为新任务处理。")
        matches = _exact_title_matches(record, candidate, tasks, hashes, hashed)
        if matches:
            first = matches[0].task
            proposals.append(SyncProposal(record, POSSIBLE_DUPLICATE, first.id, first.revision, first.due_at,
                                          f"未关联的平台 ID，但与 #{first.id}「{first.title}」可能重复：{matches[0].reason}", key))
        else:
            proposals.append(SyncProposal(record, NEW, None, None, None, "平台 ID 未出现在本地，作为新题集", key))
    return SyncPlan(source=source, course=course, proposals=tuple(proposals), rejections=tuple(result.rejections),
                    warnings=tuple(warnings), fetched_at=result.fetched_at, trustworthy=result.trustworthy)


def _exact_title_matches(record, candidate, tasks, hashes, hashed):
    """Only exact title+course matches are raised as duplicates for review.

    The stage 4 matcher is deliberately fuzzy and reports "第一阶段练习（1）"
    against "（2）" as a possible duplicate. That is the right signal for a pasted
    notice, but for a platform that hands us authoritative object ids it would
    bury the real decisions under dozens of numbered-exercise suggestions. So the
    fuzzy matcher stays available, and here it is narrowed to its certain case:
    the same title in the same course. Different platform ids are still never
    merged, and everything unmatched is imported as new with its own id.
    """
    from pku_deadline_agent.task_management import identity

    wanted = identity(record.task_title)
    course = identity(record.course)
    for task in tasks:
        if identity(task.title) != wanted:
            continue
        if course and identity(task.category) and identity(task.category) != course:
            continue
        return find_matches(candidate, tasks, hashes, hashed)
    return ()


def _change_reason(old: datetime | None, new: datetime | None) -> str:
    if old is None:
        return "原本没有截止时间，平台现在给出了明确截止时间"
    if new is None:
        return "平台不再提供截止时间，保留原值需人工确认"
    return "截止时间延后" if new > old else "截止时间提前"


def plan_from_fetch(documents, repository, adapter_class, *, source="programming_grid") -> SyncPlan:
    """Build one reviewed plan from several fetched course documents.

    Used by the one-click sync path so it shares the exact planning and trust
    rules as the file import. A document that cannot be parsed, or an attempt
    where any course failed, stops the whole plan: a missing course must never be
    mistaken for a course whose sets disappeared.
    """
    if not documents:
        raise SourceRejected("没有取到任何课程数据，已中止而不是当作没有题集。")
    records, rejections, warnings = [], [], []
    for url, payload in documents:
        result = parse_grid_payload(payload, adapter_class)
        if not result.trustworthy:
            # Refuse the whole attempt and say which document was unusable.
            raise SourceRejected(
                f"课程数据未通过可信检查（{url}）："
                + "；".join([item.reason for item in result.rejections] + list(result.warnings))
            )
        records.extend(result.records)
        rejections.extend(result.rejections)
        warnings.extend(result.warnings)
    if not records:
        raise SourceRejected("取到的课程里没有题集；已中止而不是当作没有题集。")
    collected = FetchResult(source=source, records=tuple(records), rejections=tuple(rejections),
                            warnings=tuple(warnings), fetched_at=beijing_now())
    return plan_sync(collected.records, collected, repository, source=source)


def apply_sync_plan(repository, plan: SyncPlan, decisions, *, confirmed=False):
    """Write a reviewed plan through the existing all-or-nothing import path."""
    if not confirmed:
        raise ValueError("请确认同步方案后再写入。")
    if not plan.trustworthy:
        raise ValueError("本次结果未通过可信检查，已拒绝写入。")
    if len(decisions) != len(plan.proposals):
        raise ValueError("同步方案与处理决定数量不一致，已中止。")
    candidates, chosen, origins = [], [], []
    for item, decision in zip(plan.proposals, decisions):
        if decision.action == "skip":
            continue
        if decision.action == "update" and decision.target_id is None:
            raise ValueError("更新需要明确的目标任务编号。")
        candidates.append(Candidate(item.record))
        chosen.append(decision)
        origins.append(NoticeOrigin(plan.source, item.stored_key, item.record.published_at))
    if not candidates:
        return []
    return repository.apply_reviewed_tasks(tuple(candidates), tuple(chosen), tuple(origins), confirmed=True)


def describe_plan(plan: SyncPlan) -> str:
    """One-line summary for the page and for logs (no titles, no ids)."""
    return (f"新题集 {plan.new_count}，截止变化 {plan.change_count}，"
            f"无变化 {plan.unchanged_count}，待核对重复 {plan.duplicate_count}，"
            f"无法解析 {len(plan.rejections)}")
