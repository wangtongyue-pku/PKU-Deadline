import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pku_deadline_agent.database import TaskRepository, TaskChanged, TaskConflict
from pku_deadline_agent.extraction import ExtractedTask
from pku_deadline_agent.extraction import local_candidates
from pku_deadline_agent.notices import filter_notices
from pku_deadline_agent.task_management import ImportDecision, NoticeOrigin, origin_for_notice, urgency, filter_tasks
from pku_deadline_agent.time_utils import BEIJING

NOW = datetime(2026, 10, 11, 9, tzinfo=BEIJING)
DUE = NOW + timedelta(days=5)
BASE = ExtractedTask(1, "虚构课程第三次作业", "虚构课程", "作业", "10月16日09:00", DUE, False, ())
ORIGIN = NoticeOrigin("wechat", "a" * 64, NOW)


@pytest.fixture
def repo(tmp_path):
    result = TaskRepository(tmp_path / "tasks.sqlite3")
    result.initialize()
    return result


def add(repo, candidate=BASE, origin=ORIGIN):
    return repo.apply_reviewed_tasks((candidate,), (ImportDecision("new"),), (origin,), confirmed=True)[0]


def update(repo, task, candidate, reason="虚构通知延期"):
    return repo.apply_reviewed_tasks((candidate,), (ImportDecision("update", task.id, task.revision, reason),), (ORIGIN,), confirmed=True)[0]


def test_exact_duplicate_links_without_new_task_or_overwrite(repo):
    original = add(repo)
    match = repo.suggestions(BASE, ORIGIN)[0]
    assert match.kind == "duplicate"
    assert match.task == original
    linked = repo.apply_reviewed_tasks((BASE,), (ImportDecision("link", original.id, original.revision),), (ORIGIN,), confirmed=True)
    assert linked == [original]
    assert repo.list_tasks() == [original]
    with sqlite3.connect(repo.path) as connection:
        assert connection.execute("SELECT count(*) FROM task_notices").fetchone()[0] == 1
    assert len(repo.history(original.id)) == 1  # Repeated same source has no new history.


def test_course_and_type_prevent_false_strong_duplicates(repo):
    add(repo)
    other = replace(BASE, category="另一门虚构课程")
    assert not repo.suggestions(other)
    assert add(repo, other, replace(ORIGIN, notice_hash="b" * 64)).category == other.category
    different_type = replace(BASE, task_type="考试")
    assert all(match.kind == "possible" for match in repo.suggestions(different_type))


def test_different_assignment_numbers_in_same_notice_are_distinct(repo):
    add(repo)
    second = replace(BASE, title="虚构课程第四次作业")
    assert not repo.suggestions(second, ORIGIN)
    add(repo, second)
    assert len(repo.list_tasks()) == 2


def test_arabic_chinese_ordinal_and_fuzzy_titles_require_review(repo):
    add(repo)
    assert repo.suggestions(replace(BASE, title="虚构课程第3次作业"))[0].kind == "duplicate"
    candidate = replace(BASE, title="虚构课程第3次编程作业")
    assert repo.suggestions(candidate)[0].kind == "possible"
    with pytest.raises(TaskConflict):
        add(repo, candidate)
    add_distinct = ImportDecision("new", allow_distinct=True, reason="用户确认两项独立任务")
    repo.apply_reviewed_tasks((candidate,), (add_distinct,), (ORIGIN,), confirmed=True)
    assert len(repo.list_tasks()) == 2


def test_generic_titles_never_claim_certain_duplicate(repo):
    candidate = replace(BASE, title="待确认任务", category="")
    add(repo, candidate)
    assert all(item.kind == "possible" for item in repo.suggestions(candidate, ORIGIN))


def test_precise_match_does_not_mix_unrelated_same_course_change_suggestions(repo):
    original = add(repo)
    repo.add_task("虚构期末实验报告", category=BASE.category)
    matches = repo.suggestions(replace(BASE, deadline_changed=True, due_at=DUE + timedelta(days=1)))
    assert [match.task.id for match in matches] == [original.id]


@pytest.mark.parametrize("verb", ["提前至", "提前截止至", "调整为", "修正为", "更正为", "纠正为"])
def test_correction_notice_selects_new_date_and_marks_change(verb):
    raw = f"《虚构课程》第三次作业原定下周五20:00截止，截止时间{verb}下周三20:00。"
    tasks, complete = local_candidates(filter_notices(raw, NOW, mode="natural").notices)
    assert complete and tasks[0].deadline_changed
    assert tasks[0].due_at == datetime(2026, 10, 14, 20, tzinfo=BEIJING)


@pytest.mark.parametrize("delta, action", [(2, "postpone"), (-2, "advance")])
def test_deadline_change_requires_explicit_confirmation_and_records_history(repo, delta, action):
    original = add(repo)
    changed = replace(BASE, due_at=DUE + timedelta(days=delta), deadline_changed=True)
    assert repo.suggestions(changed)[0].kind == "deadline_change"
    with pytest.raises(ValueError, match="确认"):
        repo.apply_reviewed_tasks((changed,), (ImportDecision("update", original.id, original.revision),), (ORIGIN,))
    assert repo.get_task(original.id) == original
    result = update(repo, original, changed)
    assert result.due_at == changed.due_at
    assert result.revision == 1 and result.deadline_revision == 1
    history = repo.history(original.id)[0]
    assert history["old_due_at"] == DUE.isoformat()
    assert history["new_due_at"] == changed.due_at.isoformat()
    assert history["action"] == action
    assert history["reason"] == "虚构通知延期"
    assert history["source"] == "wechat" and history["notice_hash"] == ORIGIN.notice_hash
    assert datetime.fromisoformat(history["changed_at"]).utcoffset() == timedelta(hours=8)


def test_linking_conflicting_date_keeps_old_date(repo):
    original = add(repo)
    candidate = replace(BASE, due_at=DUE + timedelta(days=1))
    origin = replace(ORIGIN, notice_hash="b" * 64)
    repo.apply_reviewed_tasks((candidate,), (ImportDecision("link", original.id, original.revision),), (origin,), confirmed=True)
    assert repo.get_task(original.id) == original
    assert repo.history(original.id)[0]["action"] == "link_notice"


def test_stale_confirmation_and_partial_batch_are_rolled_back(repo):
    original = add(repo)
    done = repo.set_completed(original.id, original.revision, True, confirmed=True)
    independent = replace(BASE, title="独立虚构活动", category="另一课程", task_type="活动报名")
    with pytest.raises(TaskChanged):
        repo.apply_reviewed_tasks((independent, BASE), (ImportDecision("new"), ImportDecision("update", original.id, original.revision)), (ORIGIN, ORIGIN), confirmed=True)
    assert repo.list_tasks(include_completed=True) == [done]
    assert len(repo.history(original.id)) == 2


def test_completed_deleted_restored_and_import_update_preserves_completion(repo):
    original = add(repo)
    done = repo.set_completed(original.id, original.revision, True, confirmed=True)
    assert not repo.list_tasks()
    assert repo.list_tasks(True) == [done]
    changed = update(repo, done, replace(BASE, due_at=DUE + timedelta(days=2)))
    assert changed.completed
    removed = repo.set_deleted(changed.id, changed.revision, True, confirmed=True)
    assert not repo.list_tasks(True)
    assert repo.list_tasks(True, include_deleted=True) == [removed]
    assert removed.deleted_at is not None
    restored = repo.set_deleted(removed.id, removed.revision, False, confirmed=True)
    reopened = repo.set_completed(restored.id, restored.revision, False, confirmed=True)
    assert repo.list_tasks() == [reopened]
    assert {row["action"] for row in repo.history(original.id)} >= {"create", "complete", "postpone", "delete", "restore", "reopen"}


def test_manual_edit_normalizes_timezone_and_keeps_history(repo):
    original = add(repo)
    changed = repo.edit_task(original.id, original.revision, title="更正后的虚构任务", category="新课程", task_type="考试", due_at=datetime(2026, 10, 16, 12, tzinfo=timezone.utc), reason="时区修正", confirmed=True)
    assert changed.due_at.hour == 20
    assert changed.due_at.tzinfo.key == "Asia/Shanghai"
    assert json.loads(repo.history(original.id)[0]["old_state"])["title"] == original.title


@pytest.mark.parametrize("kwargs", [
    {"confirmed": False}, {"title": ""}, {"category": "x" * 101}, {"task_type": "shell"},
    {"due_at": datetime(2026, 10, 16)}, {"reason": ""},
], ids=["no-confirmation", "empty-title", "long-course", "bad-type", "naive-date", "no-reason"])
def test_invalid_edits_cannot_modify_task(repo, kwargs):
    original = add(repo)
    options = dict(title=BASE.title, category=BASE.category, task_type=BASE.task_type, due_at=DUE, reason="手动修正", confirmed=True)
    options.update(kwargs)
    with pytest.raises(ValueError):
        repo.edit_task(original.id, original.revision, **options)
    assert repo.get_task(original.id) == original


def test_board_filters_urgency_and_sorts_beijing_dates(repo):
    past = repo.add_task("虚构过期", "甲", due_at=NOW - timedelta(seconds=1))
    soon = repo.add_task("虚构临近", "甲", due_at=NOW + timedelta(hours=2))
    later = repo.add_task("虚构考试", "乙", "考试", NOW + timedelta(days=2))
    pending = repo.add_task("未知时间", "甲")
    done = repo.set_completed(later.id, later.revision, True, confirmed=True)
    assert [urgency(task, NOW) for task in (past, soon, done, pending)] == ["已过期", "3小时内", "已完成", "待确认"]
    assert filter_tasks(repo.list_tasks(True), category="甲", now=NOW) == [past, soon, pending]
    assert filter_tasks(repo.list_tasks(True), status="已过期", now=NOW) == [past]
    assert filter_tasks(repo.list_tasks(True), task_type="考试", status="全部", now=NOW) == [done]


def test_only_sanitized_notice_hash_is_persisted(repo):
    raw = "《虚构课程》第三次作业10月16日09:00截止。\n联系人:张小明，电话:13800138000。"
    notice = filter_notices(raw, NOW, mode="natural").notices[0]
    origin = origin_for_notice(notice)
    add(repo, origin=origin)
    with sqlite3.connect(repo.path) as connection:
        stored = connection.execute("SELECT * FROM task_notices").fetchall()
    assert len(origin.notice_hash) == 64
    assert all(value not in repr(stored) for value in (raw, "张小明", "13800138000", "第三次作业"))


def make_v2(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,category TEXT NOT NULL,
        task_type TEXT NOT NULL,due_at TEXT,source TEXT NOT NULL,completed INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
    connection.execute("CREATE TABLE task_extractions(task_id INTEGER PRIMARY KEY,source_index INTEGER,deadline_changed INTEGER,review_notes TEXT,deadline_text TEXT)")
    connection.execute("INSERT INTO tasks VALUES (7,'虚构旧任务','虚构课程','作业',?,'manual',0,?,?)", (DUE.isoformat(), NOW.isoformat(), NOW.isoformat()))
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    return connection


def test_migration_backs_up_wal_and_preserves_all_original_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    source = make_v2(path)
    before = source.execute("SELECT * FROM tasks").fetchall()
    repo = TaskRepository(path)
    repo.initialize()
    backups = list((tmp_path / "old.sqlite3.backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 2
        assert backup.execute("SELECT * FROM tasks").fetchall() == before
    assert repo.get_task(7).title == "虚构旧任务"
    with sqlite3.connect(path) as current:
        assert current.execute("SELECT id,title,category,task_type,due_at,source,completed,created_at,updated_at FROM tasks").fetchall() == before
        assert current.execute("PRAGMA user_version").fetchone()[0] == 3
    repo.initialize()
    assert len(list((tmp_path / "old.sqlite3.backups").glob("*.sqlite3"))) == 1
    source.close()


def test_failed_backup_aborts_before_database_changes(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    source = make_v2(path)
    repo = TaskRepository(path)
    monkeypatch.setattr(repo, "backup", lambda: (_ for _ in ()).throw(OSError("fictional backup failure")))
    with pytest.raises(OSError):
        repo.initialize()
    assert source.execute("PRAGMA user_version").fetchone()[0] == 2
    assert "revision" not in {row[1] for row in source.execute("PRAGMA table_info(tasks)")}
    source.close()


def test_migration_failure_rolls_back_columns_and_schema_version(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    source = make_v2(path)
    repo = TaskRepository(path)
    original_connect = repo._connect

    class FailConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            if "CREATE TABLE IF NOT EXISTS task_history" in sql:
                raise sqlite3.OperationalError("fictional migration interruption")
            return self.connection.execute(sql, *args)

    @contextmanager
    def failing_connect():
        with original_connect() as connection:
            yield FailConnection(connection)

    monkeypatch.setattr(repo, "_connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError):
        repo.initialize()
    assert source.execute("PRAGMA user_version").fetchone()[0] == 2
    assert "revision" not in {row[1] for row in source.execute("PRAGMA table_info(tasks)")}
    assert source.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    source.close()


def test_manual_backup_is_independent_and_unique(repo):
    original = add(repo)
    first, second = repo.backup(), repo.backup()
    assert first != second
    repo.set_deleted(original.id, original.revision, True, confirmed=True)
    assert TaskRepository(first).get_task(original.id).deleted_at is None
