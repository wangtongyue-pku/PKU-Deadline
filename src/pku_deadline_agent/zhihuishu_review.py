"""Local comparison and simulated decisions; no database writer or AI client."""

from dataclasses import dataclass, field, asdict, replace
from datetime import datetime
import hashlib
import json

from pku_deadline_agent.extraction import ExtractedTask
from pku_deadline_agent.models import Task
from pku_deadline_agent.task_management import ImportDecision, MatchSuggestion, find_matches, identity
from pku_deadline_agent.time_utils import BEIJING, normalize_datetime
from pku_deadline_agent.zhihuishu_reader import ReadTask, _text, possible_matches
from pku_deadline_agent.zhihuishu_scroll import ScrollReport

CATEGORIES = ('待新增', '可能重复', '疑似截止时间变更', '信息不足')
UNKNOWN_DEADLINE = '截止日期或时刻缺失，不自动补造'
# Sibling candidates only need stable fields for the shared matcher; they are
# never stored or compared for identity.
SIBLING_STAMP = datetime.min.replace(tzinfo=BEIJING)

# Where a preview's data came from. The reader stamps every report with one of
# these; the page labels the origin honestly, and the sandbox import path only
# ever accepts fictional material (see is_fictional).
ORIGIN_LABELS = {
    'synthetic': '虚构列表（样例或离线页面读取）',
    'synthetic-validation': '虚构页面读取（验证模式）',
    'synthetic-review': '虚构列表（开发样例）',
    'authorized-real-validation': '受限真实读取（验证模式）',
    'authorized-real-profile': '真实读取（已核验完整配置）',
}


def origin_label(report: ScrollReport) -> str:
    """Human-readable provenance for a report; unknown markers are refused."""
    label = ORIGIN_LABELS.get(getattr(report, 'evidence', None))
    if label is None:
        raise ValueError('读取结果缺少可辨认的来源标记，拒绝进入预览；请重新通过有界读取器获取。')
    return label


def is_fictional(report: ScrollReport) -> bool:
    """True for sample or fictional-page reads — the only material the sandbox
    import path may ever write. Real-read evidence previews but never writes."""
    return report.evidence.startswith('synthetic')


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    default=str).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class ReviewItem:
    index: int  # This preview only; never a platform/source ID.
    task: ReadTask = field(repr=False)
    category: str
    matches: tuple[MatchSuggestion, ...] = field(repr=False)
    warnings: tuple[str, ...]
    batch_peers: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReviewSnapshot:
    report: ScrollReport = field(repr=False)
    items: tuple[ReviewItem, ...] = field(repr=False)
    local_tasks: tuple[Task, ...] = field(repr=False)
    digest: str = field(repr=False)


@dataclass(frozen=True)
class ManualEdit:
    """User corrections for one previewed item.

    Only the fields the user actually changed are set; ``None`` means "leave the
    read value alone", while an empty string means "there is no value". A
    correction never invents a platform fact: it is local user input, validated by
    the same rules as the reader, and the item stays partial.
    """

    title: str | None = None
    course: str | None = None
    deadline: datetime | None = None
    deadline_date: str | None = None
    status: str | None = None


def apply_edits(report: ScrollReport, edits) -> ScrollReport:
    """Rebuild the report with the user's manual corrections applied.

    Provenance, coverage and completeness are deliberately untouched: completing a
    field by hand makes a task importable, it does not make the READ complete.
    """
    if not isinstance(edits, dict):
        raise ValueError('手动补充信息无效。')
    tasks = []
    for index, task in enumerate(report.tasks, 1):
        edit = edits.get(index)
        if edit is None:
            tasks.append(task)
            continue
        if not isinstance(edit, ManualEdit):
            raise ValueError('手动补充信息无效。')
        title = task.title if edit.title is None else _text(edit.title, 200)
        course = task.course if edit.course is None else (_text(edit.course, 100) or None)
        deadline, date_text = task.deadline, task.deadline_date
        if edit.deadline_date is not None or edit.deadline is not None:
            deadline, date_text = edit.deadline, (_text(edit.deadline_date, 80) or None)
        status = task.status if edit.status is None else edit.status
        if status not in ('未完成', '已完成'):
            raise ValueError('平台状态只能是未完成或已完成。')
        if deadline is not None:
            normalize_datetime(deadline)
        tasks.append(replace(task, title=title, course=course, deadline=deadline,
                             deadline_date=date_text, status=status))
    return replace(report, tasks=tuple(tasks))


def build_review(report: ScrollReport, tasks) -> ReviewSnapshot:
    if len(report.tasks) > 20:
        raise ValueError('预览累计最多 20 条任务。')
    local = tuple(t for t in tasks if t.deleted_at is None)
    # Sibling candidates as plain Tasks, so the preview can ask the SAME matcher
    # the write transaction uses. Without this the conflict only appears after the
    # user has already confirmed twice, as a rolled-back batch.
    siblings = tuple(Task(index + 1, task.title, task.course or '', '作业', task.deadline,
                          'zhihuishu', False, SIBLING_STAMP, SIBLING_STAMP)
                     for index, task in enumerate(report.tasks))
    items = []
    for index, task in enumerate(report.tasks):
        warnings = []
        if not task.title.strip(): warnings.append('任务名称缺失')
        if not task.course or not task.course.strip(): warnings.append('课程绑定不确定')
        if not report.course: warnings.append('读取批次尚未确认课程上下文')
        if report.course and task.course != report.course: warnings.append('课程与读取上下文不一致')
        if task.deadline is None: warnings.append(UNKNOWN_DEADLINE)
        else: normalize_datetime(task.deadline)
        if task.status not in ('未完成', '已完成'): warnings.append('平台完成状态未知')
        if task.kind != 'homework': warnings.append('当前读取路线仅验证作业，不混用其他时间语义')
        candidate = ExtractedTask(index+1, task.title, task.course or '', '作业',
                                  None, task.deadline, False, ())
        matches = find_matches(candidate, list(local), {}) if task.title.strip() else ()
        peers = batch_peers(index + 1, task, candidate, siblings, report.tasks)
        category = ('信息不足' if warnings else
                    '疑似截止时间变更' if any(m.kind == 'deadline_change' for m in matches) else
                    '可能重复' if matches or peers else '待新增')
        items.append(ReviewItem(index+1, task, category, matches, tuple(warnings), peers))
    digest = fingerprint({'report': asdict(report), 'local': [asdict(t) for t in local]})
    return ReviewSnapshot(report, tuple(items), local, digest)


def batch_peers(index: int, task: ReadTask, candidate: ExtractedTask,
                siblings: tuple[Task, ...], batch: tuple[ReadTask, ...]) -> tuple[int, ...]:
    """Batch positions the WRITE-TIME matcher would call similar to this one.

    Two rules, both reusing existing code so preview and transaction agree:
    exact namesakes (``possible_matches``, which ignores dates) and the shared
    conservative similarity rule (``find_matches``). Flagging them here lets the
    user confirm "still separate tasks" BEFORE the batch is prepared, instead of
    discovering it as a rollback after two confirmations. Suggestions only: the
    positions are never identity and never merge anything.
    """
    exact = {i + 1 for i in possible_matches(task, batch) if i != index - 1}
    others = [item for item in siblings if item.id != index]
    similar = {match.task.id for match in find_matches(candidate, others, {})}
    return tuple(sorted(exact | similar))


@dataclass(frozen=True)
class SimulatedOperation:
    index: int
    action: str
    target: Task | None = field(repr=False)
    incoming: ReadTask = field(repr=False)
    reason: str = field(repr=False)


@dataclass(frozen=True)
class SimulationPlan:
    operations: tuple[SimulatedOperation, ...] = field(repr=False)
    list_complete: bool
    simulation_only: bool = True


def simulate_choices(snapshot: ReviewSnapshot, choices: dict[int, ImportDecision], *, unknown_deadlines=frozenset()) -> SimulationPlan:
    """Validate a proposed plan in memory. This type cannot be applied by sync.py."""
    items = {i.index: i for i in snapshot.items}
    local = {t.id: t for t in snapshot.local_tasks}
    operations, targets = [], set()
    for index, choice in choices.items():
        if index not in items or choice.action not in ('new', 'link', 'update', 'skip'):
            raise ValueError('预览项或模拟操作无效。')
        if choice.action == 'skip': continue
        item = items[index]
        unknown_new = (item.warnings == (UNKNOWN_DEADLINE,) and choice.action == 'new'
                       and index in unknown_deadlines)
        if item.category == '信息不足' and not unknown_new:
            raise ValueError(f'第 {index} 项信息不足，只能查看或暂不处理。')
        target = None
        if choice.action == 'new':
            if choice.target_id is not None:
                raise ValueError('拟新增不能同时关联本地目标。')
            if (item.matches or item.batch_peers) and not choice.allow_distinct:
                raise ValueError('可能重复的独立新增须明确选择仍作为不同任务。')
        else:
            target = local.get(choice.target_id)
            if target is None or target.revision != choice.expected_revision:
                raise ValueError('未选择有效目标，或目标版本已变化，请重新核对。')
            if identity(target.category) != identity(item.task.course or '') or target.task_type != '作业':
                raise ValueError('课程或类型不一致，不能拟关联或改期；请先核对来源信息。')
            if target.id in targets:
                raise ValueError('多条候选指向同一本地任务，须先解决匹配冲突。')
            targets.add(target.id)
            if choice.action == 'update':
                if target.due_at == item.task.deadline:
                    raise ValueError('截止相同，无需拟修改，可选择拟关联。')
                if not choice.reason.strip():
                    raise ValueError('拟修改截止时间必须填写原因。')
        operations.append(SimulatedOperation(index, choice.action, target, item.task, choice.reason.strip()))
    return SimulationPlan(tuple(operations), snapshot.report.coverage.complete)
