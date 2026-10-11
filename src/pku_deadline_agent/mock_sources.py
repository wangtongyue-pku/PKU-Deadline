"""Stage 5 mock adapters. Fully offline: fictional payloads, no network import.

No real platform request, login, credential or session data is used or stored here.
The mock transport exists so adapter parsing, field mapping and failure handling can
be tested before any real account is authorized in stage 6.
"""

import re
from datetime import datetime

from pku_deadline_agent.sources import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_PENDING,
    TASK_TYPE_OTHER,
    FetchResult,
    Rejection,
    SourceAdapter,
    SourceAuthRequired,
    SourceCapabilities,
    SourceRateLimited,
    SourceRecord,
    SourceRejected,
    SourceUnavailable,
    SourceUnsupported,
    label_identifier,
    register_adapter,
)
from pku_deadline_agent.time_utils import BEIJING, beijing_now

# Fictional payloads for local testing only. Course, title and platform id values
# are invented; nothing here is copied from a real announcement or task.
FICTIONAL_TEACHING_PAYLOAD = (
    {
        "id": "fictional-course-001",
        "name": "虚构课程甲",
        "assignments": [
            {
                "assignmentId": "FA-1",
                "title": "虚构第一次作业",
                "type": "作业",
                "publishedAt": "2026-10-08T09:00:00+08:00",
                "dueAt": "2026-10-20T23:59:00+08:00",
                "url": "https://example.invalid/fictional/course-001/assignments/FA-1",
                "state": "pending",
            },
            {
                "assignmentId": "FA-2",
                "title": "虚构第二次作业（已延期）",
                "type": "作业",
                "publishedAt": "2026-10-09T10:30:00+08:00",
                "dueAt": "2026-11-02T20:00:00+08:00",
                "url": "https://example.invalid/fictional/course-001/assignments/FA-2",
                "state": "closed",
            },
        ],
    },
    {
        "id": "fictional-activity-002",
        "name": "虚构校园活动乙",
        "assignments": [
            {
                "assignmentId": "FB-1",
                "title": "虚构活动报名",
                "type": "活动报名",
                "publishedAt": "2026-10-10T12:00:00+08:00",
                "dueAt": None,
                "url": "https://example.invalid/fictional/activity-002/signup",
                "state": "unknown",
            }
        ],
    },
)

FICTIONAL_GRID_PAYLOAD = (
    {
        "problemSetId": "FP-100",
        "problemSetName": "虚构题集：循环与数组",
        "courseName": "虚构程序设计",
        "deadline": "2026-10-15T22:00:00+08:00",
        "publishTime": "2026-10-07T08:00:00+08:00",
        "problemCount": 6,
    },
    {
        "problemSetId": "FP-101",
        "problemSetName": "虚构题集：递归",
        "courseName": "虚构程序设计",
        "deadline": "2026-10-29T22:00:00+08:00",
        "publishTime": "2026-10-14T08:00:00+08:00",
        "problemCount": 4,
    },
)


class MockTransport:
    """In-memory stand-in for a platform session.

    It stores the raw payload a real adapter would have received and raises the
    configured failure instead of any network call, so tests can cover failure
    handling without touching a platform.
    """

    def __init__(self, payload=(), *, failure=None, incremental_supported=False):
        # Only sequences are normalized. Coercing an arbitrary value here would
        # turn a damaged payload (a dict of keys, a string) into something that
        # silently iterates, hiding the damage the adapter is supposed to report.
        self.payload = tuple(payload) if isinstance(payload, (list, tuple)) else payload
        self.failure = failure
        self.incremental_supported = incremental_supported
        self.calls: list[datetime | None] = []

    def request(self, *, since: datetime | None, source: str):
        self.calls.append(since)
        if self.failure is not None:
            raise self.failure(source)
        if since is not None and not self.incremental_supported:
            # No silent full-history fallback: the caller must ask for a full sync.
            raise SourceUnsupported(f"{source} 的模拟接口不支持按更新时间过滤。")
        return self.payload


def _failure_unavailable(source: str) -> Exception:
    return SourceUnavailable(f"{source} 暂时不可访问（模拟的网络故障）。")


def _failure_auth(source: str) -> Exception:
    return SourceAuthRequired(f"{source} 需要登录（模拟的会话失效）。")


def _failure_rate(source: str) -> Exception:
    return SourceRateLimited(f"{source} 请求过于频繁（模拟的限流）。")


def _parse_moment(value, label: str):
    """Parse one platform timestamp. Unparseable values raise, never default."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceRejected(f"{label}必须是 ISO 8601 文本。")
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        raise SourceRejected(f"{label}格式无法识别，保留为空而不是猜测时间。") from None


def _map_teaching_state(state) -> str | None:
    """Platform wording is mapped explicitly; unknown wording stays None."""
    return {"pending": STATUS_PENDING, "submitted": STATUS_DONE, "closed": STATUS_CANCELLED, "unknown": None}.get(state)


@register_adapter
class MockTeachingPlatformAdapter(SourceAdapter):
    """Teaching-platform-shaped mock: course -> assignments with per-item mapping."""

    source = "teaching_platform"
    display_name = "教学网（模拟适配器）"
    capabilities = SourceCapabilities(
        readable=("course", "task_title", "published_at", "deadline", "source_url", "source_id", "status"),
        incremental=False,
        reports_status=True,
        needs_login=True,
        notes="仅用于本地模拟；真实教学网接入方式见 docs/stage5_feasibility.md，尚未验证。",
    )

    def __init__(self, transport=None) -> None:
        super().__init__()
        self.transport = transport if transport is not None else MockTransport(FICTIONAL_TEACHING_PAYLOAD)

    def _load(self, since: datetime | None) -> FetchResult:
        payload = self.transport.request(since=since, source=self.source)
        if not isinstance(payload, (list, tuple)):
            # Reported as damage instead of being iterated: iterating a dict would
            # yield its key names and quietly look like a list of broken courses.
            raise SourceRejected("课程列表不是数组，无法读取；已拒绝而不是当作没有作业。")
        records, rejections, warnings = [], [], []
        for index, course in enumerate(payload):
            if not isinstance(course, dict):
                # A non-object course entry is damage, not "no tasks": a silent
                # empty result would look identical to a platform with no work.
                rejections.append(Rejection(None, f"第 {index + 1} 个课程条目不是对象，无法读取其作业。"))
                continue
            course_name = course.get("name")
            label = str(course_name).strip() if course_name else f"第 {index + 1} 个课程条目"
            assignments = course.get("assignments")
            if assignments is None:
                # Could be a genuinely empty course or a changed field name. Either
                # way we say we could not convert it instead of staying silent.
                warnings.append(f"{label} 没有 assignments 列表，无法确认是否有作业；请人工核对。")
                continue
            if not isinstance(assignments, list):
                rejections.append(Rejection(None, f"{label} 的 assignments 不是列表，无法读取作业。"))
                continue
            for item in assignments:
                identifier = item.get("assignmentId") if isinstance(item, dict) else None
                try:
                    if not isinstance(item, dict):
                        raise SourceRejected("作业条目不是对象。")
                    records.append(SourceRecord(
                        source=self.source,
                        source_id=str(identifier or ""),
                        task_title=str(item.get("title") or ""),
                        course=str(course_name or ""),
                        task_type=str(item.get("type") or TASK_TYPE_OTHER),
                        published_at=_parse_moment(item.get("publishedAt"), "发布时间"),
                        deadline=_parse_moment(item.get("dueAt"), "截止时间"),
                        source_url=item.get("url"),
                        status=_map_teaching_state(item.get("state")),
                        last_updated=None,
                    ))
                except SourceRejected as error:
                    rejections.append(Rejection(label_identifier(identifier), str(error)))
        return FetchResult(source=self.source, records=tuple(records), rejections=tuple(rejections),
                           since=since, fetched_at=beijing_now(), warnings=tuple(warnings))


class MockProgrammingGridAdapter(SourceAdapter):
    """Synthetic grid shape used for generic contract tests.

    Not registered: ``ProgramingGridJsonAdapter`` below is the authoritative
    adapter for this source because its shape was confirmed on real data. This
    one stays as a deliberately simple payload for exercising the shared
    validation and ordering paths.
    """

    source = "programming_grid"
    display_name = "编程网格（模拟适配器）"
    capabilities = SourceCapabilities(
        readable=("course", "task_title", "published_at", "deadline", "source_id"),
        incremental=False,
        reports_status=False,
        needs_login=True,
        notes="仅用于本地模拟；真实编程网格接入方式见 docs/stage5_feasibility.md，尚未验证。",
    )

    def __init__(self, transport=None) -> None:
        super().__init__()
        self.transport = transport if transport is not None else MockTransport(FICTIONAL_GRID_PAYLOAD)

    def _load(self, since: datetime | None) -> FetchResult:
        payload = self.transport.request(since=since, source=self.source)
        if not isinstance(payload, (list, tuple)):
            raise SourceRejected("题集列表不是数组，无法读取；已拒绝而不是当作没有作业。")
        records, rejections = [], []
        for item in payload:
            identifier = item.get("problemSetId") if isinstance(item, dict) else None
            try:
                if not isinstance(item, dict):
                    raise SourceRejected("题集条目不是对象。")
                name = str(item.get("problemSetName") or "")
                count = item.get("problemCount")
                if not name:
                    raise SourceRejected("题集名称为空，不能编造标题。")
                records.append(SourceRecord(
                    source=self.source,
                    source_id=str(identifier or ""),
                    task_title=name + (f"（共 {count} 题）" if isinstance(count, int) and count > 0 else ""),
                    course=str(item.get("courseName") or ""),
                    task_type="作业",
                    published_at=_parse_moment(item.get("publishTime"), "发布时间"),
                    deadline=_parse_moment(item.get("deadline"), "截止时间"),
                    source_url=None,
                    # The mock payload carries no completion state, and we do not guess one.
                    status=None,
                    last_updated=None,
                ))
            except SourceRejected as error:
                rejections.append(Rejection(label_identifier(identifier), str(error)))
        return FetchResult(source=self.source, records=tuple(records), rejections=tuple(rejections), since=since, fetched_at=beijing_now())


def describe_payload(payload) -> str:
    """Diagnostics without leaking content: shape only, never titles or ids."""
    if isinstance(payload, (list, tuple)):
        return f"list[{len(payload)}]"
    return type(payload).__name__


# ---------------------------------------------------------------------------
# Realistic programming-grid shape, confirmed against a real course page on
# 2026-10-11. The structure (field names, nesting, time string format, note the
# trailing ".0" and the space separator) is real; teacher, course, title and id
# values are replaced with synthetic ones. Times are kept as plausible Beijing
# values because the format is what the parser must handle.
#
# Not reproducible here: /account/query.do, so this payload carries no per-problem
# completion state. See ProgrammingGridAdapter.capabilities.
# ---------------------------------------------------------------------------

GRID_COURSE_JSON = {
    "now": "Sun Oct 11 23:33:57 CST 2026",
    "course": {
        "teacherName": "",
        "problemlists": [
            {"assignment": {"closeTime": "2026-11-13 15:00:00.0", "openTime": "2026-09-30 15:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1", "title": "虚拟题集：期中试题（新）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-11-13 15:00:00.0", "openTime": "2026-09-30 15:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2", "title": "虚拟题集：期中试题（旧）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-11-11 15:00:00.0", "openTime": "2026-09-30 15:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa3", "title": "虚拟第二阶段练习（6）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-11-06 15:00:00.0", "openTime": "2026-09-30 15:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa4", "title": "虚拟第二阶段练习（5）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-11-04 15:00:00.0", "openTime": "2026-09-30 15:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa5", "title": "虚拟第二阶段练习（4）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-10-30 15:00:00.0", "openTime": "2026-09-30 15:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa6", "title": "虚拟第二阶段练习（3）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-10-28 15:00:00.0", "openTime": "2026-09-30 15:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa7", "title": "虚拟第二阶段练习（2）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-10-23 12:00:00.0", "openTime": "2026-09-30 12:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa8", "title": "虚拟第二阶段练习（1）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-10-14 12:00:00.0", "openTime": "2026-09-30 12:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa9", "title": "虚拟第一阶段练习（6）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-10-14 12:00:00.0", "openTime": "2026-09-30 12:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa10", "title": "虚拟第一阶段练习（5）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-10-07 17:00:00.0", "openTime": "2026-09-23 17:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa11", "title": "虚拟第一阶段练习（4）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-10-07 17:00:00.0", "openTime": "2026-09-23 17:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa12", "title": "虚拟第一阶段练习（3）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-09-27 14:00:00.0", "openTime": "2026-09-17 14:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa13", "title": "虚拟第一阶段练习（2）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-09-27 14:00:00.0", "openTime": "2026-09-17 14:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa14", "title": "虚拟第一阶段练习（1）", "type": "Homework"},
            {"assignment": {"closeTime": "2026-09-16 15:00:00.0", "openTime": "2026-09-09 15:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa15", "title": "虚拟抄写练习（选做，不计入成绩）", "type": "Homework"},
            {"assignment": {"closeTime": "2027-01-07 15:00:00.0", "openTime": "2026-09-09 15:00:00.0"},
             "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa16", "title": "虚拟试手练习（选做，不计入成绩）", "type": "Homework"},
        ],
        "closeTime": 1788969599000,
        "id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "title": "虚拟课程 2026 虚拟老师",
        "openTime": 1788710400000,
    },
    "type": "course",
    "status": "OK",
}

# "2026-11-13 15:00:00.0": space separator, trailing tenths, no timezone offset.
_GRID_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?$")


def parse_grid_time(value, label: str) -> datetime:
    """Parse the grid's own time string, which is *not* ISO 8601.

    The platform sends "2026-11-13 15:00:00.0": a space separator, a trailing
    fractional part and no offset. The trailing part is only ever ".0" in every
    observed row, but a nonzero value would silently move a deadline, so any
    non-zero fraction is refused instead of rounded away.
    """
    if not isinstance(value, str):
        raise SourceRejected(f"{label}必须是文本时间，收到 {type(value).__name__}。")
    match = _GRID_TIME.match(value.strip())
    if match is None:
        raise SourceRejected(f"{label}格式无法识别（{value!r}），保留为空而不是猜测时间。")
    fraction = value.strip().split(".")[1] if "." in value.strip() else "0"
    if fraction.strip("0"):
        raise SourceRejected(f"{label}含非零小数秒（{value!r}），无法确定精确时刻，已拒绝。")
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    # Times from this platform are Beijing time by definition and carry no offset.
    return datetime(year, month, day, hour, minute, second, tzinfo=BEIJING)


@register_adapter
class ProgrammingGridJsonAdapter(SourceAdapter):
    """Reads one grid course page's JSON, confirmed against a real response.

    Field mapping, all verified on real data (2026-10-11):

    - ``course.title`` -> course (the course name, includes teacher and year)
    - ``course.problemlists[].title`` -> task_title
    - ``course.problemlists[].type`` -> task_type ("Homework")
    - ``course.problemlists[].assignment.openTime`` -> published_at
    - ``course.problemlists[].assignment.closeTime`` -> deadline
    - ``course.problemlists[].id`` -> source_id (a 32-hex server-side id)

    Deliberately not mapped:

    - ``course.openTime`` / ``course.closeTime`` are Unix **milliseconds** for the
      whole course window, not a task deadline. Mixing them with the task-level
      strings would be a unit error waiting to happen, so they are ignored here.
    - completion state is absent from this response, so ``status`` is always None.
    """

    source = "programming_grid"
    display_name = "编程网格课程页 JSON（模拟适配器）"
    capabilities = SourceCapabilities(
        readable=("course", "task_title", "task_type", "published_at", "deadline", "source_id"),
        incremental=False,
        reports_status=False,
        needs_login=True,
        notes="结构按 2026-10-11 的真实课程页 JSON 编写，内容为虚构；本模块不联网。完成状态需另查，见 docs/stage5_feasibility.md。",
    )

    def __init__(self, transport=None) -> None:
        super().__init__()
        self.transport = transport if transport is not None else MockTransport(GRID_COURSE_JSON)

    def _load(self, since: datetime | None) -> FetchResult:
        payload = self.transport.request(since=since, source=self.source)
        if not isinstance(payload, dict):
            raise SourceRejected("课程页响应不是对象，无法读取；已拒绝而不是当作没有作业。")
        status = payload.get("status")
        if status != "OK":
            # A non-OK envelope means the platform declined; treating it as an
            # empty course would hide an expired session or a permissions change.
            raise SourceAuthRequired(f"编程网格返回 status={status!r}，可能需要重新登录；未当作空课程处理。")
        course = payload.get("course")
        if not isinstance(course, dict):
            raise SourceRejected("响应缺少 course 对象，无法读取题集；已拒绝而不是当作没有作业。")
        course_title = str(course.get("title") or "").strip()
        lists = course.get("problemlists")
        if lists is None:
            raise SourceRejected("course 里没有 problemlists 字段，无法确认题集；已拒绝。")
        if not isinstance(lists, (list, tuple)):
            raise SourceRejected("problemlists 不是数组，无法读取题集。")
        records, rejections, warnings = [], [], []
        for index, item in enumerate(lists):
            identifier = item.get("id") if isinstance(item, dict) else None
            label = f"第 {index + 1} 个题集"
            try:
                if not isinstance(item, dict):
                    raise SourceRejected("题集条目不是对象。")
                title = str(item.get("title") or "").strip()
                if not title:
                    raise SourceRejected("题集标题为空，不能编造标题。")
                assignment = item.get("assignment")
                if not isinstance(assignment, dict):
                    # Real payloads pair every set with an assignment object; a
                    # missing one means an unknown deadline, not a missing task.
                    raise SourceRejected("题集缺少 assignment 对象，无法确定截止时间。")
                records.append(SourceRecord(
                    source=self.source,
                    source_id=str(identifier or ""),
                    task_title=title,
                    course=course_title,
                    task_type=str(item.get("type") or TASK_TYPE_OTHER),
                    published_at=parse_grid_time(assignment.get("openTime"), "开放时间") if assignment.get("openTime") else None,
                    deadline=parse_grid_time(assignment.get("closeTime"), "截止时间") if assignment.get("closeTime") else None,
                    source_url=None,
                    status=None,
                    last_updated=None,
                ))
            except SourceRejected as error:
                rejections.append(Rejection(label_identifier(identifier) or label, str(error)))
        if not records and not rejections and not warnings:
            # "status OK" with an empty array is the only confirmed-empty case.
            warnings.append(f"{course_title or '课程'} 返回了空题集列表；请确认这门课确实没有题集。")
        return FetchResult(source=self.source, records=tuple(records), rejections=tuple(rejections),
                           since=since, fetched_at=beijing_now(), warnings=tuple(warnings))
