"""Offline Zhihuishu contract prototype; NOT a platform response parser.

The document format below is ours, not a discovered API or official export.
No browser, transport, credentials, AI or database writes exist in this module.
Keep richer time semantics alongside SourceRecord rather than dropping them in
the base adapter's normalization. Real acquisition and persistence await approval.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import unquote, urlsplit

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.models import SOURCES
from pku_deadline_agent.privacy import scan_privacy
from pku_deadline_agent.sources import (
    FetchResult, Rejection, SourceAdapter, SourceCapabilities, SourceRecord,
    SourceRejected, SourceUnsupported, normalize_optional_datetime, normalize_text, validate_record,
)
from pku_deadline_agent.sync import plan_records

SOURCE = "zhihuishu"
DOCUMENT_FORMAT = "pku-deadline.zhihuishu.v1"
# These distinguish meanings even when two times happen to be equal.
KINDS = {
    "course_end": "课程结束",
    "study_deadline": "课程学习截止",
    "video": "视频学习截止",
    "chapter_test": "章节测试截止",
    "homework": "作业截止",
    "exam": "考试截止",
}
MAX_COURSES = 20
MAX_TASKS = 1000
MAX_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
_ROOT_FIELDS = {"format", "courses"}
_COURSE_FIELDS = {"course_id", "enrollment_id", "name", "tasks"}
_TASK_FIELDS = {"id", "kind", "title", "deadline", "opens_at", "status",
                "source_url", "requirements", "published_at", "last_updated"}


@dataclass(frozen=True)
class ZhihuishuEntry:
    record: SourceRecord = field(repr=False)
    kind: str
    opens_at: datetime | None = None
    deadline_text: str | None = None
    opens_at_text: str | None = None
    requirements: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ZhihuishuSnapshot:
    result: FetchResult
    entries: tuple[ZhihuishuEntry, ...] = field(repr=False)


def _object(value, allowed):
    if not isinstance(value, dict) or set(value) - allowed:
        # Unknown fields can include credentials. Do not echo keys or values.
        raise SourceRejected("本地规范对象结构无效或包含非白名单字段。")
    return value


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SourceRejected("缺少有效的稳定来源标识；不以标题或行号替代。")
    return value


def _text(value, limit, label):
    text = normalize_text(value, limit=limit, label=label)
    if scan_privacy(text).blocked:
        raise SourceRejected("文本含认证资料或无法确认的敏感内容，请仅保留必要任务信息。")
    return text


def _time(value, label):
    # A date-only value remains visible but is never silently made midnight.
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            date.fromisoformat(value)
        except ValueError:
            raise SourceRejected(f"{label}日期无效。") from None
        return None, value
    return normalize_optional_datetime(value, label), None


def _link(value):
    text = normalize_text(value, limit=2000, label="来源链接")
    if not text:
        return None
    try:
        parts = urlsplit(text)
        valid = (parts.scheme == "https" and parts.hostname and not parts.username
                 and not parts.password and parts.port in (None, 443))
    except ValueError:
        valid = False
    # Scanning the full path as an opaque secret also rejects ordinary long
    # URLs. Retain credential-marker checks everywhere and opaque-value checks
    # on decoded query/fragment, where session material is often carried.
    marker_risk = any(f.blocks_sending and f.kind != "不明长串（需本地核查）"
                      for f in scan_privacy(unquote(text)).findings)
    parameter_risk = valid and scan_privacy(unquote(parts.query + " " + parts.fragment)).blocked
    if not valid or marker_risk or parameter_risk:
        raise SourceRejected("来源链接无效或可能含认证信息，请提供不含凭据的 HTTPS 链接。")
    # It is stored as data only. This function never navigates to it.
    return text


class ZhihuishuAdapter(SourceAdapter):
    """Read only an in-memory normalized document, using fictional data in tests.

    Deliberately not registered as an available production source. No network
    transport or credential argument can be supplied, and no real API is claimed.
    """

    source = SOURCE
    display_name = "智慧树（离线契约原型）"
    capabilities = SourceCapabilities(
        readable=tuple(KINDS), reports_status=True, needs_login=False,
        notes="仅本地规范样例；状态能力只针对该规范，真实平台字段与能力未经验证。",
    )

    def __init__(self, document):
        super().__init__()
        self._document = document
        self._entries: tuple[ZhihuishuEntry, ...] = ()

    def snapshot(self) -> ZhihuishuSnapshot:
        result = self.fetch()
        by_id = {entry.record.source_id: entry for entry in self._entries}
        return ZhihuishuSnapshot(result, tuple(by_id[r.source_id] for r in result.records))

    def _load(self, since):
        self._entries = ()
        root = _object(self._document, _ROOT_FIELDS)
        if root.get("format") != DOCUMENT_FORMAT:
            raise SourceRejected("不是本项目的智慧树本地规范；不猜测真实平台响应结构。")
        courses = root.get("courses")
        if not isinstance(courses, list) or len(courses) > MAX_COURSES:
            raise SourceRejected("课程必须是数组且不超过 20 门；缺失不等于没有课程。")
        entries, rejections, warnings = [], [], []
        seen_courses, seen_tasks = set(), set()
        total = 0
        for course_number, raw_course in enumerate(courses, 1):
            label = f"课程 {course_number}"
            try:
                course = _object(raw_course, _COURSE_FIELDS)
                course_id = _identifier(course.get("course_id"))
                enrollment = _identifier(course.get("enrollment_id"))
                scope = (course_id, enrollment)
                if scope in seen_courses:
                    raise SourceRejected("同批出现重复的课程修读标识。")
                seen_courses.add(scope)
                course_name = _text(course.get("name"), 100, "课程名称")
                if "tasks" not in course:
                    warnings.append(f"{label}未提供任务列表，无法确认是否有任务。")
                    continue
                tasks = course["tasks"]
                if not isinstance(tasks, list):
                    raise SourceRejected("任务列表损坏，不等于没有任务。")
                total += len(tasks)
                if total > MAX_TASKS:
                    raise SourceRejected("任务总数超过 1000 项，已中止。")
            except SourceRejected as error:
                rejections.append(Rejection(label, str(error)))
                if total > MAX_TASKS:
                    break
                continue
            for task_number, task in enumerate(tasks, 1):
                task_label = f"{label}任务 {task_number}"
                try:
                    entry = self._entry(task, course_name, course_id, enrollment)
                    key = entry.record.source_id
                    if key in seen_tasks:
                        raise SourceRejected("同批出现重复的来源任务标识，不能合并或导入。")
                    seen_tasks.add(key)
                    entries.append(entry)
                except SourceRejected as error:
                    rejections.append(Rejection(task_label, str(error)))
        self._entries = tuple(entries)
        return FetchResult(SOURCE, records=tuple(e.record for e in entries),
                           rejections=tuple(rejections), warnings=tuple(warnings))

    def _entry(self, raw, course_name, course_id, enrollment):
        task = _object(raw, _TASK_FIELDS)
        object_id = _identifier(task.get("id"))
        kind = task.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            raise SourceRejected("任务的时间语义缺失或未知，不能猜成作业截止。")
        title = _text(task.get("title"), 200, "任务标题")
        deadline, deadline_text = _time(task.get("deadline"), "截止时间")
        opens_at, opens_at_text = _time(task.get("opens_at"), "开放时间")
        if deadline and opens_at and opens_at > deadline:
            raise SourceRejected("开放时间晚于截止时间，请核对来源数据。")
        # Scope identity to enrollment/term, course AND kind; dates/titles are not identity.
        identity = json.dumps([enrollment, course_id, kind, object_id], separators=(",", ":"))
        source_id = "zhs." + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        record = validate_record(SourceRecord(
            source=SOURCE, source_id=source_id, task_title=title, course=course_name,
            task_type=kind, deadline=deadline, status=task.get("status"),
            source_url=_link(task.get("source_url")),
            published_at=task.get("published_at"), last_updated=task.get("last_updated"),
        ), source=SOURCE)
        return ZhihuishuEntry(record, kind, opens_at, deadline_text, opens_at_text,
                              _text(task.get("requirements"), 2000, "学习要求") or None)


def decode_zhihuishu_document(raw: bytes) -> dict:
    """Bounded local format decoding, NOT an official platform export reader."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise SourceRejected("本地内容必须为非空字节且不超过 2 MB。")
    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise SourceRejected("JSON 含重复字段，不能用后一个覆盖前一个。")
            obj[key] = value
        return obj
    try:
        result = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise SourceRejected("本地内容不是可读取的 UTF-8 JSON。") from None
    if not isinstance(result, dict):
        raise SourceRejected("本地 JSON 顶层必须是对象。")
    return result


def plan_zhihuishu(snapshot: ZhihuishuSnapshot, repository):
    """Preview-only reuse of the shared planner. No save path in this stage.

    Schema v4 cannot persist source=zhihuishu or richer metadata. Never route
    this plan to apply_sync_plan until the reviewed migration is implemented.
    """
    snapshot.result.require_trustworthy()
    if isinstance(repository, TaskRepository) and SOURCE not in SOURCES:
        raise SourceUnsupported("当前 SQLite schema 尚未接入智慧树来源；此阶段只支持离线仓储替身预演。")
    entries = {entry.record.source_id: entry for entry in snapshot.entries}
    # Course end is context, not a substitute deadline or an actionable task.
    records = tuple(r for r in snapshot.result.records if entries[r.source_id].kind != "course_end")
    return plan_records(
        records, snapshot.result, repository, source=SOURCE,
        new_reason="智慧树新增任务（仅离线预览）", changed_reason="智慧树截止变更待核对",
        unchanged_reason="来源标识与截止时间一致",
        duplicate_reason="可能与任务 #{task_id} 重复：{detail}，需用户核对",
        notes=("智慧树离线契约预览；尚未提供写库入口",),
        task_type_mapper=lambda kind: ({"homework": "作业", "chapter_test": "考试", "exam": "考试"}.get(kind, "其他"), None),
    )
