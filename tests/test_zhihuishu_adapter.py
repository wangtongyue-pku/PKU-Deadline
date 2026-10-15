"""Fictional normalized contract only. No platform, browser, API or user data."""

import ast
import copy
from datetime import datetime
from pathlib import Path

import pytest

from pku_deadline_agent import zhihuishu_adapter as zhs
from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.models import Task
from pku_deadline_agent.sources import (
    SourceRejected, SourceUnsupported, SourceUnverified, external_key, notice_key,
)
from pku_deadline_agent.sync import DEADLINE_CHANGED, NEW, UNCHANGED
from pku_deadline_agent.time_utils import BEIJING


@pytest.fixture
def document():
    path = Path(__file__).parent / "fixtures" / "zhihuishu_normalized.json"
    return zhs.decode_zhihuishu_document(path.read_bytes())


def task(document):
    return document["courses"][0]["tasks"][2]


def entry_for(snapshot, kind, course="虚构通识课甲"):
    return next(e for e in snapshot.entries if e.kind == kind and e.record.course == course)


@pytest.fixture
def repository(tmp_path):
    repo = TaskRepository(tmp_path / "fictional.sqlite3")
    repo.initialize()
    return repo


def test_multiple_courses_keep_six_distinct_semantics_and_missing_values(document):
    snapshot = zhs.ZhihuishuAdapter(document).snapshot()
    assert snapshot.result.trustworthy
    assert len(snapshot.entries) == 8
    assert {e.kind for e in snapshot.entries} == set(zhs.KINDS)
    assert len({r.course for r in snapshot.result.records}) == 2
    assert entry_for(snapshot, "exam").opens_at == datetime(2026, 12, 16, 8, tzinfo=BEIJING)
    assert entry_for(snapshot, "exam").record.deadline == datetime(2026, 12, 20, 20, tzinfo=BEIJING)
    assert entry_for(snapshot, "exam").record.status is None
    assert entry_for(snapshot, "chapter_test").record.status == "已完成"
    assert entry_for(snapshot, "video").record.status == "未完成"
    assert entry_for(snapshot, "study_deadline").requirements
    assert entry_for(snapshot, "video", "虚构通识课乙").record.deadline is None
    assert entry_for(snapshot, "video", "虚构通识课乙").deadline_text == "2026-11-15"
    assert entry_for(snapshot, "homework", "虚构通识课乙").record.deadline is None
    assert entry_for(snapshot, "video").record.source_url == "https://example.invalid/course-aaaa/videos"


def test_identity_ignores_title_deadline_and_sort_order(document):
    before = zhs.ZhihuishuAdapter(document).snapshot()
    changed = copy.deepcopy(document)
    task(changed).update(title="虚构修改后的标题", deadline="2026-11-13T20:00:00+08:00")
    changed["courses"].reverse()
    changed["courses"][1]["tasks"].reverse()
    after = zhs.ZhihuishuAdapter(changed).snapshot()
    assert entry_for(before, "video").record.source_id == entry_for(after, "video").record.source_id


@pytest.mark.parametrize("field", ["course_id", "enrollment_id"])
def test_identity_scoped_to_course_and_term(document, field):
    before = entry_for(zhs.ZhihuishuAdapter(document).snapshot(), "video")
    document["courses"][0][field] = "fictional-distinct-bbbb"
    after = entry_for(zhs.ZhihuishuAdapter(document).snapshot(), "video")
    assert before.record.source_id != after.record.source_id


def test_same_id_different_task_kind_does_not_merge(document):
    before = entry_for(zhs.ZhihuishuAdapter(document).snapshot(), "video")
    task(document)["kind"] = "homework"
    after = next(e for e in zhs.ZhihuishuAdapter(document).snapshot().entries
                 if e.record.task_title == "虚构第一章视频" and e.record.course == "虚构通识课甲")
    assert before.record.source_id != after.record.source_id


@pytest.mark.parametrize("bad", [None, "not-an-object", [], {"format": "unknown", "courses": []},
                                 {"format": zhs.DOCUMENT_FORMAT},
                                 {"format": zhs.DOCUMENT_FORMAT, "courses": {}}])
def test_broken_root_is_never_confirmed_empty(bad):
    with pytest.raises(SourceRejected):
        zhs.ZhihuishuAdapter(bad).fetch()


@pytest.mark.parametrize("bad", [None, "bad", [], {"course_id": "fictional-course-cccc"}])
def test_bad_course_keeps_valid_rows_but_prevents_plan(document, repository, bad):
    document["courses"].append(bad)
    snapshot = zhs.ZhihuishuAdapter(document).snapshot()
    assert len(snapshot.result.records) == 8
    assert snapshot.result.rejections and not snapshot.result.trustworthy
    assert not snapshot.result.confirmed_empty
    with pytest.raises(SourceUnverified):
        zhs.plan_zhihuishu(snapshot, repository)
    assert not repository.list_tasks()


def test_missing_tasks_is_warning_explicit_empty_is_empty(document):
    document["courses"] = [document["courses"][0]]
    del document["courses"][0]["tasks"]
    result = zhs.ZhihuishuAdapter(document).fetch()
    assert result.complete and result.warnings and not result.trustworthy
    assert not result.confirmed_empty
    document["courses"][0]["tasks"] = []
    assert zhs.ZhihuishuAdapter(document).fetch().confirmed_empty


@pytest.mark.parametrize("bad", [None, {}, "", 1])
def test_tasks_must_be_array(document, bad):
    document["courses"][0]["tasks"] = bad
    result = zhs.ZhihuishuAdapter(document).fetch()
    assert result.rejections and not result.trustworthy


@pytest.mark.parametrize("mutate", [
    lambda t: t.pop("id"),
    lambda t: t.update(id=""),
    lambda t: t.update(id=True),
    lambda t: t.update(kind="unknown-kind"),
    lambda t: t.update(kind=[]),
    lambda t: t.update(title=""),
    lambda t: t.update(deadline="2026-11-20T20:00:00"),
    lambda t: t.update(deadline="2026-02-30"),
    lambda t: t.update(deadline="下周五"),
    lambda t: t.update(opens_at="2027-01-01T20:00:00+08:00"),
    lambda t: t.update(published_at="2026-11-01"),
    lambda t: t.update(status=True),
])
def test_invalid_task_is_explicit_rejection(document, mutate):
    mutate(task(document))
    result = zhs.ZhihuishuAdapter(document).fetch()
    assert len(result.records) == 7
    assert len(result.rejections) == 1
    assert not result.trustworthy


@pytest.mark.parametrize("status", [None, "未知", "已上交", "已过期", "100%", "已自动交卷"])
def test_status_is_not_inferred_from_submission_progress_or_expiry(document, status):
    task(document)["status"] = status
    assert entry_for(zhs.ZhihuishuAdapter(document).snapshot(), "video").record.status is None


def test_duplicate_identity_stops_whole_preview(document, repository):
    document["courses"][0]["tasks"].append(copy.deepcopy(task(document)))
    snapshot = zhs.ZhihuishuAdapter(document).snapshot()
    assert snapshot.result.rejections and not snapshot.result.trustworthy
    with pytest.raises(SourceUnverified):
        zhs.plan_zhihuishu(snapshot, repository)


def test_duplicate_course_blocks_preview(document):
    document["courses"].append(copy.deepcopy(document["courses"][0]))
    assert not zhs.ZhihuishuAdapter(document).fetch().trustworthy


def test_times_normalized_to_beijing_past_dates_not_advanced(document):
    task(document)["deadline"] = "2025-12-31T18:00:00Z"
    record = entry_for(zhs.ZhihuishuAdapter(document).snapshot(), "video").record
    assert record.deadline == datetime(2026, 1, 1, 2, tzinfo=BEIJING)


def test_no_incremental_or_credential_arguments(document):
    adapter = zhs.ZhihuishuAdapter(document)
    with pytest.raises(SourceUnsupported):
        adapter.fetch(since=datetime(2026, 10, 15, tzinfo=BEIJING))
    with pytest.raises(TypeError):
        zhs.ZhihuishuAdapter(document, password="fictional-only")


@pytest.mark.parametrize("field", ["Cookie", "token", "password", "headers"])
def test_credential_fields_refused_without_echo(document, field):
    secret = "fictional-sensitive-value"
    task(document)[field] = secret
    result = zhs.ZhihuishuAdapter(document).fetch()
    assert result.rejections and secret not in repr(result)
    assert field not in repr(result.rejections)


@pytest.mark.parametrize("value", ["Cookie: fictional-cookie-value", "Token: fictional-token-value"])
def test_credentials_in_description_not_retained(document, value):
    task(document)["requirements"] = value
    result = zhs.ZhihuishuAdapter(document).fetch()
    assert result.rejections and value not in repr(result)


@pytest.mark.parametrize("url", ["http://example.invalid/tasks", "javascript:alert(1)",
                                 "https://user:fictional-password@example.invalid/tasks",
                                 "https://example.invalid/tasks?token=fictional-only"])
def test_links_never_accept_credentials_or_active_schemes(document, url):
    task(document)["source_url"] = url
    result = zhs.ZhihuishuAdapter(document).fetch()
    assert result.rejections and url not in repr(result)


@pytest.mark.parametrize("raw", [b"", b"[]", b"null", b"broken", b"\xff\xfe",
                                 b'{"courses":[],"courses":[]}', b"0" * (zhs.MAX_BYTES + 1)],
                         ids=["empty", "array", "null", "invalid-json", "invalid-encoding", "duplicate-key", "over-limit"])
def test_bounded_local_json_errors_do_not_echo_input(raw):
    with pytest.raises(SourceRejected):
        zhs.decode_zhihuishu_document(raw)


def test_limits_are_explicit(document):
    document["courses"] *= 11
    with pytest.raises(SourceRejected):
        zhs.ZhihuishuAdapter(document).fetch()


def test_task_limit_is_not_silent(document):
    document["courses"][0]["tasks"] = [task(document)] * (zhs.MAX_TASKS + 1)
    result = zhs.ZhihuishuAdapter(document).fetch()
    assert not result.trustworthy and result.rejections


def test_shared_planner_only_previews_never_writes_sqlite(document, repository):
    snapshot = zhs.ZhihuishuAdapter(document).snapshot()
    before = repository.path.read_bytes()
    with pytest.raises(SourceUnsupported, match="SQLite"):
        zhs.plan_zhihuishu(snapshot, repository)
    assert repository.path.read_bytes() == before
    assert not repository.list_tasks()


def test_shared_planner_new_items_exclude_course_end(document):
    class EmptyRepository:
        def list_tasks(self, **kwargs):
            return []
        def source_mappings(self, source):
            return {}
    plan = zhs.plan_zhihuishu(zhs.ZhihuishuAdapter(document).snapshot(), EmptyRepository())
    assert plan.new_count == 7
    assert all(p.kind == NEW for p in plan.proposals)
    assert all(p.record.task_type != "course_end" for p in plan.proposals)
    assert any(p.record.deadline is None for p in plan.proposals)


class ExistingMapping:
    """Future-repository shape for planner tests; no schema v4 write is claimed."""

    def __init__(self, record):
        self.task = Task(1, record.task_title, record.course, "其他", record.deadline,
                         zhs.SOURCE, False, datetime(2026, 10, 15, tzinfo=BEIJING),
                         datetime(2026, 10, 15, tzinfo=BEIJING))
        self.key = notice_key(external_key(record))

    def list_tasks(self, **kwargs):
        return [self.task]

    def source_keys(self, source, ids):
        return {self.key}

    def source_mappings(self, source):
        return {self.key: self.task.id}


@pytest.mark.parametrize("deadline, expected", [
    ("2026-11-10T20:00:00+08:00", UNCHANGED),
    ("2026-11-14T20:00:00+08:00", DEADLINE_CHANGED),
    ("2026-11-09T20:00:00+08:00", DEADLINE_CHANGED),
    (None, DEADLINE_CHANGED),
])
def test_repeat_postpone_advance_and_removed_time_only_proposed(document, deadline, expected):
    old = entry_for(zhs.ZhihuishuAdapter(document).snapshot(), "video").record
    repo = ExistingMapping(old)
    task(document)["deadline"] = deadline
    plan = zhs.plan_zhihuishu(zhs.ZhihuishuAdapter(document).snapshot(), repo)
    proposal = next(p for p in plan.proposals if p.task_id == 1)
    assert proposal.kind == expected
    assert repo.task.due_at == old.deadline
    assert repo.task.completed is False


def test_missing_from_response_does_not_delete_or_complete(document):
    old = entry_for(zhs.ZhihuishuAdapter(document).snapshot(), "video").record
    repo = ExistingMapping(old)
    document["courses"] = []
    plan = zhs.plan_zhihuishu(zhs.ZhihuishuAdapter(document).snapshot(), repo)
    assert not plan.proposals
    assert repo.task.completed is False and repo.task.deleted_at is None


def test_no_network_browser_ai_or_save_implementation():
    module = Path(zhs.__file__).read_text(encoding="utf-8")
    imports = [n.module or "" for n in ast.walk(ast.parse(module)) if isinstance(n, ast.ImportFrom)]
    imports += [a.name for n in ast.walk(ast.parse(module)) if isinstance(n, ast.Import) for a in n.names]
    assert not any(n.startswith(("requests", "http", "socket", "playwright", "urllib.request"))
                   or "ai_client" in n for n in imports)
    assert not hasattr(zhs, "apply_zhihuishu")
