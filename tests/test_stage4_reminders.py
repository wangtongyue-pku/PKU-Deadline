import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.reminders import deliver, preview_due, reserve_due, run_once, parse_hours, main, WindowsNotifier
from pku_deadline_agent.time_utils import BEIJING

NOW = datetime(2026, 10, 11, 23, 30, tzinfo=BEIJING)


@pytest.fixture
def repo(tmp_path):
    result = TaskRepository(tmp_path / "tasks.sqlite3")
    result.initialize()
    return result


def add(repo, hours=72):
    return repo.add_task("虚构提醒任务", due_at=NOW + timedelta(hours=hours))


def test_three_reminder_levels_are_sent_once_across_restarts(repo):
    task = add(repo)
    calls = []
    callback = lambda item, seconds: calls.append((item.id, seconds))
    assert run_once(repo, callback, NOW)["sent"] == 1
    reopened = TaskRepository(repo.path)
    assert run_once(reopened, callback, NOW + timedelta(seconds=1))["sent"] == 0
    assert run_once(reopened, callback, NOW + timedelta(hours=48))["sent"] == 1
    assert run_once(reopened, callback, NOW + timedelta(hours=69))["sent"] == 1
    assert run_once(reopened, callback, task.due_at)["sent"] == 0
    assert calls == [(task.id, 259200), (task.id, 86400), (task.id, 10800)]


def test_late_start_coalesces_missed_levels_and_never_reminds_expired(repo):
    add(repo, hours=2)
    calls = []
    assert run_once(repo, lambda item, seconds: calls.append(seconds), NOW)["sent"] == 1
    assert calls == [10800]
    assert sorted(event["state"] for event in repo.reminder_events()) == ["sent", "superseded", "superseded"]
    assert not preview_due(repo, NOW + timedelta(minutes=1))
    assert not preview_due(repo, NOW + timedelta(hours=3))


def test_pending_expired_completed_deleted_and_not_yet_due_are_skipped(repo):
    repo.add_task("待确认任务")
    repo.add_task("过期任务", due_at=NOW - timedelta(seconds=1))
    add(repo, 73)
    done = add(repo, 2)
    repo.set_completed(done.id, done.revision, True, confirmed=True)
    removed = add(repo, 2)
    repo.set_deleted(removed.id, removed.revision, True, confirmed=True)
    assert not preview_due(repo, NOW)
    assert not reserve_due(repo, NOW)


@pytest.mark.parametrize("operation", ["complete", "delete", "change", "disable", "config"])
def test_rechecks_current_task_and_rules_before_delivery(repo, operation):
    task = add(repo, 2)
    event = reserve_due(repo, NOW)[0]
    if operation == "complete":
        repo.set_completed(task.id, task.revision, True, confirmed=True)
    elif operation == "delete":
        repo.set_deleted(task.id, task.revision, True, confirmed=True)
    elif operation == "change":
        repo.edit_task(task.id, task.revision, title=task.title, category=task.category, task_type=task.task_type, due_at=NOW + timedelta(days=10), reason="虚构延期", confirmed=True)
    else:
        repo.configure_reminders((3600,), enabled=operation != "disable", confirmed=True)
    calls = []
    assert deliver(repo, event, lambda *args: calls.append(args), NOW) in {"superseded", "skipped"}
    assert not calls


def test_task_expiring_between_claim_and_send_is_not_notified(repo):
    add(repo, 2)
    event = reserve_due(repo, NOW)[0]
    calls = []
    assert deliver(repo, event, lambda *args: calls.append(args), NOW + timedelta(hours=3)) == "superseded"
    assert not calls


def test_concurrent_workers_do_not_duplicate_claim_or_send(repo):
    add(repo, 2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: reserve_due(TaskRepository(repo.path), NOW), range(2)))
    assert sum(map(len, claimed)) == 1
    event = next(value[0] for value in claimed if value)
    calls = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: deliver(TaskRepository(repo.path), event, lambda *args: calls.append(args), NOW), range(2)))
    assert sorted(outcomes) == ["sent", "skipped"]
    assert len(calls) == 1


def test_uncertain_or_failed_send_is_not_automatically_retried(repo):
    add(repo, 2)
    calls = []

    def failing(*args):
        calls.append(args)
        raise OSError("fictional delivery failure; never log task or credentials")

    assert run_once(repo, failing, NOW)["failed"] == 1
    assert run_once(repo, failing, NOW + timedelta(seconds=1))["failed"] == 0
    assert len(calls) == 1
    assert any(event["state"] == "failed" for event in repo.reminder_events())
    # A crash after reservation also consumes this level rather than double send.
    task = add(repo, 1)
    reserve_due(repo, NOW)
    assert run_once(repo, failing, NOW)["failed"] == 0
    assert any(event["task_id"] == task.id and event["state"] == "reserved" for event in repo.reminder_events())


def test_deadline_edits_make_new_cycle_but_title_edit_does_not(repo):
    task = add(repo, 2)
    calls = []
    notify = lambda item, seconds: calls.append(item.deadline_revision)
    run_once(repo, notify, NOW)
    renamed = repo.edit_task(task.id, task.revision, title="更名虚构任务", category="", task_type="作业", due_at=task.due_at, reason="更名", confirmed=True)
    assert renamed.deadline_revision == 0
    assert run_once(repo, notify, NOW)["sent"] == 0
    changed = repo.edit_task(task.id, renamed.revision, title=renamed.title, category="", task_type="作业", due_at=NOW + timedelta(hours=1), reason="提前", confirmed=True)
    assert run_once(repo, notify, NOW)["sent"] == 1
    repo.edit_task(task.id, changed.revision, title=changed.title, category="", task_type="作业", due_at=task.due_at, reason="修正回原日期", confirmed=True)
    assert run_once(repo, notify, NOW)["sent"] == 1
    assert calls == [0, 1, 2]


def test_reopening_does_not_repeat_previously_sent_level(repo):
    task = add(repo, 2)
    run_once(repo, lambda *args: None, NOW)
    done = repo.set_completed(task.id, task.revision, True, confirmed=True)
    repo.set_completed(task.id, done.revision, False, confirmed=True)
    assert run_once(repo, lambda *args: pytest.fail("duplicate after reopening"), NOW)["sent"] == 0


def test_custom_offsets_and_config_persist(repo):
    assert parse_hours("72，24,3,0.5,3") == (259200, 86400, 10800, 1800)
    repo.configure_reminders(parse_hours("0.5,1"), enabled=True, confirmed=True)
    assert TaskRepository(repo.path).reminder_config()["offsets"] == (3600, 1800)
    add(repo, .5)
    calls = []
    run_once(repo, lambda item, seconds: calls.append(seconds), NOW)
    assert calls == [1800]


@pytest.mark.parametrize("text", ["", "nan", "inf", "-1", "0", "0.001", "abc", "9000", ",3", ",".join(["1"] * 13)])
def test_invalid_offsets_rejected(text):
    with pytest.raises(ValueError):
        parse_hours(text)


def test_dry_run_and_preview_have_no_receipts_or_popup(repo, monkeypatch, capsys):
    add(repo, 2)
    monkeypatch.setattr("pku_deadline_agent.reminders.beijing_now", lambda: NOW)
    monkeypatch.setattr(WindowsNotifier, "__call__", lambda *args: pytest.fail("must not notify"))
    assert len(preview_due(repo, NOW)) == 1
    assert not repo.reminder_events()
    assert main(["--database", str(repo.path), "--dry-run", "--once"]) == 0
    assert "1 reminder(s)" in capsys.readouterr().out
    assert not repo.reminder_events()
    assert repo.reminder_config()["last_check_at"] is None


def test_utc_due_and_cross_day_are_consistent(repo):
    task = repo.add_task("虚构跨天", due_at=datetime(2026, 10, 11, 18, 30, tzinfo=timezone.utc))
    assert task.due_at.day == 12 and task.due_at.hour == 2
    assert preview_due(repo, NOW)[0][1] == 10800
    with pytest.raises(ValueError):
        reserve_due(repo, NOW.replace(tzinfo=None))


def test_native_adapter_never_executes_task_text_or_inherits_api_key(repo, monkeypatch):
    task = replace(add(repo, 2), title="虚构 $(Write-Output injected); '任务'")
    commands = []
    monkeypatch.setenv("PKU_DEEPSEEK_API_KEY", "fictional-secret-not-in-child")
    monkeypatch.setattr("pku_deadline_agent.reminders.subprocess.run", lambda *args, **kwargs: commands.append((args, kwargs)))
    # Windows test host, only the subprocess is mocked; no native popup is sent.
    WindowsNotifier()(task, 10800)
    args, options = commands[0]
    command = args[0]
    assert "-EncodedCommand" in command
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    assert "Write-Output injected" not in script
    payload = script.split("FromBase64String('")[1].split("')")[0]
    assert task.title in base64.b64decode(payload).decode("utf-8")
    assert "fictional-secret-not-in-child" not in repr(commands)
    assert "PKU_DEEPSEEK_API_KEY" not in options["env"]
    assert options["timeout"] == 12 and options["check"]
