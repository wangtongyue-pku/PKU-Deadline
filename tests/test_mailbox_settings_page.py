"""Secure setup, daily-job UI and cached board; all OS/mail backends are fake."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.email_credentials import WindowsCredentialStore
from pku_deadline_agent.email_demo import demo_mailbox
from pku_deadline_agent.email_page import EmailPageDependencies, MODE_REAL
from pku_deadline_agent.email_sync import MailboxStateStore
from pku_deadline_agent.mailbox_cache import MailboxCache, account_key, sync_mailbox
from test_mailbox_automation import FakeVault, FakeProtection, CREDS

APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    monkeypatch.setenv("PKU_DATABASE_PATH", str(repo.path))
    vault = FakeVault()
    credentials = WindowsCredentialStore(user_confirmed_persistence=True, backend=vault)
    cache = MailboxCache(tmp_path / "mailbox.sqlite3", protector=FakeProtection())
    dependency = EmailPageDependencies(MODE_REAL, demo_mailbox, credentials, MailboxStateStore(), "fictional", cache)
    monkeypatch.setattr("pku_deadline_agent.email_page.build_dependencies", lambda mode: dependency)
    calls = []
    class Schedule:
        def __init__(self, **kw): calls.append(kw)
        def enable(self): calls.append("enable")
        def disable(self): calls.append("disable")
    monkeypatch.setattr("pku_deadline_agent.email_page.WindowsMailSchedule", Schedule)
    app = AppTest.from_file(APP)
    app.session_state["page"] = "学校邮箱"
    return app.run(), credentials, cache, repo, calls


def enter(app):
    app.text_input(key="email_address").set_value(CREDS.address)
    app.text_input(key="email_code").set_value(CREDS.authorization_code)
    app.button(key="email_credentials_save").click().run()
    assert not app.exception


def test_enter_once_saves_securely_enables_daily_and_clears_code(setup):
    app, credentials, cache, repo, calls = setup
    enter(app)
    assert credentials.get() == CREDS
    info = cache.info(account_key(CREDS.address))
    assert info["enabled"] and info["interval_hours"] == 24
    assert app.text_input(key="email_code").value == ""
    assert "enable" in calls
    assert calls[0]["task_path"] == repo.path
    text = "\n".join(item.value for item in [*app.caption, *app.markdown, *app.success, *app.info])
    assert CREDS.authorization_code not in text
    assert not repo.list_tasks()


def test_new_session_remembers_address_without_code_refill(setup):
    app, credentials, cache, repo, calls = setup
    enter(app)
    restarted = AppTest.from_file(APP)
    restarted.session_state["page"] = "学校邮箱"
    restarted.run()
    assert not restarted.exception
    assert restarted.text_input(key="email_address").value == CREDS.address
    assert restarted.text_input(key="email_code").value == ""
    assert credentials.get() == CREDS


def test_background_snapshot_appears_in_an_already_open_page(setup):
    app, credentials, cache, repo, calls = setup
    enter(app)
    sync_mailbox(cache, CREDS, demo_mailbox, force=True)
    app.run()
    assert not app.exception
    assert any(item.label == "本次邮件数" and item.value == "7" for item in app.metric)
    assert not repo.list_tasks()
    assert not repo.path.with_name("ai_usage.sqlite3").exists()


def test_pause_and_forget_do_not_delete_tasks_or_clear_other_keys(setup):
    app, credentials, cache, repo, calls = setup
    enter(app)
    original = repo.add_task("虚构保留任务")
    app.button(key="email_auto_disable").click().run()
    assert not cache.info(account_key(CREDS.address))["enabled"]
    assert credentials.get() == CREDS
    app.button(key="email_forget").click().run()
    assert not app.exception
    assert credentials.get() is None
    assert repo.list_tasks() == [original]


def test_task_scheduler_failure_is_visible_and_not_claimed_enabled(setup, monkeypatch):
    app, credentials, cache, repo, calls = setup
    def fail(self): raise RuntimeError("fictional schedule failure")
    monkeypatch.setattr("pku_deadline_agent.email_page.WindowsMailSchedule.enable", fail)
    enter(app)
    assert credentials.get() == CREDS
    assert not cache.info(account_key(CREDS.address))["enabled"]
    assert any("自动检查未能启用" in item.value for item in app.error)
