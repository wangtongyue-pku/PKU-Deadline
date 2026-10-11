import json
from datetime import date, time, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.time_utils import BEIJING

APP = Path(__file__).resolve().parents[1] / "app.py"
SIMPLE = "《虚构课程》第三次作业延期至下周三晚20:00，请在教学平台提交。"
COMPLEX = "虚构探索活动明晚八点截止报名。"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr("pku_deadline_agent.parse_page.beijing_now", lambda: datetime(2026, 10, 11, 9, tzinfo=BEIJING))
    result = AppTest.from_file(APP).run()
    result.radio(key="page").set_value("Deadline 智能解析").run()
    assert not result.exception
    return result


def prepare(app, raw=SIMPLE, publication=True):
    app.text_area(key="parse_raw").set_value(raw)
    if publication:
        app.date_input(key="parse_date").set_value(date(2026, 10, 11))
        app.time_input(key="parse_time").set_value(time(9))
    app.run()
    app.button(key="parse_scan").click().run()
    assert not app.exception


def execute(app):
    app.checkbox(key="parse_confirm").check().run()
    app.button(key="parse_run").click().run()
    assert not app.exception


def test_default_mock_local_rules_review_and_save_flow(app):
    assert app.radio(key="parse_execution").value.startswith("本地模拟")
    prepare(app)
    assert app.session_state["parse_session"].plan.route == "local"
    assert app.button(key="parse_run").disabled
    execute(app)
    assert "2026-10-14T20:00:00+08:00" in app.code[-1].value
    assert app.button(key="parse_save").disabled
    app.checkbox(key="parse_save_confirm").check().run()
    app.button(key="parse_save").click().run()
    assert not app.exception
    assert any("已审核并保存 1 项" in message.value for message in app.success)
    app.radio(key="page").set_value("手动待办").run()
    assert app.dataframe[0].value.iloc[0]["标题"] == "虚构课程第三次作业"


def test_mock_json_preview_matches_request_and_model_switch_invalidates(app):
    prepare(app, COMPLEX)
    shown = app.session_state["parse_session"].plan.request_json
    assert shown == app.code[-1].value
    assert json.loads(shown)["model"] == "deepseek-flash"
    execute(app)
    assert app.session_state["parse_session"].mock.requests == [shown]
    app.selectbox(key="parse_provider").set_value("MiMo").run()
    assert not app.exception
    assert app.session_state["parse_session"].plan is None
    assert not app.checkbox(key="parse_confirm").value
    assert app.selectbox(key="parse_model_MiMo_False").value == "mimo-v2.6-flash"
    app.button(key="parse_scan").click().run()
    assert "max_completion_tokens" in app.code[-1].value


def test_unknown_dates_show_pending_and_can_be_manually_corrected(app):
    prepare(app, raw="《虚构课程》作业下周三截止。", publication=False)
    execute(app)
    assert app.session_state["parse_session"].results[0].due_at is None
    generation = app.session_state["parse_generation"]
    prefix = f"candidate_{generation}_0"
    app.checkbox(key=prefix + "_explicit").check().run()
    assert not app.exception
    assert any("补全日期" in message.value for message in app.error)
    app.date_input(key=prefix + "_date").set_value(date(2026, 10, 14))
    app.time_input(key=prefix + "_time").set_value(time(20)).run()
    app.checkbox(key="parse_save_confirm").check().run()
    app.text_input(key=prefix + "_title").set_value("用户核对后的虚构任务").run()
    assert not app.checkbox(key="parse_save_confirm").value
    app.checkbox(key="parse_save_confirm").check().run()
    app.button(key="parse_save").click().run()
    assert not app.exception
    assert app.session_state["parse_session"].saved


@pytest.mark.parametrize("raw, expected", [
    (SIMPLE, "2026-10-14T20:00:00+08:00"),
    (COMPLEX, "2026-10-12T20:00:00+08:00"),
    ("《虚构课程》作业10月20日20:00截止。", "2026-10-20T20:00:00+08:00"),
    ("《虚构课程》作业周五20:00截止。", "2026-10-09T20:00:00+08:00"),
    ("《虚构课程》作业明年1月2日20:00截止。", "2027-01-02T20:00:00+08:00"),
], ids=["default-week", "default-tomorrow", "default-year", "default-weekday", "explicit-next-year"])
def test_no_manual_publication_or_year_needed(app, tmp_path, raw, expected):
    prepare(app, raw=raw, publication=False)
    plan = app.session_state["parse_session"].plan
    assert plan.batch.notices[0].published_at.isoformat() == "2026-10-11T09:00:00+08:00"
    assert app.date_input(key="parse_date").value is None
    assert app.time_input(key="parse_time").value is None
    execute(app)
    assert app.session_state["parse_session"].results[0].due_at.isoformat() == expected
    app.checkbox(key="parse_save_confirm").check().run()
    app.button(key="parse_save").click().run()
    assert not app.exception
    assert TaskRepository(tmp_path / "tasks.sqlite3").list_tasks()[0].due_at.isoformat() == expected


def test_automatic_reference_is_frozen_until_rescan_even_across_midnight(app, monkeypatch):
    first = datetime(2026, 12, 31, 23, 59, tzinfo=BEIJING)
    later = first + timedelta(minutes=2)
    monkeypatch.setattr("pku_deadline_agent.parse_page.beijing_now", lambda: first)
    prepare(app, COMPLEX, publication=False)
    plan = app.session_state["parse_session"].plan
    shown = plan.request_json
    monkeypatch.setattr("pku_deadline_agent.parse_page.beijing_now", lambda: later)
    execute(app)
    session = app.session_state["parse_session"]
    assert session.mock.requests == [shown]
    assert session.results[0].due_at.isoformat() == "2027-01-01T20:00:00+08:00"
    assert session.plan.batch.notices[0].published_at == first
    app.button(key="parse_scan").click().run()
    assert not app.exception
    assert session.results is None
    assert session.plan.batch.notices[0].published_at == later
    assert not app.checkbox(key="parse_confirm").value


def test_notice_publication_overrides_automatic_reference(app):
    prepare(app, "发布时间: 2025-12-31 09:00\n《虚构课程》作业明晚八点截止。", publication=False)
    execute(app)
    assert app.session_state["parse_session"].results[0].due_at.isoformat() == "2026-01-01T20:00:00+08:00"


@pytest.mark.parametrize("raw", ["明晚交作业。\nToken:fake-only", "请私聊我报名，明晚截止。"])
def test_privacy_blocks_cannot_be_approved_in_ui(app, raw):
    prepare(app, raw)
    assert app.button(key="parse_run").disabled
    assert app.checkbox(key="parse_confirm").disabled
    assert app.session_state["parse_session"].plan.request_json is None


def test_live_stays_disabled_without_environment_even_if_selected(app, monkeypatch):
    monkeypatch.delenv("PKU_REAL_API_ENABLED", raising=False)
    app.radio(key="parse_execution").set_value("真实 API（需单独授权）").run()
    assert not app.exception
    assert any("真实调用尚未启用" in error.value for error in app.error)
    assert not any(button.key == "parse_run" for button in app.button)


def test_leaving_page_revokes_both_upload_and_save_approval(app):
    prepare(app)
    execute(app)
    app.checkbox(key="parse_save_confirm").check().run()
    app.radio(key="page").set_value("通知隐私预览").run()
    assert not app.exception
    assert app.session_state["parse_session"].results is None
    app.radio(key="page").set_value("Deadline 智能解析").run()
    assert not app.checkbox(key="parse_save_confirm").value if any(box.key == "parse_save_confirm" for box in app.checkbox) else True


def test_conflict_in_ui_does_not_overwrite(app, tmp_path):
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    original = repo.add_task("虚构课程第三次作业", category="虚构课程")
    prepare(app)
    execute(app)
    app.checkbox(key="parse_save_confirm").check().run()
    assert not app.exception
    assert app.button(key="parse_save").disabled
    assert any("确认前不会覆盖" in message.value for message in app.warning)
    assert repo.list_tasks() == [original]


def test_clear_removes_candidates_and_confirmation(app):
    prepare(app, COMPLEX)
    execute(app)
    app.button(key="parse_clear").click().run()
    assert not app.exception
    assert app.text_area(key="parse_raw").value == ""
    assert app.session_state["parse_session"].results is None
    assert not app.session_state["parse_session"].mock.requests


def test_live_ui_requires_both_confirmations_and_never_exposes_key(app, monkeypatch):
    monkeypatch.setenv("PKU_REAL_API_ENABLED", "1")
    monkeypatch.setenv("PKU_DEEPSEEK_API_KEY", "fictional-only-hidden-key")
    captured = []

    def fake_complete(client, request_json, settings, notices):
        captured.append(request_json)
        return {"usage": {"prompt_tokens": 100, "completion_tokens": 50}, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"tasks": [{"source_index": 1, "title": "虚构活动报名", "category": "虚构活动", "task_type": "活动报名", "deadline_text": "明晚八点", "due_at": "2026-10-12T20:00:00+08:00", "deadline_changed": False, "review_notes": []}]}, ensure_ascii=False)}}]}

    monkeypatch.setattr("pku_deadline_agent.ai_client.HTTPSClient.complete", fake_complete)
    app.radio(key="parse_execution").set_value("真实 API（需单独授权）").run()
    prepare(app, COMPLEX + "\n联系人:张小明，电话:13800138000。", publication=False)
    assert not app.exception
    shown = app.session_state["parse_session"].plan.request_json
    assert all(private not in shown for private in ("fictional-only-hidden-key", "张小明", "13800138000"))
    app.checkbox(key="parse_confirm").check().run()
    assert app.button(key="parse_run").disabled
    assert not captured
    app.checkbox(key="parse_real_confirm").check().run()
    app.button(key="parse_run").click().run()
    assert not app.exception
    assert captured == [shown]
    assert not app.checkbox(key="parse_real_confirm").value
    assert not app.checkbox(key="parse_confirm").value
