from datetime import date, time, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.time_utils import BEIJING

APP = Path(__file__).resolve().parents[1] / "app.py"
NOW = datetime(2026, 10, 11, 9, tzinfo=BEIJING)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("PKU_DATABASE_PATH", str(path))
    monkeypatch.setattr("pku_deadline_agent.management_page.beijing_now", lambda: NOW)
    monkeypatch.setattr("pku_deadline_agent.parse_page.beijing_now", lambda: NOW)
    repo = TaskRepository(path)
    repo.initialize()
    return AppTest.from_file(APP).run(), repo


def manual_add(app, title="虚构第三次作业", category="虚构课程"):
    app.text_input[0].set_value(title)
    app.text_input[1].set_value(category)
    app.button[0].click().run()
    assert not app.exception


def parse(app, raw="《虚构课程》第三次作业延期至下周三晚20:00。"):
    app.radio(key="page").set_value("Deadline 智能解析").run()
    app.text_area(key="parse_raw").set_value(raw).run()
    app.button(key="parse_scan").click().run()
    if app.session_state["parse_session"].results is None:
        app.button(key="parse_run").click().run()
    assert not app.exception
    return f"candidate_{app.session_state['parse_generation']}_0_import"


def test_manual_duplicate_requires_decision_and_links_without_inserting(setup):
    app, repo = setup
    manual_add(app)
    manual_add(app)
    assert len(repo.list_tasks()) == 1
    assert app.button(key="manual_apply").disabled
    app.selectbox(key="manual_1_action").set_value("关联重复通知，保留原任务").run()
    app.checkbox(key="manual_import_confirm").check().run()
    app.button(key="manual_apply").click().run()
    assert not app.exception
    assert len(repo.list_tasks()) == 1
    assert "manual_pending" not in app.session_state


def test_complete_reopen_delete_restore_and_history_are_available(setup):
    app, repo = setup
    manual_add(app)
    task = repo.list_tasks()[0]
    app.button(key=f"manage_{task.id}_0_complete").click().run()
    assert not app.exception
    assert repo.get_task(task.id).completed
    app.selectbox(key="board_status").set_value("全部").run()
    app.button(key=f"manage_{task.id}_1_complete").click().run()
    app.button(key=f"manage_{task.id}_2_delete").click().run()
    assert not repo.list_tasks()
    app.checkbox(key="board_recycle").check().run()
    assert not app.exception
    app.button(key=f"manage_{task.id}_3_restore").click().run()
    assert not app.exception
    assert repo.get_task(task.id).deleted_at is None
    app.checkbox(key="board_recycle").uncheck().run()
    assert len(app.dataframe) >= 2
    assert "恢复任务" in app.dataframe[-1].value["操作"].tolist()


def test_edit_deadline_requires_confirmation_and_shows_old_new_history(setup):
    app, repo = setup
    manual_add(app)
    task = repo.list_tasks()[0]
    prefix = f"manage_{task.id}_0"
    app.checkbox(key=prefix + "_explicit").check().run()
    app.date_input(key=prefix + "_date").set_value(date(2026, 10, 20))
    app.time_input(key=prefix + "_time").set_value(time(20))
    app.text_input(key=prefix + "_reason").set_value("虚构通知明确截止时间")
    button = next(button for button in app.button if button.label == "保存修改")
    button.click().run()
    assert not app.exception
    assert any("确认修改" in error.value for error in app.error), [error.value for error in app.error]
    assert repo.get_task(task.id).due_at is None
    app.checkbox(key=prefix + "_confirmed").check()
    next(button for button in app.button if button.label == "保存修改").click().run()
    assert not app.exception
    assert repo.get_task(task.id).due_at == datetime(2026, 10, 20, 20, tzinfo=BEIJING)
    assert app.dataframe[1].value.iloc[0]["原截止"] == "待确认"
    assert app.dataframe[1].value.iloc[0]["新截止"] == "2026-10-20 20:00:00"


def test_board_course_type_status_and_sorting_filters(setup):
    app, repo = setup
    repo.add_task("虚构过期", "甲", due_at=NOW - timedelta(days=1))
    repo.add_task("虚构临近", "甲", due_at=NOW + timedelta(hours=2))
    repo.add_task("虚构考试", "乙", "考试", NOW + timedelta(days=1))
    repo.add_task("虚构未知", "甲")
    app.run()
    assert app.dataframe[0].value["状态"].tolist() == ["已过期", "3小时内", "1天内", "待确认"]
    app.selectbox(key="board_category").set_value("甲").run()
    app.selectbox(key="board_order").set_value("由远到近").run()
    assert app.dataframe[0].value["标题"].tolist() == ["虚构临近", "虚构过期", "虚构未知"]
    app.selectbox(key="board_status").set_value("已过期").run()
    assert app.dataframe[0].value["标题"].tolist() == ["虚构过期"]
    app.selectbox(key="board_status").set_value("全部")
    app.selectbox(key="board_category").set_value("全部")
    app.selectbox(key="board_type").set_value("考试").run()
    assert app.dataframe[0].value["标题"].tolist() == ["虚构考试"]


def test_board_quick_filter_reset_and_visible_status_column(setup):
    """The board must stay readable: status visible, and one click to narrow by urgency."""
    app, repo = setup
    repo.add_task("虚构过期", "甲", due_at=NOW - timedelta(days=1))
    repo.add_task("虚构临近", "甲", due_at=NOW + timedelta(hours=2))
    repo.add_task("虚构三天内", "甲", due_at=NOW + timedelta(days=2))
    repo.add_task("虚构未知", "甲")
    app.run()
    # Four columns only: the status must never be pushed off-screen by an id column.
    assert list(app.dataframe[0].value.columns) == ["标题", "课程 / 活动", "截止时间（北京）", "状态"]
    assert app.dataframe[0].value["状态"].tolist() == ["已过期", "3小时内", "3天内", "待确认"]
    app.segmented_control(key="board_quick").set_value("已过期").run()
    assert app.dataframe[0].value["标题"].tolist() == ["虚构过期"]
    app.button(key="board_reset").click().run()
    assert app.dataframe[0].value["标题"].tolist() == ["虚构过期", "虚构临近", "虚构三天内", "虚构未知"]
    # The quick filter stacks with the three selectboxes instead of replacing them.
    app.selectbox(key="board_type").set_value("作业").run()
    app.segmented_control(key="board_quick").set_value("3 天内").run()
    assert app.dataframe[0].value["标题"].tolist() == ["虚构三天内"]


def test_parsed_extension_updates_only_after_target_and_final_confirmation(setup):
    app, repo = setup
    old_due = datetime(2026, 10, 13, 20, tzinfo=BEIJING)
    original = repo.add_task("虚构课程第三次作业", "虚构课程", due_at=old_due)
    key = parse(app)
    assert app.button(key="parse_save").disabled
    assert repo.get_task(original.id).due_at == old_due
    app.selectbox(key=key + "_action").set_value("更新已有任务").run()
    assert not app.button(key="parse_save").disabled
    app.button(key="parse_save").click().run()
    assert not app.exception
    assert len(repo.list_tasks()) == 1
    updated = repo.get_task(original.id)
    assert updated.due_at == datetime(2026, 10, 14, 20, tzinfo=BEIJING)
    history = repo.history(original.id)[0]
    assert history["action"] == "postpone" and history["source"] == "wechat"
    assert len(history["notice_hash"]) == 64


def test_target_revision_change_on_save_requires_review_again(setup):
    app, repo = setup
    task = repo.add_task("虚构课程第三次作业", "虚构课程")
    key = parse(app)
    app.selectbox(key=key + "_action").set_value("更新已有任务").run()
    repo.set_completed(task.id, task.revision, True, confirmed=True)
    app.button(key="parse_save").click().run()
    assert any("上次展示后已改变" in item.value for item in app.error)
    assert repo.get_task(task.id).due_at is None


def test_clearing_known_due_needs_explicit_choice(setup):
    app, repo = setup
    original = repo.add_task("虚构课程第三次作业", "虚构课程", due_at=NOW + timedelta(days=1))
    key = parse(app, "《虚构课程》第三次作业下周三截止。")
    app.selectbox(key=key + "_action").set_value("更新已有任务").run()
    assert app.button(key="parse_save").disabled
    assert repo.get_task(original.id).due_at is not None
    app.checkbox(key=key + "_clear_due").check().run()
    app.button(key="parse_save").click().run()
    assert not app.exception
    assert repo.get_task(original.id).due_at is None


def test_board_leads_with_what_needs_attention(setup):
    """Opening a deadline tool should answer "what is urgent", not show a form."""
    app, repo = setup
    repo.add_task("虚构过期", "甲", due_at=NOW - timedelta(days=2))
    repo.add_task("虚构今天", "甲", due_at=NOW + timedelta(hours=3))
    repo.add_task("虚构以后", "甲", due_at=NOW + timedelta(days=30))
    repo.add_task("虚构待确认", "甲")
    app.run()
    assert not app.exception
    labels = [metric.label for metric in app.metric]
    assert labels == ["已过期", "24 小时内", "3 天内", "待确认截止"]
    counts = {metric.label: metric.value for metric in app.metric}
    assert counts["已过期"] == "1"
    assert counts["24 小时内"] == "1"
    assert counts["待确认截止"] == "1"
    # The overdue headline says it needs attention; the others do not.
    assert app.metric[0].delta == "需要处理"
    assert app.metric[2].delta == ""


def test_add_task_form_is_folded_away_by_default(setup):
    """It used to own the whole first screen and bury the list."""
    app, repo = setup
    repo.add_task("虚构任务", "甲", due_at=NOW + timedelta(days=1))
    app.run()
    assert not app.exception
    labels = [item.label for item in app.expander]
    assert "添加任务" in labels
    add = next(item for item in app.expander if item.label == "添加任务")
    assert add.proto.expanded is False


def test_task_controls_are_in_tabs_not_stacked_on_the_list(setup):
    app, repo = setup
    task = repo.add_task("虚构任务", "甲", due_at=NOW + timedelta(days=1))
    app.run()
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["编辑任务与截止时间", "变更历史"]
    # Completing from the tab still works.
    app.button(key=f"manage_{task.id}_0_complete").click().run()
    assert not app.exception
    assert repo.get_task(task.id).completed


def test_reminder_configuration_and_backup_do_not_send_notifications(setup, monkeypatch):
    app, repo = setup
    monkeypatch.setattr("pku_deadline_agent.reminders.WindowsNotifier.__call__", lambda *args: pytest.fail("UI must not send"))
    app.radio(key="page").set_value("提醒与备份").run()
    app.text_input(key="reminder_hours").set_value("72,24,3,0.5").run()
    app.button(key="reminder_save").click().run()
    assert not app.exception
    assert repo.reminder_config()["offsets"] == (259200, 86400, 10800, 1800)
    app.text_input(key="reminder_hours").set_value("nan").run()
    app.button(key="reminder_save").click().run()
    assert app.error
    assert repo.reminder_config()["offsets"] == (259200, 86400, 10800, 1800)
    app.button(key="database_backup").click().run()
    assert not app.exception
    assert any("备份完成" in message.value for message in app.success)
    assert len(list((repo.path.parent / (repo.path.name + ".backups")).glob("*.sqlite3"))) == 1
    assert not repo.reminder_events()
