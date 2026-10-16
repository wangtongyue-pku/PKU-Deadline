"""Read one already-open unfinished-homework view, without login or navigation.

No SQLite, AI, HTTP, cookie/storage access or persistent browser state. The caller
owns the existing Playwright Page/context and must close it. Real account use
needs separate human authorization AND a genuinely verified layout profile.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Protocol
from urllib.parse import unquote, urljoin, urlsplit

from pku_deadline_agent.grid_session import SessionAuthRequired, SessionLayoutChanged
from pku_deadline_agent.privacy import scan_privacy
from pku_deadline_agent.sources import SourceRejected, SourceUnverified
from pku_deadline_agent.zhihuishu_display import parse_display_deadline
from pku_deadline_agent.zhihuishu_dom import SNAPSHOT_SCRIPT

MAX_VISIBLE_TASKS = 20
REAL_HOST = 'wenda.zhihuishu.com'
# Public, static list route observed under explicit authorization; no query IDs.
# Only validation mode may use it until a full production profile is verified.
FLIPPED_HOMEWORK_LIST_PATH = '/stu/courseInfo/homeworkExam/'


class ReaderError(SessionLayoutChanged):
    """Fixed, non-sensitive errors; never echo browser exception text."""


class ReaderLimitExceeded(ReaderError):
    def __init__(self, loaded: int, visible: int, observed_positions: tuple[int, ...] = ()):
        self.loaded, self.visible = loaded, visible
        self.observed_positions = observed_positions
        super().__init__(f'当前加载 {loaded} 个卡片、可见 {visible} 个；超过本次上限，未读取任务字段。')


class ReaderUnstable(ReaderError):
    def __init__(self, observed_positions: tuple[int, ...] = ()):
        self.observed_positions = observed_positions
        super().__init__('两次有界观察不一致，页面仍在变化；请等待加载完成。')


@dataclass(frozen=True)
class ListProfile:
    """Unobserved selectors have NO production defaults. Not page-supplied code.

    Set real_verified only after an authorized inspection has verified ALL field,
    course and coverage selectors; fictional fixtures do not qualify.
    No real_verified profile is shipped in this iteration.
    """

    title: str
    course_scope: str
    course: str
    deadline: str = '.homework-item-time'
    total: str | None = None
    next: str | None = None
    empty: str | None = None
    loading: str | None = None
    lazy: str | None = None
    virtual: str | None = None
    card_course: str | None = None
    link: str | None = None
    task_id_attribute: str | None = None
    course_id_attribute: str | None = None
    enrollment_id_attribute: str | None = None
    scan_position_attribute: str | None = None  # Temporary viewport traversal, never source ID.
    scan_position_base: int = 1
    real_verified: bool = False

    def __post_init__(self):
        for name in ('title', 'course_scope', 'course'):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError('必须显式提供字段与课程范围选择器；不猜实际页面结构。')
        for name, value in asdict(self).items():
            if name == 'scan_position_attribute':
                if value not in (None, 'aria-posinset', 'aria-rowindex', 'data-index'):
                    raise ValueError('滚动位置只允许已核验的列表序号属性。')
                continue
            if name.endswith('_attribute') and value is not None:
                if not re.fullmatch(r'data-(?!v-|.*(?:token|cookie|auth|user|account|password|key|secret|session|credential|email|student))[a-z-]{1,40}', value):
                    raise ValueError('仅允许已核验的非认证 data 标识属性；不读取任意属性。')
        if type(self.real_verified) is not bool:
            raise ValueError('配置核验标记必须是布尔值。')
        if type(self.scan_position_base) is not int or self.scan_position_base not in (0, 1):
            raise ValueError('列表序号基准只支持 0 或 1。')


@dataclass(frozen=True)
class ReadTask:
    # These objects are private, in-memory observations, not diagnostic log data.
    title: str = field(repr=False)
    course: str | None = field(repr=False)
    deadline: datetime | None = field(default=None, repr=False)
    deadline_date: str | None = field(default=None, repr=False)
    source_url: str | None = field(default=None, repr=False)
    observed_task_id: str | None = field(default=None, repr=False)
    observed_course_id: str | None = field(default=None, repr=False)
    observed_enrollment_id: str | None = field(default=None, repr=False)
    status: str = '未完成'
    kind: str = 'homework'
    opens_at: datetime | None = None  # Never filled from a deadline or exam page.
    identity_verified: bool = False  # One snapshot cannot prove ID stability.
    scan_position: int | None = None  # Session-local position, not a persistent identity.


@dataclass(frozen=True)
class ReadIssue:
    code: str
    position: int | None = None  # Ephemeral viewport ordinal, NOT a source ID.


@dataclass(frozen=True)
class Coverage:
    loaded_count: int
    visible_count: int
    read_count: int
    rejected_count: int
    outside_view_count: int
    displayed_total: int | None
    has_next: bool | None
    loading: bool
    lazy: bool
    virtual: bool
    unread_minimum: int
    unread_scope: tuple[str, ...]
    view_complete: bool


@dataclass(frozen=True)
class ReadReport:
    tasks: tuple[ReadTask, ...] = field(repr=False)
    coverage: Coverage
    issues: tuple[ReadIssue, ...]
    verification: str  # synthetic[-validation] / authorized-real-{profile,validation}
    fragments: tuple[ReadTask, ...] = field(default=(), repr=False)
    observed_positions: tuple[int, ...] = ()
    course_context: str | None = field(default=None, repr=False)

    @property
    def fields_valid(self) -> bool:
        return not self.issues

    @property
    def ready_for_sync(self) -> bool:
        # This iteration has no verified stable identities or write gateway.
        return False

    @property
    def confirmed_empty(self) -> bool:
        return self.coverage.view_complete and self.coverage.displayed_total == 0

    def require_sync_ready(self) -> None:
        raise SourceUnverified('仅为有界读取结果；身份稳定性及审核入库尚未验证，不能直接同步。')


class ReadablePage(Protocol):
    url: str

    def evaluate(self, expression: str, arg: dict) -> dict: ...


def _text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise SourceRejected('字段类型或长度不正确。')
    result = re.sub(r'\s+', ' ', value).strip() or None
    if result and scan_privacy(result).blocked:
        raise SourceRejected('字段含认证资料或无法确认的风险。')
    return result


def _source_link(value: str | None, base: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2000 or any(c.isspace() for c in value):
        raise SourceRejected('来源链接无效。')
    result = urljoin(base, value)
    try:
        p = urlsplit(result)
        unsafe = (p.scheme != 'https' or p.hostname != REAL_HOST or p.username or p.password
                  or p.port not in (None, 443) or p.query or p.fragment or '\\' in result
                  or re.search(r'exam|paper|answer|submit|video|learn|login|auth', unquote(p.path), re.I)
                  or any(f.blocks_sending and f.kind != '不明长串（需本地核查）'
                         for f in scan_privacy(unquote(result)).findings))
    except ValueError:
        unsafe = True
    if unsafe:
        raise SourceRejected('来源链接未通过只读安全规则；不访问或改写。')
    return result


def _identifier(value: str | None) -> str | None:
    # IDs are allowed to be opaque, but never interpreted as proven identities.
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,200}', value):
        raise SourceRejected('观察到的标识格式无效。')
    if any(f.blocks_sending and f.kind != '不明长串（需本地核查）'
           for f in scan_privacy(value).findings):
        raise SourceRejected('标识字段含认证资料。')
    return value


def _normalize(snapshot: dict, *, base: str, verification: str,
               capture_fragments: bool = False) -> ReadReport:
    counts = snapshot['counts']
    issues: list[ReadIssue] = []
    tasks: list[ReadTask] = []
    fragments: list[ReadTask] = []
    course = _text(snapshot['course'], 100)
    if not course:
        issues.append(ReadIssue('course_binding_missing'))
    scope_ids = tuple(_identifier(snapshot[key]) for key in ('course_id', 'enrollment_id'))
    observed_ids: set[str] = set()
    for raw in snapshot['tasks']:
        position = raw['position']
        if raw.get('error'):
            issues.append(ReadIssue(raw['error'], position))
            continue
        try:
            title = _text(raw['title'], 200)
            if not title:
                issues.append(ReadIssue('title_missing', position))
                if not capture_fragments:
                    continue
            card_course = _text(raw['course'], 100)
            if card_course and card_course != course:
                issues.append(ReadIssue('course_binding_conflict', position))
                continue
            due, date_only = parse_display_deadline(_text(raw['deadline'], 80))
            if due is None:
                issues.append(ReadIssue('deadline_time_missing' if date_only else 'deadline_missing', position))
            observed_id = _identifier(raw['task_id'])
            if observed_id and observed_id in observed_ids:
                issues.append(ReadIssue('duplicate_observed_id', position))
            if observed_id:
                observed_ids.add(observed_id)
            link = None
            try:
                link = _source_link(raw['source_url'], base)
            except SourceRejected:
                issues.append(ReadIssue('unsafe_source_link', position))
            task = ReadTask(title or '', course, due, date_only, link, observed_id, *scope_ids,
                            scan_position=raw.get('scan_position'))
            if capture_fragments:
                fragments.append(task)
            if title:
                tasks.append(task)
        except SourceRejected:
            issues.append(ReadIssue('invalid_task_field', position))
    total_text = counts['total_text']
    total = None
    if total_text is not None:
        match = re.fullmatch(r'共\s*([0-9]{1,6})\s*[条项]', total_text)
        if match:
            total = int(match[1])
        else:
            issues.append(ReadIssue('total_format_changed'))
    if total is not None and total < counts['loaded']:
        issues.append(ReadIssue('total_count_conflict'))
    if not tasks and not (snapshot['empty'] and total == 0 and counts['loaded'] == 0):
        issues.append(ReadIssue('empty_not_proven'))
    if snapshot['empty'] and counts['loaded']:
        issues.append(ReadIssue('empty_state_conflict'))
    scope: list[str] = []
    if verification in ('authorized-real-validation', 'synthetic-validation'):
        # Partial selector research is not a verified production layout, even if
        # an unverified counter happens to equal the number of observed cards.
        scope.append('layout_validation_only')
    if counts['outside_view']:
        scope.append('loaded_outside_view')
    if total is None:
        scope.append('total_unknown')
    elif total > len(tasks):
        scope.append('displayed_total_not_read')
    if counts['has_next']:
        scope.append('later_pages')
    for flag in ('loading', 'lazy', 'virtual'):
        if counts[flag]:
            scope.append(flag)
    coverage = Coverage(counts['loaded'], counts['visible'], len(tasks),
                        counts['visible'] - len(tasks), counts['outside_view'], total,
                        counts['has_next'], counts['loading'], counts['lazy'], counts['virtual'],
                        max(0, max(counts['loaded'], total or 0) - len(tasks)),
                        tuple(scope), not scope and not issues)
    return ReadReport(tuple(tasks), coverage, tuple(issues), verification, tuple(fragments),
                      tuple(snapshot.get('observed_positions', ())), course)


class BoundedTaskListReader:
    """Only two atomic observations of the same view; no retry/scroll/click/goto.

    Real use consumes authorization supplied by the human through the caller.
    A verified layout is an additional engineering gate, never a substitute for
    that authorization. No currently shipped profile is real-verified.
    """

    def __init__(self, profile: ListProfile, *, limit: int = MAX_VISIBLE_TASKS):
        if type(limit) is not int or not 1 <= limit <= MAX_VISIBLE_TASKS:
            raise ValueError('可见任务上限必须为 1–20。')
        self.profile, self.limit = profile, limit

    def check_access(self, page: ReadablePage, *, allow_real: bool = False,
                     validation_only: bool = False) -> tuple[str, str, str]:
        """Explicit validation mode permits an incomplete, observed profile.

        This is only for a separately authorized inspection, NOT a production
        fallback. All URL/privacy/viewport/count/concurrency gates remain active;
        validation results can never claim a complete view or confirmed empty.
        Default real reads still require a fully verified profile.
        """
        if type(validation_only) is not bool:
            raise ValueError('验证模式必须显式传入布尔值。')
        url = page.url
        if url == 'about:blank':
            verification = 'synthetic-validation' if validation_only else 'synthetic'
            base = f'https://{REAL_HOST}/'
        else:
            try:
                parts = urlsplit(url)
                safe = (parts.scheme == 'https' and parts.hostname == REAL_HOST
                        and not parts.username and not parts.password and parts.port in (None, 443)
                        and (not re.search(r'exam|paper|answer|submit|video|learn|login|auth',
                                           unquote(parts.path), re.I)
                             or (validation_only and parts.path == FLIPPED_HOMEWORK_LIST_PATH)))
            except ValueError:
                safe = False
            if not safe or allow_real is not True:
                raise PermissionError('未授权的页面或真实读取；未读取任务字段。')
            if not self.profile.real_verified and not validation_only:
                raise PermissionError('字段 / 课程 / 完整性配置仅经模拟验证，不能用于真实页面。')
            verification = 'authorized-real-validation' if validation_only else 'authorized-real-profile'
            base = url
        return url, verification, base

    def read(self, page: ReadablePage, *, allow_real: bool = False,
             validation_only: bool = False, capture_fragments: bool = False,
             seen_positions: tuple[int, ...] = ()) -> ReadReport:
        url, verification, base = self.check_access(page, allow_real=allow_real,
                                                     validation_only=validation_only)
        if type(capture_fragments) is not bool or len(seen_positions) > self.limit or any(
                type(x) is not int or x < 1 for x in seen_positions):
            raise ValueError('滚动片段 / 累计范围参数无效。')
        params = asdict(self.profile) | {'limit': self.limit, 'capture_fragments': capture_fragments,
                                        'seen_positions': seen_positions}
        observations = []
        touched = set(seen_positions)
        for _ in range(2):
            if page.url != url:
                raise ReaderError('读取期间页面跳转，已停止。')
            try:
                snapshot = page.evaluate(SNAPSHOT_SCRIPT, params)
            except Exception:
                raise ReaderError('DOM 读取失败或页面结构变化；未生成可信结果。') from None
            if not isinstance(snapshot, dict):
                raise ReaderError('DOM 返回结构无效。')
            code = snapshot.get('error')
            if code in ('visible_task_limit', 'cumulative_task_limit'):
                c = snapshot['counts']
                raise ReaderLimitExceeded(c['loaded'], c['visible'], tuple(sorted(touched)))
            if code == 'authentication_required':
                raise SessionAuthRequired('需要用户本人重新登录；没有任务读取结果。')
            if code:
                raise ReaderError('列表结构 / 选中视图或字段发生变化，已停止。')
            observations.append(snapshot)
            if capture_fragments:
                touched.update(snapshot.get('observed_positions', ()))
                params['seen_positions'] = tuple(sorted(touched))
        if page.url != url:
            raise ReaderError('读取期间页面跳转，已停止。')
        if observations[0] != observations[1]:
            raise ReaderUnstable(tuple(sorted(touched)))
        try:
            return _normalize(observations[0], base=base, verification=verification,
                              capture_fragments=capture_fragments)
        except (KeyError, TypeError, ValueError, SourceRejected):
            raise ReaderError('读取结果校验失败；未当成没有任务。') from None


def possible_matches(task: ReadTask, previous: tuple[ReadTask, ...]) -> tuple[int, ...]:
    """Suggestions only: keep namesakes distinct, dates out of the identity.

    Return temporary candidate positions for explicit review; NEVER a merge key.
    Even a repeated observed DOM ID is not verified stable across page reloads.
    """
    def normalized(value):
        return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value or '')).strip().casefold()

    if not task.course:
        return ()
    return tuple(i for i, old in enumerate(previous)
                 if normalized(task.course) == normalized(old.course)
                 and normalized(task.title) == normalized(old.title) and task.kind == old.kind)


# Observed under the 2026-10-16 limited real-page authorization. Missing counters,
# IDs and offscreen course names are deliberately not inferred. This is NOT a
# fully verified profile: use only with separately authorized validation_only.
FLIPPED_HOMEWORK_VALIDATION_PROFILE = ListProfile(
    title='.title-wrapper > .tit',
    course_scope='.qa-list-wrapper',
    course='.banner .course-info .course-name > .name',
    deadline='.homework-item-time > span:first-child',
    link='a[href]',
)


def summarize_validation(reports: tuple[ReadReport, ...]) -> dict:
    """Safe aggregate output only; raw titles/courses/times/IDs never leave memory.

    Equality across this same viewport is short-term consistency, NOT evidence
    for identity stability across reloads, different courses or enrollment terms.
    """
    if not reports or len(reports) > 3:
        raise ValueError('验证摘要需要 1–3 次有界读取。')
    first = reports[0]
    known_times = sum(t.deadline is not None for t in first.tasks)
    available_ids = sum(t.observed_task_id is not None for t in first.tasks)
    return {
        'readings': len(reports),
        'same_view_fields_equal': all(r.tasks == first.tasks for r in reports[1:]),
        'coverage_equal': all(r.coverage == first.coverage for r in reports[1:]),
        'loaded_count': first.coverage.loaded_count,
        'visible_count': first.coverage.visible_count,
        'read_count': first.coverage.read_count,
        'title_count': len(first.tasks),
        'course_bound_count': sum(t.course is not None for t in first.tasks),
        'known_deadline_count': known_times,
        'unknown_deadline_count': len(first.tasks) - known_times,
        'pending_status_count': sum(t.status == '未完成' for t in first.tasks),
        'source_link_count': sum(t.source_url is not None for t in first.tasks),
        'available_task_id_count': available_ids,
        'id_stability': 'unverified_same_view_only' if available_ids else 'not_evaluable_no_id',
        'displayed_total': first.coverage.displayed_total,
        'pagination_has_next': first.coverage.has_next,
        'lazy_loading_assessment': 'observed' if first.coverage.lazy else 'unverified',
        'virtual_scroll_assessment': 'observed' if first.coverage.virtual else 'unverified',
        'outside_view_count': first.coverage.outside_view_count,
        'unread_minimum': first.coverage.unread_minimum,
        'unread_scope': first.coverage.unread_scope,
        'view_complete': first.coverage.view_complete,
        'ready_for_sync': False,
        'verification': first.verification,
        'issues': tuple({'code': i.code, 'position': i.position} for i in first.issues),
    }
