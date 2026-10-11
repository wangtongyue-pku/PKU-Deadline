"""Stage 6 Streamlit tests for the programming-grid import page.

The uploader itself cannot be driven by the test harness, so ``_uploaded_bytes``
is replaced; everything downstream (decode, parse, plan, render, confirm, write)
is the real production code.
"""

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.mock_sources import GRID_COURSE_JSON

APP = Path(__file__).resolve().parents[1] / "app.py"
PAGE = "编程网格导入"
# Only the read-back is replaced; the uploader widget itself still renders, so
# the page under test is the real one.
UPLOAD = "pku_deadline_agent.grid_page._read_upload"


class _FakeUpload:
    def __init__(self, name, raw):
        self.name = name
        self._raw = raw

    def getvalue(self):
        return self._raw


def payload_bytes(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def course_payload(entries, title="虚拟课程 2026 虚拟老师"):
    return {"status": "OK", "type": "course", "course": {"title": title, "problemlists": list(entries)}}


def entry(identifier, title, close, open_="2026-09-30 15:00:00.0", type_="Homework"):
    return {"id": identifier, "title": title, "type": type_,
            "assignment": {"openTime": open_, "closeTime": close}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "grid_ui.sqlite3"
    monkeypatch.setenv("PKU_DATABASE_PATH", str(path))
    repo = TaskRepository(path)
    repo.initialize()
    return path, repo


def open_page(path, monkeypatch, raw=None, name="grid.json"):
    """Launch the app on the import page.

    Widget values must be set before the first ``run()``: assigning to
    ``session_state`` after a widget has been instantiated raises, so the page is
    selected on a freshly constructed AppTest.
    """
    if raw is not None:
        monkeypatch.setattr(UPLOAD, lambda upload=None: (name, raw))
    else:
        monkeypatch.setattr(UPLOAD, lambda upload=None: None)
    app = AppTest.from_file(APP)
    app.session_state["page"] = PAGE
    return app.run()


def checkboxes(app):
    return {item.key: item for item in app.checkbox if item.key and item.key.startswith("grid_pick_")}


def confirm_key(app):
    """The confirmation key carries the generation, so it changes per file."""
    keys = [item.key for item in app.checkbox if item.key and item.key.startswith("grid_confirm")]
    assert len(keys) == 1, keys
    return keys[0]


def apply_buttons(app):
    return [item for item in app.button if item.key and item.key.startswith("grid_apply_")]


def test_page_exposes_a_json_only_uploader_and_no_paste_box(setup, monkeypatch):
    path, _ = setup
    app = open_page(path, monkeypatch)
    assert not app.exception
    assert len(app.file_uploader) == 1
    # No text area: pasting a whole page (possibly with cookies) is not invited.
    assert len(app.text_area) == 0
    titles = [item.value for item in app.subheader]
    assert "编程网格导入" in titles


def test_full_import_flow_writes_only_after_confirmation(setup, monkeypatch):
    path, repo = setup
    raw = payload_bytes(course_payload([
        entry("p1", "虚拟题集一", "2026-11-13 15:00:00.0"),
        entry("p2", "虚拟题集二", "2026-11-20 15:00:00.0"),
    ]))
    app = open_page(path, monkeypatch, raw)
    assert not app.exception
    assert repo.list_tasks(include_completed=True) == []

    picks = checkboxes(app)
    assert len(picks) == 2 and all(item.value for item in picks.values())

    # Confirmation is required: the button stays disabled until the box is ticked.
    confirm = app.checkbox(key=confirm_key(app))
    assert confirm.value is False
    confirm.check().run()
    assert not app.exception
    buttons = apply_buttons(app)
    assert len(buttons) == 1 and buttons[0].disabled is False
    buttons[0].click().run()
    assert not app.exception, [item.value for item in app.error]
    tasks = repo.list_tasks(include_completed=True)
    assert len(tasks) == 2, [item.value for item in app.error]
    assert {task.title for task in tasks} == {"虚拟题集一", "虚拟题集二"}
    assert all(task.source == "programming_grid" for task in tasks)


def test_real_shaped_file_imports_all_sets(setup, monkeypatch):
    path, repo = setup
    raw = payload_bytes(GRID_COURSE_JSON)
    app = open_page(path, monkeypatch, raw)
    assert not app.exception
    assert len(checkboxes(app)) == 16
    app.checkbox(key=confirm_key(app)).check().run()
    [item for item in app.button if item.key and item.key.startswith("grid_apply_")][0].click().run()
    assert len(repo.list_tasks(include_completed=True)) == 16


def test_deselecting_one_entry_skips_exactly_that_one(setup, monkeypatch):
    path, repo = setup
    raw = payload_bytes(course_payload([
        entry("p1", "虚拟题集一", "2026-11-13 15:00:00.0"),
        entry("p2", "虚拟题集二", "2026-11-20 15:00:00.0"),
    ]))
    app = open_page(path, monkeypatch, raw)
    picks = sorted(checkboxes(app))
    app.checkbox(key=picks[0]).uncheck().run()
    assert not app.exception
    app.checkbox(key=confirm_key(app)).check().run()
    [item for item in app.button if item.key and item.key.startswith("grid_apply_")][0].click().run()
    titles = {task.title for task in repo.list_tasks(include_completed=True)}
    assert len(titles) == 1
    assert titles == {"虚拟题集二"}


def test_second_identical_upload_proposes_nothing_and_writes_nothing(setup, monkeypatch):
    path, repo = setup
    raw = payload_bytes(course_payload([entry("p1", "虚拟题集一", "2026-11-13 15:00:00.0")]))
    app = open_page(path, monkeypatch, raw)
    app.checkbox(key=confirm_key(app)).check().run()
    [item for item in app.button if item.key and item.key.startswith("grid_apply_")][0].click().run()
    assert len(repo.list_tasks(include_completed=True)) == 1

    again = open_page(path, monkeypatch, raw)
    assert not again.exception
    assert len(checkboxes(again)) == 0
    assert any("无需导入" in item.value for item in again.success) or any(
        "无需导入" in item.value for item in again.markdown)


def test_deadline_change_needs_a_reason_and_updates_in_place(setup, monkeypatch):
    path, repo = setup
    raw = payload_bytes(course_payload([entry("p1", "虚拟题集一", "2026-11-13 15:00:00.0")]))
    app = open_page(path, monkeypatch, raw)
    app.checkbox(key=confirm_key(app)).check().run()
    [item for item in app.button if item.key and item.key.startswith("grid_apply_")][0].click().run()
    task_id = repo.list_tasks(include_completed=True)[0].id

    moved = payload_bytes(course_payload([entry("p1", "虚拟题集一", "2026-11-27 15:00:00.0")]))
    app = open_page(path, monkeypatch, moved, name="moved.json")
    assert not app.exception
    picks = checkboxes(app)
    assert len(picks) == 1
    reason_key = [key for key in (item.key for item in app.text_input) if key and key.startswith("grid_reason_")]
    assert reason_key, "截止变化必须提供原因输入"
    app.checkbox(key=confirm_key(app)).check().run()
    [item for item in app.button if item.key and item.key.startswith("grid_apply_")][0].click().run()
    assert not app.exception
    # Missing reason: nothing written.
    assert repo.get_task(task_id).due_at.isoformat() == "2026-11-13T15:00:00+08:00"
    assert any("原因" in item.value for item in app.error)


def test_deadline_change_with_reason_updates_and_records_history(setup, monkeypatch):
    path, repo = setup
    raw = payload_bytes(course_payload([entry("p1", "虚拟题集一", "2026-11-13 15:00:00.0")]))
    app = open_page(path, monkeypatch, raw)
    app.checkbox(key=confirm_key(app)).check().run()
    [item for item in app.button if item.key and item.key.startswith("grid_apply_")][0].click().run()
    task_id = repo.list_tasks(include_completed=True)[0].id

    moved = payload_bytes(course_payload([entry("p1", "虚拟题集一", "2026-11-27 15:00:00.0")]))
    app = open_page(path, monkeypatch, moved, name="moved.json")
    app.text_input(key=[key for key in (item.key for item in app.text_input if item.key) if key.startswith("grid_reason_")][0]).set_value("平台延期").run()
    app.checkbox(key=confirm_key(app)).check().run()
    [item for item in app.button if item.key and item.key.startswith("grid_apply_")][0].click().run()
    assert not app.exception
    task = repo.get_task(task_id)
    assert task.due_at.isoformat() == "2026-11-27T15:00:00+08:00"
    assert repo.history(task_id)[0]["old_due_at"] == "2026-11-13T15:00:00+08:00"
    assert repo.history(task_id)[0]["reason"] == "平台延期"


def test_broken_entry_aborts_the_whole_import(setup, monkeypatch):
    path, repo = setup
    raw = payload_bytes(course_payload([
        entry("p1", "虚拟好题集", "2026-11-13 15:00:00.0"),
        entry("p2", "虚拟坏题集", "下周三"),
    ]))
    app = open_page(path, monkeypatch, raw)
    assert not app.exception
    assert any("整批中止" in item.value for item in app.error)
    assert len(checkboxes(app)) == 0
    assert not [item for item in app.button if item.key and item.key.startswith("grid_apply_")]
    assert repo.list_tasks(include_completed=True) == []


def test_non_json_upload_is_refused_without_touching_the_database(setup, monkeypatch):
    path, repo = setup
    app = open_page(path, monkeypatch, b"<html>login page</html>")
    assert not app.exception
    assert any("JSON" in item.value for item in app.error)
    assert repo.list_tasks(include_completed=True) == []


def test_missing_sets_never_delete_existing_tasks(setup, monkeypatch):
    path, repo = setup
    raw = payload_bytes(course_payload([
        entry("p1", "虚拟题集一", "2026-11-13 15:00:00.0"),
        entry("p2", "虚拟题集二", "2026-11-20 15:00:00.0"),
    ]))
    app = open_page(path, monkeypatch, raw)
    app.checkbox(key=confirm_key(app)).check().run()
    [item for item in app.button if item.key and item.key.startswith("grid_apply_")][0].click().run()
    assert len(repo.list_tasks(include_completed=True)) == 2

    partial = payload_bytes(course_payload([entry("p1", "虚拟题集一", "2026-11-13 15:00:00.0")]))
    app = open_page(path, monkeypatch, partial, name="partial.json")
    assert not app.exception
    assert len(repo.list_tasks(include_completed=True)) == 2
    assert {task.title for task in repo.list_tasks(include_completed=True)} == {"虚拟题集一", "虚拟题集二"}


def test_leaving_the_page_drops_the_preview(setup, monkeypatch):
    path, repo = setup
    raw = payload_bytes(course_payload([entry("p1", "虚拟题集一", "2026-11-13 15:00:00.0")]))
    app = open_page(path, monkeypatch, raw)
    assert "grid_plan" in app.session_state
    app.session_state["page"] = "手动待办"
    app.run()
    assert not app.exception
    assert "grid_plan" not in app.session_state
    assert repo.list_tasks(include_completed=True) == []


def test_existing_tasks_are_not_touched_by_an_unrelated_import(setup, monkeypatch):
    path, repo = setup
    existing = repo.add_task("虚构已有任务", "虚构课程", "作业")
    raw = payload_bytes(course_payload([entry("p1", "虚拟题集一", "2026-11-13 15:00:00.0")]))
    app = open_page(path, monkeypatch, raw)
    app.checkbox(key=confirm_key(app)).check().run()
    apply_buttons(app)[0].click().run()
    kept = repo.get_task(existing.id)
    assert kept.title == "虚构已有任务" and kept.revision == existing.revision
    assert len(repo.list_tasks(include_completed=True)) == 2


# ----------------------------------------------------- one-click sync (6C-2)


COURSE_ID = "11111111111111111111111111111111"


class FakeSyncSession:
    """Scripted stand-in for the browser, injected through the module."""

    reply = None
    fetch_error = None
    login_error = None
    instances = []

    def __init__(self, **options):
        self.options = options
        self.opened = 0
        self.closed = False
        FakeSyncSession.instances.append(self)

    def open_and_ensure_login(self):
        self.opened += 1
        if FakeSyncSession.login_error is not None:
            raise FakeSyncSession.login_error
        return "https://programming.pku.edu.cn/home/"

    def fetch_text(self, url):
        if FakeSyncSession.fetch_error is not None:
            raise FakeSyncSession.fetch_error
        body = FakeSyncSession.reply
        return body.decode("utf-8") if isinstance(body, bytes) else body

    def close(self):
        self.closed = True


@pytest.fixture
def fake_sync(monkeypatch):
    from pku_deadline_agent import grid_page

    FakeSyncSession.reply = payload_bytes(GRID_COURSE_JSON)
    FakeSyncSession.fetch_error = None
    FakeSyncSession.login_error = None
    FakeSyncSession.instances = []
    monkeypatch.setattr(grid_page.grid_session, "PlaywrightGridSession", FakeSyncSession)
    # Pretend the optional package is installed so the button is enabled.
    monkeypatch.setattr(grid_page, "_browser_available", lambda: True)
    return FakeSyncSession


def sync_button(app):
    buttons = [item for item in app.button if item.key == "grid_sync_run"]
    assert len(buttons) == 1, [item.key for item in app.button]
    return buttons[0]


def run_sync(app, course_id=COURSE_ID):
    """Click sync; return the messages the working run produced.

    A failure is reported in the same run that produced it. A success also reports
    there, then asks for a restart so the new plan gets fresh widget keys — which a
    browser performs by itself, and which replaces the output, so the messages are
    captured before it.
    """
    app.text_input(key="grid_sync_course").set_value(course_id).run()
    assert not app.exception
    sync_button(app).click().run()
    messages = {
        "success": [item.value for item in app.success],
        "error": [item.value for item in app.error],
    }
    if app.session_state.get("grid_rerun"):
        app.run()
    assert not app.exception, [item.value for item in app.exception]
    return messages


def test_sync_section_is_present_and_guarded(setup, monkeypatch):
    """Without the optional package the button is disabled, not silently broken."""
    path, _ = setup
    monkeypatch.setattr("pku_deadline_agent.grid_page._browser_available", lambda: False)
    app = open_page(path, monkeypatch)
    assert not app.exception
    assert any("一键同步" in item.value for item in app.markdown)
    assert sync_button(app).disabled is True


def test_sync_requires_a_course_id(setup, monkeypatch, fake_sync):
    path, repo = setup
    app = open_page(path, monkeypatch)
    app.text_input(key="grid_sync_course").set_value("").run()
    sync_button(app).click().run()
    assert not app.exception
    assert any("课程编号" in item.value for item in app.error)
    assert repo.list_tasks(include_completed=True) == []


def test_sync_fetches_then_waits_for_confirmation(setup, monkeypatch, fake_sync):
    path, repo = setup
    app = open_page(path, monkeypatch)
    messages = run_sync(app)

    # Fetched and planned, but not written: the preview waits for the user.
    assert repo.list_tasks(include_completed=True) == []
    assert len(checkboxes(app)) == 16
    assert any("同步完成" in text for text in messages["success"])

    app.checkbox(key=confirm_key(app)).check().run()
    apply_buttons(app)[0].click().run()
    app.run()  # a real browser performs the rerun automatically
    assert not app.exception
    assert len(repo.list_tasks(include_completed=True)) == 16


def test_sync_reuses_the_same_idempotent_write_path(setup, monkeypatch, fake_sync):
    path, repo = setup
    app = open_page(path, monkeypatch)
    run_sync(app)
    app.checkbox(key=confirm_key(app)).check().run()
    apply_buttons(app)[0].click().run()
    assert len(repo.list_tasks(include_completed=True)) == 16

    # A second sync of the same data proposes nothing.
    app = open_page(path, monkeypatch)
    run_sync(app)
    assert checkboxes(app) == {}
    assert len(repo.list_tasks(include_completed=True)) == 16


def test_sync_stops_when_login_is_required(setup, monkeypatch, fake_sync):
    path, repo = setup
    from pku_deadline_agent.grid_session import SessionAuthRequired

    fake_sync.login_error = SessionAuthRequired("需要登录")
    app = open_page(path, monkeypatch)
    messages = run_sync(app)
    assert any("需要登录" in text for text in messages["error"])
    assert checkboxes(app) == {}
    assert repo.list_tasks(include_completed=True) == []


def test_sync_stops_on_network_error_without_touching_tasks(setup, monkeypatch, fake_sync):
    path, repo = setup
    from pku_deadline_agent.grid_session import SessionNetworkError

    existing = repo.add_task("虚构已有任务", "虚构课程", "作业")
    fake_sync.fetch_error = SessionNetworkError("连接超时")
    app = open_page(path, monkeypatch)
    messages = run_sync(app)
    assert any(("网络" in text) or ("服务端" in text) for text in messages["error"])
    assert repo.get_task(existing.id).title == "虚构已有任务"
    assert len(repo.list_tasks(include_completed=True)) == 1


def test_sync_stops_when_the_page_changed(setup, monkeypatch, fake_sync):
    path, repo = setup
    fake_sync.reply = b"<html>completely new page layout</html>"
    app = open_page(path, monkeypatch)
    messages = run_sync(app)
    assert any(("改版" in text) or ("JSON" in text) for text in messages["error"])
    assert repo.list_tasks(include_completed=True) == []


def test_sync_stops_when_the_reply_is_a_login_page(setup, monkeypatch, fake_sync):
    path, repo = setup
    fake_sync.reply = b"<html>loginwithcaptcha please sign in</html>"
    app = open_page(path, monkeypatch)
    messages = run_sync(app)
    assert any("登录" in text for text in messages["error"])
    assert repo.list_tasks(include_completed=True) == []


def test_sync_closes_the_browser_even_on_failure(setup, monkeypatch, fake_sync):
    path, _ = setup
    from pku_deadline_agent.grid_session import SessionNetworkError

    fake_sync.fetch_error = SessionNetworkError("连接超时")
    app = open_page(path, monkeypatch)
    run_sync(app)
    assert fake_sync.instances and all(item.closed for item in fake_sync.instances)


def test_manual_import_still_works_alongside_sync(setup, monkeypatch, fake_sync):
    """Requirement 9: the manual path must survive the sync feature."""
    path, repo = setup
    raw = payload_bytes(course_payload([entry("p1", "虚拟题集一", "2026-11-13 15:00:00.0")]))
    app = open_page(path, monkeypatch, raw)
    assert len(checkboxes(app)) == 1
    app.checkbox(key=confirm_key(app)).check().run()
    apply_buttons(app)[0].click().run()
    assert len(repo.list_tasks(include_completed=True)) == 1
