import sqlite3
from datetime import datetime, timezone

import pytest

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.time_utils import BEIJING


@pytest.fixture
def repository(tmp_path):
    repo = TaskRepository(tmp_path / "private" / "tasks.sqlite3")
    repo.initialize()
    return repo


def test_persists_task_after_reopening(repository):
    due = datetime(2026, 10, 20, 20, 30, tzinfo=BEIJING)
    task = repository.add_task("  虚构作业  ", "虚构课程", "作业", due)
    reopened = TaskRepository(repository.path)
    reopened.initialize()
    assert reopened.list_tasks() == [task]
    assert task.title == "虚构作业"
    assert task.due_at == due
    assert task.created_at.tzinfo.key == "Asia/Shanghai"
    assert not task.completed


def test_unspecified_deadline_is_pending(repository):
    task = repository.add_task("虚构活动，时间待定", task_type="活动报名")
    assert task.due_at is None
    assert task.needs_confirmation


def test_sorts_deadlines_and_puts_pending_last(repository):
    pending = repository.add_task("待确认")
    later = repository.add_task("较晚", due_at=datetime(2026, 10, 21, tzinfo=BEIJING))
    earlier = repository.add_task("较早", due_at=datetime(2026, 10, 20, tzinfo=BEIJING))
    assert repository.list_tasks() == [earlier, later, pending]


def test_converts_explicit_timezone_before_storing(repository):
    task = repository.add_task("时区转换", due_at=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    assert task.due_at.hour == 20
    assert task.due_at.tzinfo.key == "Asia/Shanghai"
    with sqlite3.connect(repository.path) as connection:
        value = connection.execute("SELECT due_at FROM tasks").fetchone()[0]
    assert value == "2026-10-20T20:00:00+08:00"


@pytest.mark.parametrize("kwargs", [
    {"title": "   "},
    {"title": "x" * 201},
    {"title": "测试", "category": "x" * 101},
    {"title": "测试", "task_type": "非法类型"},
    {"title": "测试", "source": "pku3b"},
    {"title": "测试", "due_at": datetime(2026, 10, 20)},
])
def test_rejects_invalid_input_without_inserting(repository, kwargs):
    with pytest.raises(ValueError):
        repository.add_task(**kwargs)
    assert repository.list_tasks() == []


def test_user_text_is_not_executed_as_sql(repository):
    text = "'); DROP TABLE tasks; --"
    repository.add_task(text)
    assert repository.list_tasks()[0].title == text
    repository.add_task("下一条仍可保存")
    assert len(repository.list_tasks()) == 2


def test_newer_schema_is_rejected(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="数据库版本较新"):
        TaskRepository(path).initialize()
