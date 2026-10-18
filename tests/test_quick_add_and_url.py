"""Bookmarkable pages and the sidebar quick-add form."""

import importlib.util
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.database import TaskRepository

APP = Path(__file__).resolve().parents[1] / "app.py"


def load_app():
    spec = importlib.util.spec_from_file_location("app_under_test", APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_nav_page_has_a_slug_and_parsing_is_forgiving():
    module = load_app()
    for page in module.NAV_PAGES:
        assert page in module.SLUG_BY_PAGE, page
    assert module.page_from_query("zhihuishu") == "智慧树任务预览"
    assert module.page_from_query("  MAIL ") == "学校邮箱"          # case and spaces
    assert module.page_from_query(["yuetang", "mail"]) == "问学阅堂"  # first value wins
    assert module.page_from_query(None) is None
    assert module.page_from_query("") is None
    assert module.page_from_query("nonsense") is None


@pytest.fixture
def app_with_db(tmp_path, monkeypatch):
    path = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("PKU_DATABASE_PATH", str(path))
    repo = TaskRepository(path)
    repo.initialize()
    app = AppTest.from_file(APP, default_timeout=15)
    app.session_state["page"] = "手动待办"
    app.run()
    return app, repo


def test_sidebar_quick_add_saves_a_task(app_with_db):
    app, repo = app_with_db
    assert not app.exception, [error.value for error in app.exception]
    app.text_input(key="quick_title").set_value("虚构快速任务")
    app.text_input(key="quick_category").set_value("虚构快速课")
    next(button for button in app.button if button.label == "保存任务").click().run()
    assert not app.exception, [error.value for error in app.exception]
    titles = [task.title for task in repo.list_tasks(include_completed=True)]
    assert "虚构快速任务" in titles


def test_quick_add_refuses_a_duplicate_instead_of_guessing(app_with_db):
    app, repo = app_with_db
    repo.add_task("虚构重复任务", "虚构快速课")
    app.run()
    app.text_input(key="quick_title").set_value("虚构重复任务")
    next(button for button in app.button if button.label == "保存任务").click().run()
    assert not app.exception
    assert any("可能有重复任务" in str(item.value) for item in app.warning)
    assert len(repo.list_tasks(include_completed=True)) == 1


def test_platform_pages_never_offer_quick_add(tmp_path, monkeypatch):
    """Platform pages render before any database initialize, so they must not have it."""
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=15)
    app.session_state["page"] = "智慧树任务预览"
    app.run()
    assert not app.exception
    assert not any(item.key == "quick_title" for item in app.text_input)
