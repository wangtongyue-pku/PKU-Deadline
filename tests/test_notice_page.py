"""Local privacy-only page; production has no simulated send option."""

from datetime import date, time, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.time_utils import BEIJING

APP = Path(__file__).resolve().parents[1] / "app.py"
RAW = "《虚构课程》作业延期至下周三20:00。\n联系人：张小明，手机13800138000。"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "tasks.sqlite3"))
    result = AppTest.from_file(APP).run()
    result.radio(key="page").set_value("通知隐私预览").run()
    assert not result.exception
    return result


def scan(app, raw=RAW):
    app.text_area(key="notice_raw").set_value(raw).run()
    app.button(key="notice_scan").click().run()
    assert not app.exception
    return app.session_state["notice_review"].preview


def test_natural_default_and_no_simulated_send(app):
    assert app.radio(key="notice_mode").value.startswith("自然语言")
    assert not any(item.key in {"notice_send", "notice_provider"} for item in [*app.button, *app.selectbox])
    assert not any("模拟" in item.label for item in app.button)


def test_full_sanitized_preview_without_private_values(app):
    preview = scan(app)
    assert preview.request_json
    shown = "\n".join(item.value for item in app.code)
    assert "下周三20:00" in shown
    assert all(value not in shown for value in ("张小明", "13800138000"))
    assert not app.session_state["notice_review"].client.requests


@pytest.mark.parametrize("credential", ["password=fictional", "Token:fake-only", "Cookie:session=fake", "API Key: sk-fictional12345678901234567890"])
def test_credentials_cannot_get_a_sendable_preview(app, credential):
    preview = scan(app, RAW + "\n" + credential)
    assert preview.request_json is None
    assert not any(item.key == "notice_send" for item in app.button)


def test_uncertain_privacy_requires_edit(app):
    preview = scan(app, "请私聊我报名，明晚截止。")
    assert preview.request_json is None
    assert app.error


@pytest.mark.parametrize("change", ["raw", "date", "time", "mode", "page"])
def test_context_changes_revoke_preview(app, change):
    scan(app)
    if change == "raw": app.text_area(key="notice_raw").set_value(RAW + "改期").run()
    elif change == "date": app.date_input(key="notice_date").set_value(date(2026, 10, 11)).run()
    elif change == "time": app.time_input(key="notice_time").set_value(time(9)).run()
    elif change == "mode": app.radio(key="notice_mode").set_value("结构化输入").run()
    else: app.radio(key="page").set_value("手动待办").run()
    assert app.session_state["notice_review"].preview is None


def test_partial_historical_date_blocks_scan(app):
    app.date_input(key="notice_date").set_value(date(2026, 10, 11)).run()
    assert app.button(key="notice_scan").disabled


def test_structured_backup_and_batch(app):
    app.radio(key="notice_mode").set_value("结构化输入").run()
    preview = scan(app, "标题: 虚构作业\n课程: 虚构课程\n截止时间: 下周三20:00\n---\n标题: 虚构活动报名\n截止时间: 10月20日20:00")
    assert len(preview.batch.notices) == 2 and preview.request_json


def test_clear_and_rescan_dont_send(app):
    scan(app)
    app.button(key="notice_scan").click().run()
    assert not app.session_state["notice_review"].client.requests
    app.button(key="notice_clear").click().run()
    assert app.text_area(key="notice_raw").value == ""
    assert app.session_state["notice_review"].preview is None


def test_default_reference_fixed_until_rescan(app, monkeypatch):
    first = datetime(2026, 12, 31, 23, 59, tzinfo=BEIJING)
    monkeypatch.setattr("pku_deadline_agent.notice_page.beijing_now", lambda: first)
    preview = scan(app)
    assert preview.batch.notices[0].published_at == first
    monkeypatch.setattr("pku_deadline_agent.notice_page.beijing_now", lambda: first + timedelta(minutes=2))
    app.run()
    assert app.session_state["notice_review"].preview == preview
    app.button(key="notice_scan").click().run()
    assert app.session_state["notice_review"].preview.batch.notices[0].published_at == first + timedelta(minutes=2)
