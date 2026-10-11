"""Explainable local matching. Hash necessary sanitized fragments, never raw chat."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher

from pku_deadline_agent.models import Task
from pku_deadline_agent.time_utils import normalize_datetime


def identity(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    numbers = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
    text = re.sub(r"第([一二三四五六七八九十])(?=[次章节题])", lambda m: "第" + numbers[m[1]], text)
    return re.sub(r"[\W_]", "", text)


@dataclass(frozen=True)
class NoticeOrigin:
    source: str = "wechat"
    notice_hash: str = ""
    published_at: datetime | None = None


def origin_for_notice(notice) -> NoticeOrigin:
    text = notice.sanitized_text if notice.sanitized_text is not None else "\n".join((notice.title, notice.category, notice.task_type, notice.deadline_text or ""))
    # Keep all meaningful text in the digest, including dates; don't store the text.
    digest = hashlib.sha256(unicodedata.normalize("NFKC", re.sub(r"\s+", "", text)).encode("utf-8")).hexdigest()
    return NoticeOrigin("wechat", digest, notice.published_at)


@dataclass(frozen=True)
class ImportDecision:
    action: str  # new, link, update, skip
    target_id: int | None = None
    expected_revision: int | None = None
    reason: str = "用户审核通知后确认"
    allow_distinct: bool = False


@dataclass(frozen=True)
class MatchSuggestion:
    task: Task
    kind: str
    reason: str


def find_matches(candidate, tasks: list[Task], hashes: dict[int, set[str]], notice_hash: str = "") -> tuple[MatchSuggestion, ...]:
    title, category = identity(candidate.title), identity(candidate.category)
    generic = title in {"待确认任务", "待解析通知", "作业", "任务", "活动", "报名"}
    matches = []
    ordinal = re.search(r"第(\d+)(?:次|章|节|题)", title)
    for task in tasks:
        other, course = identity(task.title), identity(task.category)
        other_ordinal = re.search(r"第(\d+)(?:次|章|节|题)", other)
        if ordinal and other_ordinal and ordinal[1] != other_ordinal[1]:
            continue
        same_course = category == course
        same_type = candidate.task_type == task.task_type
        same_notice = bool(notice_hash and notice_hash in hashes.get(task.id, set()))
        similarity = SequenceMatcher(None, title.replace(category, "") if category else title, other.replace(course, "") if course else other).ratio()
        if title == other and same_course and same_type and not generic:
            same_due = candidate.due_at == task.due_at
            matches.append(MatchSuggestion(task, "duplicate" if same_due else "deadline_change", "课程、任务名称和类型一致" + ("；已关联相同通知" if same_notice else "") + ("；截止时间相同" if same_due else "；截止时间不同，需确认更新")))
        elif ((title == other and (same_course or not category or not course))
              or (same_course and bool(category) and same_type and (similarity >= .72 or candidate.deadline_changed))
              or (same_notice and same_type and similarity >= .5)):
            matches.append(MatchSuggestion(task, "possible", "名称相近、来源相同或同课程改期；不能确定是否同一任务，请选择"))
    precise = [match for match in matches if match.kind != "possible"]
    return tuple(sorted(precise or matches, key=lambda item: item.task.id))


def urgency(task: Task, now: datetime) -> str:
    if task.deleted_at:
        return "已删除"
    if task.completed:
        return "已完成"
    if task.due_at is None:
        return "待确认"
    seconds = (task.due_at - normalize_datetime(now)).total_seconds()
    if seconds <= 0:
        return "已过期"
    if seconds <= 3 * 3600:
        return "3小时内"
    if seconds <= 86400:
        return "1天内"
    if seconds <= 3 * 86400:
        return "3天内"
    return "待办"


def filter_tasks(tasks, *, category="全部", task_type="全部", status="未完成", now: datetime):
    selected = [task for task in tasks
                if (category == "全部" or task.category == category)
                and (task_type == "全部" or task.task_type == task_type)
                and (status == "全部" or status == "已完成" and task.completed
                     or status == "未完成" and not task.completed
                     or status == "已过期" and urgency(task, now) == "已过期")]
    return sorted(selected, key=lambda task: (task.due_at is None, task.due_at or normalize_datetime(now), task.id))
