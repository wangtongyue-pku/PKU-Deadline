"""The reminder process switch: hermetic tests, no real process is ever spawned."""

import json
from pathlib import Path

import pytest

from pku_deadline_agent import reminder_control


class FakeProcess:
    def __init__(self, pid=4242):
        self.pid = pid


def fake_spawn(record):
    def spawn(command):
        record.append(command)
        return FakeProcess()
    return spawn


def test_start_writes_pid_file_and_reports_running(tmp_path, monkeypatch):
    pid_file = tmp_path / "reminders.pid"
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "tasks.sqlite3"))
    commands: list[list[str]] = []
    state = reminder_control.start(path=pid_file, spawn=fake_spawn(commands), probe=lambda pid: False)
    assert state.running and state.pid == 4242
    payload = json.loads(pid_file.read_text(encoding="utf-8"))
    assert payload["pid"] == 4242 and payload["started_at"]
    assert commands and commands[0][1:3] == ["-m", "pku_deadline_agent.reminders"]
    assert "--notify" in commands[0]
    assert str(tmp_path / "tasks.sqlite3") in commands[0]


def test_start_is_idempotent_and_status_cleans_a_stale_file(tmp_path):
    pid_file = tmp_path / "reminders.pid"
    calls: list[list[str]] = []
    reminder_control.start(path=pid_file, spawn=fake_spawn(calls), probe=lambda pid: False)
    # Second start sees a live pid and does not spawn again.
    state = reminder_control.start(path=pid_file, spawn=fake_spawn(calls), probe=lambda pid: True)
    assert state.running and len(calls) == 1
    # A dead pid is reported as not running and the stale file is removed.
    dead = reminder_control.status(path=pid_file, probe=lambda pid: False)
    assert not dead.running and not pid_file.exists()
    assert reminder_control.status(path=tmp_path / "absent.pid").running is False


def test_stop_only_kills_a_live_process(tmp_path):
    pid_file = tmp_path / "reminders.pid"
    reminder_control.start(path=pid_file, spawn=fake_spawn([]), probe=lambda pid: False)
    killed: list[int] = []
    # A dead pid is cleaned up and never "killed".
    assert reminder_control.stop(path=pid_file, kill=killed.append, probe=lambda pid: False) is False
    assert killed == [] and not pid_file.exists()
    # A live one is stopped, then its file is removed.
    reminder_control.start(path=pid_file, spawn=fake_spawn([]), probe=lambda pid: False)
    assert reminder_control.stop(path=pid_file, kill=killed.append, probe=lambda pid: True) is True
    assert killed == [4242] and not pid_file.exists()


def test_describe_renders_only_real_timestamps():
    assert reminder_control.describe("2026-10-18T09:30:00+08:00") == "10-18 09:30"
    assert reminder_control.describe("") == ""
    assert reminder_control.describe("not a time") == ""


def test_reminder_page_reflects_a_running_process(tmp_path, monkeypatch):
    """When the process is up, Start is disabled and Stop is available."""
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    monkeypatch.setattr(reminder_control, "status", lambda **kwargs: reminder_control.ReminderStatus(
        True, 4321, "2026-10-18T09:30:00+08:00"))
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=15)
    app.session_state["page"] = "提醒与备份"
    app.run()
    assert not app.exception, [error.value for error in app.exception]
    assert app.button(key="reminder_start").disabled is True
    assert app.button(key="reminder_stop").disabled is False
    assert any("提醒进程运行中" in str(item.value) for item in app.success)


def test_reminder_page_shows_the_switch_without_starting_anything(tmp_path, monkeypatch):
    """Rendering the page must never spawn a process."""
    from streamlit.testing.v1 import AppTest

    started: list[list[str]] = []
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    monkeypatch.setenv("PKU_REMINDER_PID_FILE", str(tmp_path / "reminders.pid"))
    monkeypatch.setattr(reminder_control, "start",
                        lambda **kwargs: pytest.fail("the page must not start the process"))
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=15)
    app.session_state["page"] = "提醒与备份"
    app.run()
    assert not app.exception, [error.value for error in app.exception]
    assert app.button(key="reminder_start").disabled is False
    assert app.button(key="reminder_stop").disabled is True
    assert started == []
