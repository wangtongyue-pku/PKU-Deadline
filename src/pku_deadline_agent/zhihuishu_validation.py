"""Move ONE authorized read's display fields out of the browser process.

A real read happens in the same process as the browser window; the review page
runs in another. This module is that narrow bridge, and it is deliberately a
whitelist rather than a serializer:

* may leave the browser — the fields the user asked to review (title, course,
  deadline, date text, status, kind), the coverage counters, the course context
  and the provenance marker;
* may NOT — observed platform IDs, source links, cookies, tokens, raw page text
  or scroll fragments. `encode_report` never writes them and `decode_report`
  refuses payloads that carry anything outside the whitelist.

Nothing here writes a database, talks to a model or touches the network.
"""

from dataclasses import dataclass, fields
from datetime import datetime
import json
import os
from pathlib import Path

from pku_deadline_agent.zhihuishu_reader import ReadTask
from pku_deadline_agent.zhihuishu_review import ORIGIN_LABELS
from pku_deadline_agent.zhihuishu_scroll import ScrollCoverage, ScrollReport

PAYLOAD_VERSION = 1
# Where the last authorized read is kept. Local, gitignored, and overridable so
# tests and alternative instances never touch the daily one.
PAYLOAD_ENV = 'PKU_ZHIHUISHU_PAYLOAD'
DEFAULT_PAYLOAD = (Path(__file__).resolve().parents[2]
                   / '.local-tmp' / 'zhs-validation' / 'report.json')


def payload_path() -> Path:
    override = os.environ.get(PAYLOAD_ENV, '').strip()
    current = DEFAULT_PAYLOAD.parent.parent / 'zhs-current.json'
    return Path(override) if override else (current if current.exists() else DEFAULT_PAYLOAD)


def load_latest(path=None) -> ScrollReport | None:
    """The most recent authorized read, or None when there is none usable.

    Never raises into a page: a missing, moved or hand-edited payload simply means
    "no real read to show", which falls back to the fictional samples.
    """
    source = Path(path) if path is not None else payload_path()
    try:
        if not source.exists():
            return None
        return decode_report(json.loads(source.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return None

TASK_FIELDS = ('title', 'course', 'deadline', 'deadline_date', 'status', 'kind')
COVERAGE_FIELDS = tuple(f.name for f in fields(ScrollCoverage))
REPORT_FIELDS = ('version', 'evidence', 'course', 'coverage', 'tasks')


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _moment(value, where):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{where} 必须是 ISO 8601 字符串或空值。')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f'{where} 缺少时区，拒绝解释为北京时间。')
    return parsed


def encode_report(report: ScrollReport) -> dict:
    """Whitelisted payload for one read. IDs, links and fragments are dropped."""
    payload = {
        'version': PAYLOAD_VERSION,
        'evidence': report.evidence,
        'course': report.course,
        'coverage': {name: getattr(report.coverage, name) for name in COVERAGE_FIELDS},
        'tasks': [{name: (_iso(getattr(task, name)) if name == 'deadline' else getattr(task, name))
                   for name in TASK_FIELDS} for task in report.tasks],
    }
    return json.loads(json.dumps(payload, ensure_ascii=False))


def decode_report(payload) -> ScrollReport:
    """Rebuild the review shape, refusing anything outside the whitelist."""
    if not isinstance(payload, dict):
        raise ValueError('读取结果必须是对象。')
    extra = set(payload) - set(REPORT_FIELDS)
    if extra:
        raise ValueError(f'读取结果含未允许的字段：{sorted(extra)}')
    if payload.get('version') != PAYLOAD_VERSION:
        raise ValueError('读取结果版本不匹配，请重新读取。')
    evidence = payload.get('evidence')
    if evidence not in ORIGIN_LABELS:
        raise ValueError('读取结果缺少可辨认的来源标记。')
    coverage_raw = payload.get('coverage')
    if not isinstance(coverage_raw, dict):
        raise ValueError('读取结果的覆盖度字段无效。')
    unknown = set(coverage_raw) - set(COVERAGE_FIELDS)
    if unknown:
        raise ValueError(f'读取结果的覆盖度含未允许字段：{sorted(unknown)}')
    # issue_counts arrived after the first payloads; absent means "not recorded".
    missing = set(COVERAGE_FIELDS) - set(coverage_raw) - {'issue_counts'}
    if missing:
        raise ValueError(f'读取结果的覆盖度字段不完整：{sorted(missing)}')
    reasons = coverage_raw['reasons']
    if not isinstance(reasons, (list, tuple)) or not all(isinstance(r, str) for r in reasons):
        raise ValueError('读取结果的完整性原因无效。')
    issue_counts = coverage_raw.get('issue_counts', [])
    if not isinstance(issue_counts, (list, tuple)) or not all(
            isinstance(pair, (list, tuple)) and len(pair) == 2
            and isinstance(pair[0], str) and isinstance(pair[1], int)
            for pair in issue_counts):
        raise ValueError('读取结果的异常计数无效。')
    coverage = ScrollCoverage(**(coverage_raw | {'reasons': tuple(reasons),
                                                 'issue_counts': tuple(tuple(p) for p in issue_counts)}))
    tasks_raw = payload.get('tasks')
    if not isinstance(tasks_raw, list):
        raise ValueError('读取结果的任务列表无效。')
    tasks = []
    for index, item in enumerate(tasks_raw, 1):
        if not isinstance(item, dict) or set(item) != set(TASK_FIELDS):
            raise ValueError(f'第 {index} 项字段不完整或含未允许字段。')
        tasks.append(ReadTask(
            title=item['title'], course=item['course'],
            deadline=_moment(item['deadline'], f'第 {index} 项截止时间'),
            deadline_date=item['deadline_date'], status=item['status'], kind=item['kind']))
    course = payload.get('course')
    if course is not None and not isinstance(course, str):
        raise ValueError('课程上下文无效。')
    return ScrollReport(tuple(tasks), coverage, course, evidence)


@dataclass(frozen=True)
class ReadSummary:
    """Structure and counts only: safe to print, log or hand to the user."""

    evidence: str
    task_count: int
    course_bound: bool
    titles_present: int
    deadlines_present: int
    statuses_present: int
    coverage_complete: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {'evidence': self.evidence, 'task_count': self.task_count,
                'course_bound': self.course_bound, 'titles_present': self.titles_present,
                'deadlines_present': self.deadlines_present,
                'statuses_present': self.statuses_present,
                'coverage_complete': self.coverage_complete,
                'reasons': list(self.reasons)}


def summarize(report: ScrollReport) -> ReadSummary:
    """Counts and booleans only — never a real title, course or date."""
    return ReadSummary(
        evidence=report.evidence,
        task_count=len(report.tasks),
        course_bound=bool(report.course),
        titles_present=sum(bool(t.title.strip()) for t in report.tasks),
        deadlines_present=sum(t.deadline is not None for t in report.tasks),
        statuses_present=sum(t.status in ('未完成', '已完成') for t in report.tasks),
        coverage_complete=report.coverage.complete,
        reasons=tuple(report.coverage.reasons))
