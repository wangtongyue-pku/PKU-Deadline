"""Updated everyday UX; fake settings/transport, real privacy and save gateway."""

import json
from datetime import date, time, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.ai_config import load_settings
from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.time_utils import BEIJING

APP = Path(__file__).resolve().parents[1] / "app.py"
SIMPLE = "《虚构课程》第三次作业延期至下周三晚20:00，请在教学平台提交。"
COMPLEX = "虚构探索活动明晚八点截止报名。"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr("pku_deadline_agent.parse_page.beijing_now", lambda: datetime(2026, 10, 11, 9, tzinfo=BEIJING))
    monkeypatch.setattr("pku_deadline_agent.parse_controls.load_settings", lambda provider, **kw: load_settings(provider, live=False, model=kw.get("model")))
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
    if app.session_state["parse_session"].results is None:
        app.button(key="parse_run").click().run()
    assert not app.exception


def test_local_rules_need_no_upload_confirm_and_batch_save(app):
    assert not any(item.key == "parse_execution" for item in app.radio)
    prepare(app)
    assert app.session_state["parse_session"].plan.route == "local"
    assert app.session_state["parse_session"].results[0].due_at.isoformat() == "2026-10-14T20:00:00+08:00"
    assert not any(item.key in {"parse_confirm", "parse_real_confirm", "parse_save_confirm"} for item in app.checkbox)
    app.button(key="parse_save").click().run()
    assert any("已审核并保存 1 项" in message.value for message in app.success)
    app.radio(key="page").set_value("手动待办").run()
    assert app.dataframe[0].value.iloc[0]["标题"] == "虚构课程第三次作业"


def test_exact_preview_and_provider_change_revokes_session(app):
    prepare(app, COMPLEX)
    shown = app.session_state["parse_session"].plan.request_json
    assert shown == app.code[-1].value
    assert not app.session_state["parse_session"].mock.requests
    execute(app)
    assert app.session_state["parse_session"].mock.requests == [shown]
    app.selectbox(key="parse_provider").set_value("MiMo").run()
    assert app.session_state["parse_session"].plan is None
    assert app.selectbox(key="parse_model_MiMo_True").value == "mimo-v2.6-flash"
    app.button(key="parse_scan").click().run()
    assert "max_completion_tokens" in app.code[-1].value


def test_unknown_dates_remain_pending_and_optional_edit_works(app):
    prepare(app, "《虚构课程》作业下周三截止。", publication=False)
    execute(app)
    assert app.session_state["parse_session"].results[0].due_at is None
    prefix = f"candidate_{app.session_state['parse_generation']}_0"
    app.checkbox(key=prefix + "_explicit").check().run()
    assert app.button(key="parse_save").disabled
    app.date_input(key=prefix + "_date").set_value(date(2026, 10, 14))
    app.time_input(key=prefix + "_time").set_value(time(20)).run()
    app.text_input(key=prefix + "_title").set_value("核对后的虚构任务").run()
    assert app.dataframe[0].value.iloc[0]["任务"] == "核对后的虚构任务"
    app.button(key="parse_save").click().run()
    assert not app.exception
    assert app.session_state["parse_session"].saved


@pytest.mark.parametrize("raw,expected", [(SIMPLE, "2026-10-14T20:00:00+08:00"), (COMPLEX, "2026-10-12T20:00:00+08:00"), ("《虚构课程》作业10月20日20:00截止。", "2026-10-20T20:00:00+08:00"), ("《虚构课程》作业周五20:00截止。", "2026-10-09T20:00:00+08:00"), ("《虚构课程》作业明年1月2日20:00截止。", "2027-01-02T20:00:00+08:00")])
def test_default_reference_and_year(app, tmp_path, raw, expected):
    prepare(app, raw, publication=False)
    execute(app)
    assert app.session_state["parse_session"].results[0].due_at.isoformat() == expected
    app.button(key="parse_save").click().run()
    assert TaskRepository(tmp_path / "tasks.sqlite3").list_tasks()[0].due_at.isoformat() == expected


def test_reference_stays_frozen_across_midnight(app, monkeypatch):
    first = datetime(2026, 12, 31, 23, 59, tzinfo=BEIJING)
    monkeypatch.setattr("pku_deadline_agent.parse_page.beijing_now", lambda: first)
    prepare(app, COMPLEX, publication=False)
    shown = app.session_state["parse_session"].plan.request_json
    monkeypatch.setattr("pku_deadline_agent.parse_page.beijing_now", lambda: first + timedelta(minutes=2))
    execute(app)
    session = app.session_state["parse_session"]
    assert session.mock.requests == [shown]
    assert session.results[0].due_at.isoformat() == "2027-01-01T20:00:00+08:00"
    app.button(key="parse_scan").click().run()
    assert session.results is None
    assert session.plan.batch.notices[0].published_at == first + timedelta(minutes=2)


def test_explicit_publication_overrides_default(app):
    prepare(app, "发布时间: 2025-12-31 09:00\n《虚构课程》作业明晚八点截止。", publication=False)
    execute(app)
    assert app.session_state["parse_session"].results[0].due_at.isoformat() == "2026-01-01T20:00:00+08:00"


@pytest.mark.parametrize("raw", ["明晚交作业。\nToken:fake-only", "请私聊我报名，明晚截止。"])
def test_privacy_blocks_still_cannot_be_approved(app, raw):
    prepare(app, raw)
    assert app.button(key="parse_run").disabled
    assert app.session_state["parse_session"].plan.request_json is None


def test_no_key_is_not_replaced_by_a_fake_result(app, monkeypatch):
    monkeypatch.setattr("pku_deadline_agent.parse_controls.load_settings", load_settings)
    app.run()
    prepare(app, COMPLEX)
    assert app.button(key="parse_run").disabled
    assert app.session_state["parse_session"].results is None


def test_leaving_page_and_clear_revoke_results(app):
    prepare(app)
    app.radio(key="page").set_value("通知隐私预览").run()
    assert app.session_state["parse_session"].results is None
    app.radio(key="page").set_value("Deadline 智能解析").run()
    prepare(app)
    app.button(key="parse_clear").click().run()
    assert app.text_area(key="parse_raw").value == ""
    assert app.session_state["parse_session"].results is None


def test_existing_task_change_still_needs_target_choice(app, tmp_path):
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    original = repo.add_task("虚构课程第三次作业", category="虚构课程")
    prepare(app)
    assert app.button(key="parse_save").disabled
    assert repo.list_tasks() == [original]


def test_live_upload_one_click_is_exact_and_secrets_removed(app, monkeypatch):
    monkeypatch.setattr("pku_deadline_agent.parse_controls.load_settings", load_settings)
    monkeypatch.setenv("PKU_REAL_API_ENABLED", "1")
    monkeypatch.setenv("PKU_DEEPSEEK_API_KEY", "FICTIONAL_ONLY_KEY")
    calls = []
    def complete(client, request_json, settings, notices):
        calls.append(request_json)
        return {"usage": {"prompt_tokens": 100, "completion_tokens": 50}, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"tasks": [{"source_index": 1, "title": "虚构活动报名", "category": "虚构活动", "task_type": "活动报名", "deadline_text": "明晚八点", "due_at": "2026-10-12T20:00:00+08:00", "deadline_changed": False, "review_notes": []}]}, ensure_ascii=False)}}]}
    monkeypatch.setattr("pku_deadline_agent.ai_client.HTTPSClient.complete", complete)
    app.run()
    prepare(app, COMPLEX + "\n联系人:张小明，电话:13800138000。", publication=False)
    shown = app.session_state["parse_session"].plan.request_json
    assert not calls
    assert all(value not in shown for value in ("FICTIONAL_ONLY_KEY", "张小明", "13800138000"))
    execute(app)
    assert calls == [shown]
    assert app.button(key="parse_run").disabled
