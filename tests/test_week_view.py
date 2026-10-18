"""Week view: the buckets must never invent a date, and the page must render them."""

from datetime import datetime, timedelta
from pathlib import Path

from streamlit.testing.v1 import AppTest

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.time_utils import BEIJING
from pku_deadline_agent.week_page import card_label, monday_of, week_buckets

APP = Path(__file__).resolve().parents[1] / "app.py"
MONDAY = datetime(2026, 10, 19, tzinfo=BEIJING)   # a Monday


def test_monday_of_returns_the_monday_of_that_week():
    assert monday_of(MONDAY).isoformat() == "2026-10-19"
    assert monday_of(MONDAY + timedelta(days=6)).isoformat() == "2026-10-19"
    assert monday_of(MONDAY + timedelta(days=7)).isoformat() == "2026-10-26"


def test_week_buckets_place_tasks_and_never_guess(tmp_path):
    repository = TaskRepository(tmp_path / "week.sqlite3")
    repository.initialize()
    monday_task = repository.add_task("虚构周一作业", "甲", due_at=MONDAY + timedelta(hours=9))
    sunday_task = repository.add_task("虚构周日作业", "甲", due_at=MONDAY + timedelta(days=6, hours=23))
    undated = repository.add_task("虚构待确认作业", "甲")
    next_week = repository.add_task("虚构下周作业", "甲", due_at=MONDAY + timedelta(days=8))
    overdue = repository.add_task("虚构过期未完成", "甲", due_at=MONDAY - timedelta(days=2))
    done_overdue = repository.add_task("虚构过期已完成", "甲", due_at=MONDAY - timedelta(days=3))
    repository.set_completed(done_overdue.id, done_overdue.revision, True, confirmed=True)

    tasks = repository.list_tasks(include_completed=True)
    days, undated_tasks, overdue_tasks = week_buckets(tasks, MONDAY.date())

    assert [task.id for task in days[MONDAY.date()]] == [monday_task.id]
    assert [task.id for task in days[MONDAY.date() + timedelta(days=6)]] == [sunday_task.id]
    assert len(days) == 7
    # No deadline means no day: the task is reported separately, never guessed in.
    assert [task.id for task in undated_tasks] == [undated.id]
    # Unfinished tasks from earlier weeks stay visible instead of vanishing.
    assert [task.id for task in overdue_tasks] == [overdue.id]
    # Later weeks and completed past tasks are not part of this week.
    assert all(task.id != next_week.id for bucket in days.values() for task in bucket)
    assert all(task.id != done_overdue.id for task in overdue_tasks)


def test_card_label_marks_completion_and_keeps_a_time(tmp_path):
    repository = TaskRepository(tmp_path / "week.sqlite3")
    repository.initialize()
    task = repository.add_task("虚构作业", "甲", due_at=MONDAY + timedelta(hours=20, minutes=5))
    assert card_label(task).startswith("20:05 ")
    repository.set_completed(task.id, task.revision, True, confirmed=True)
    assert card_label(repository.get_task(task.id)).startswith("✅ 20:05")
    undated = repository.add_task("虚构待确认", "甲")
    assert card_label(undated).startswith("--:--")


def test_week_view_page_renders_days_overdue_and_undated(tmp_path, monkeypatch):
    path = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("PKU_DATABASE_PATH", str(path))
    repository = TaskRepository(path)
    repository.initialize()
    today = datetime.now(BEIJING)
    repository.add_task("虚构今天任务", "甲", due_at=today + timedelta(hours=1))
    repository.add_task("虚构待确认任务", "甲")
    repository.add_task("虚构过期任务", "甲", due_at=today - timedelta(days=9))

    app = AppTest.from_file(APP, default_timeout=15)
    app.session_state["page"] = "周视图"
    app.run()
    assert not app.exception, [error.value for error in app.exception]
    texts = "\n".join(
        [str(item.value) for item in [*app.subheader, *app.markdown]]
        + [str(item.label) for item in app.button]
    )
    assert "周视图" in texts
    for weekday in ("周一", "周三", "周日"):
        assert weekday in texts
    assert "虚构待确认任务" in texts          # its own block, not a day
    assert "逾期未完成" in texts
    assert "虚构过期任务" in texts
    # Selecting a task opens the shared management controls.
    picker = app.selectbox(key="week_task")
    target = next(task for task in repository.list_tasks(include_completed=True)
                  if task.title == "虚构今天任务")
    picker.set_value(target.id).run()
    assert not app.exception
    assert any("正在管理" in str(item.value) for item in app.markdown)
