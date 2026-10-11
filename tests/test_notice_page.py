from datetime import date, time, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.time_utils import BEIJING

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
RAW = "标题: 虚构作业 13800138000\n课程: 虚构课程\n截止时间: 明晚\n闲聊: 私人话题不要转发"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    result = AppTest.from_file(APP_PATH).run()
    result.radio(key="page").set_value("通知隐私预览").run()
    result.radio(key="notice_mode").set_value("结构化输入").run()
    assert not result.exception
    return result


def prepare(app):
    app.text_area(key="notice_raw").set_value(RAW)
    app.date_input(key="notice_date").set_value(date(2026, 10, 11))
    app.time_input(key="notice_time").set_value(time(9, 0)).run()
    app.button(key="notice_scan").click().run()
    assert not app.exception
    assert app.session_state["notice_review"].preview.request_json is not None
    return app


def test_preview_needs_confirmation_and_only_sends_displayed_content(app, tmp_path):
    prepare(app)
    review = app.session_state["notice_review"]
    displayed = app.code[-1].value
    assert displayed == review.preview.request_json
    assert "13800138000" not in displayed
    assert "私人话题不要转发" not in displayed
    assert not review.client.requests
    assert app.button(key="notice_send").disabled
    app.checkbox(key="notice_confirm").check().run()
    assert not app.button(key="notice_send").disabled
    app.button(key="notice_send").click().run()
    assert not app.exception
    assert review.client.requests == (displayed,)
    assert any("未访问外部服务" in message.value for message in app.success)
    assert app.button(key="notice_send").disabled
    assert not app.checkbox(key="notice_confirm").value
    app.run()
    assert len(review.client.requests) == 1
    assert TaskRepository(tmp_path / "ui.sqlite3").list_tasks() == []


def test_credentials_disable_preview_confirmation_and_send(app):
    app.text_area(key="notice_raw").set_value(RAW + "\nCookie: sessionid=fake-only").run()
    app.button(key="notice_scan").click().run()
    assert not app.exception
    review = app.session_state["notice_review"]
    assert review.preview.request_json is None
    assert app.checkbox(key="notice_confirm").disabled
    assert app.button(key="notice_send").disabled
    assert not review.client.requests
    assert any("阻断" in error.value for error in app.error)
    assert all("fake-only" not in error.value for error in app.error)


@pytest.mark.parametrize("change", ["raw", "provider", "date", "time", "partial-date", "page"])
def test_editing_context_or_leaving_page_revokes_confirmation(app, change):
    prepare(app)
    app.checkbox(key="notice_confirm").check().run()
    assert not app.button(key="notice_send").disabled
    if change == "raw":
        app.text_area(key="notice_raw").set_value(RAW + "\n闲聊: 新内容").run()
    elif change == "provider":
        app.selectbox(key="notice_provider").set_value("MiMo").run()
    elif change == "date":
        app.date_input(key="notice_date").set_value(date(2026, 10, 12)).run()
    elif change == "time":
        app.time_input(key="notice_time").set_value(time(10, 0)).run()
    elif change == "partial-date":
        app.date_input(key="notice_date").set_value(None).run()
        assert app.button(key="notice_scan").disabled
    else:
        app.radio(key="page").set_value("手动待办").run()
        app.radio(key="page").set_value("通知隐私预览").run()
    assert not app.exception
    assert app.session_state["notice_review"].preview is None
    assert not app.checkbox(key="notice_confirm").value
    assert app.button(key="notice_send").disabled
    assert not app.session_state["notice_review"].client.requests


def test_clearing_removes_input_preview_and_local_mock_history(app):
    prepare(app)
    app.checkbox(key="notice_confirm").check().run()
    app.button(key="notice_send").click().run()
    app.button(key="notice_clear").click().run()
    assert not app.exception
    assert app.text_area(key="notice_raw").value == ""
    assert app.date_input(key="notice_date").value is None
    assert app.time_input(key="notice_time").value is None
    assert app.session_state["notice_review"].preview is None
    assert not app.session_state["notice_review"].client.requests
    assert not app.success


def test_rescan_revokes_checked_confirmation(app):
    prepare(app)
    app.checkbox(key="notice_confirm").check().run()
    app.button(key="notice_scan").click().run()
    assert not app.exception
    assert not app.checkbox(key="notice_confirm").value
    assert app.button(key="notice_send").disabled


def test_manual_management_remains_available_after_block(app):
    app.text_area(key="notice_raw").set_value("标题: 虚构练习\n密码: fake-only").run()
    app.button(key="notice_scan").click().run()
    app.radio(key="page").set_value("手动待办").run()
    app.text_input[0].set_value("仍可手动添加虚构任务")
    app.button[0].click().run()
    assert not app.exception
    assert app.dataframe[0].value.iloc[0]["标题"] == "仍可手动添加虚构任务"


def test_natural_mode_is_default_on_fresh_page(tmp_path, monkeypatch):
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "fresh.sqlite3"))
    fresh = AppTest.from_file(APP_PATH).run()
    fresh.radio(key="page").set_value("通知隐私预览").run()
    assert fresh.radio(key="notice_mode").value == "自然语言通知"
    # Exact sentence from the reported old-server failure; no manual mode switch.
    raw = "《虚构课程》作业延期至下周三晚20:00，请在教学平台提交。"
    fresh.text_area(key="notice_raw").set_value(raw).run()
    fresh.button(key="notice_scan").click().run()
    assert not fresh.exception
    assert not fresh.error
    preview = fresh.session_state["notice_review"].preview
    assert preview.request_json == fresh.code[-1].value
    assert "作业延期至下周三晚20:00" in fresh.code[-2].value
    assert "请在教学平台提交" in fresh.code[-2].value
    assert preview.batch.notices[0].published_at is not None
    assert preview.batch.notices[0].published_at.utcoffset() == timedelta(hours=8)
    assert fresh.button(key="notice_send").disabled
    assert not fresh.session_state["notice_review"].client.requests


def test_natural_input_preview_shows_full_sanitized_content_and_sends_it(app):
    import json

    app.radio(key="notice_mode").set_value("自然语言通知").run()
    raw = "大家好，《虚构课程》作业延期至下周三20:00，请在教学平台提交。\n联系人：张小明，电话:13800138000。\n今晚谁去喝奶茶？"
    app.text_area(key="notice_raw").set_value(raw).run()
    app.button(key="notice_scan").click().run()
    assert not app.exception
    review = app.session_state["notice_review"]
    preview = review.preview
    assert preview.request_json
    assert preview.batch.notices[0].sanitized_text == app.code[-2].value
    assert app.code[-1].value == preview.request_json
    assert all(value not in preview.request_json for value in ("张小明", "13800138000", "奶茶"))
    assert "延期至下周三20:00" in app.code[-2].value
    actual_content = json.loads(json.loads(preview.request_json)["messages"][1]["content"])
    assert actual_content["notices"][0]["published_at"] == preview.batch.notices[0].published_at.isoformat()
    assert app.button(key="notice_send").disabled
    app.checkbox(key="notice_confirm").check().run()
    app.button(key="notice_send").click().run()
    assert not app.exception
    assert review.client.requests == (preview.request_json,)
    assert not app.checkbox(key="notice_confirm").value


def test_default_reference_is_frozen_and_displayed_request_stays_exact(app, monkeypatch):
    first = datetime(2026, 12, 31, 23, 59, tzinfo=BEIJING)
    monkeypatch.setattr("pku_deadline_agent.notice_page.beijing_now", lambda: first)
    app.text_area(key="notice_raw").set_value(RAW).run()
    app.button(key="notice_scan").click().run()
    review = app.session_state["notice_review"]
    shown = review.preview.request_json
    assert review.preview.batch.notices[0].published_at == first
    monkeypatch.setattr("pku_deadline_agent.notice_page.beijing_now", lambda: first + timedelta(minutes=2))
    app.checkbox(key="notice_confirm").check().run()
    app.button(key="notice_send").click().run()
    assert not app.exception
    assert review.client.requests == (shown,)


def test_uncertain_natural_privacy_requires_edit_and_cannot_be_confirmed(app):
    app.radio(key="notice_mode").set_value("自然语言通知").run()
    app.text_area(key="notice_raw").set_value("请私聊我报名，明晚截止。").run()
    app.button(key="notice_scan").click().run()
    assert not app.exception
    assert app.session_state["notice_review"].preview.request_json is None
    assert any("修改原文" in error.value for error in app.error)
    assert app.checkbox(key="notice_confirm").disabled
    assert app.button(key="notice_send").disabled
    assert any("本地待修改草稿" in message.value for message in app.markdown)
    app.text_area(key="notice_raw").set_value("请于明晚前完成活动报名。").run()
    app.button(key="notice_scan").click().run()
    assert not app.exception
    assert app.session_state["notice_review"].preview.request_json
    assert not app.checkbox(key="notice_confirm").value


def test_switching_input_mode_revokes_confirmation(app):
    prepare(app)
    app.checkbox(key="notice_confirm").check().run()
    app.radio(key="notice_mode").set_value("自然语言通知").run()
    assert not app.exception
    assert app.session_state["notice_review"].preview is None
    assert not app.checkbox(key="notice_confirm").value
    assert app.button(key="notice_send").disabled
    assert not app.session_state["notice_review"].client.requests


def test_natural_credentials_block_and_structured_fallback_still_works(app):
    app.radio(key="notice_mode").set_value("自然语言通知").run()
    app.text_area(key="notice_raw").set_value("明晚交作业。\n无关聊天:Cookie:fake-only").run()
    app.button(key="notice_scan").click().run()
    assert not app.exception
    assert app.session_state["notice_review"].preview.request_json is None
    assert app.button(key="notice_send").disabled
    app.radio(key="notice_mode").set_value("结构化输入").run()
    # Switching mode alone cannot bypass authentication blocking.
    app.button(key="notice_scan").click().run()
    assert app.session_state["notice_review"].preview.request_json is None
    app.text_area(key="notice_raw").set_value("标题: 虚构练习\n截止时间: 明晚").run()
    app.button(key="notice_scan").click().run()
    assert not app.exception
    assert app.session_state["notice_review"].preview.request_json
