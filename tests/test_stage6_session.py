"""Stage 6C-2 tests: one-click sync session, with no browser and no network.

A fake session stands in for the browser, so login expiry, network failures,
server errors and a changed page are all exercised deterministically. The real
backend is only checked for the guards that stop it from starting by accident.
"""

import json

import pytest

from pku_deadline_agent import grid_session
from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.grid_session import (
    BASE_URL,
    FetchOutcome,
    PlaywrightGridSession,
    SessionAuthRequired,
    SessionBrowserUnavailable,
    SessionError,
    SessionLayoutChanged,
    SessionNetworkError,
    course_url,
    fetch_courses,
    looks_like_login,
    looks_like_json,
    parse_document,
    profile_dir,
    validate_traversal_url,
)
from pku_deadline_agent.mock_sources import GRID_COURSE_JSON
from pku_deadline_agent.sync import plan_sync, parse_grid_payload
from pku_deadline_agent.mock_sources import ProgrammingGridJsonAdapter


COURSE_ID = "22222222222222222222222222222222"


class FakeSession:
    """Stands in for the browser: scripted replies, recorded calls."""

    def __init__(self, replies=None, *, login_error=None):
        self.replies = replies or {}
        self.login_error = login_error
        self.opened = 0
        self.fetch_calls = []
        self.closed = False

    def open_and_ensure_login(self):
        self.opened += 1
        if self.login_error is not None:
            raise self.login_error
        return f"{BASE_URL}/home/"

    def fetch_text(self, url):
        self.fetch_calls.append(url)
        reply = self.replies.get(url, "")
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        self.closed = True


@pytest.fixture
def repo(tmp_path):
    result = TaskRepository(tmp_path / "tasks.sqlite3")
    result.initialize()
    return result


# ----------------------------------------------------------------- url handling


def test_course_url_is_built_from_a_hex_id():
    assert course_url(COURSE_ID) == f"{BASE_URL}/course/{COURSE_ID}/?type=json"


@pytest.mark.parametrize("value", ["", "  ", "not-hex!", "abc/../def", "../etc", "a" * 65, None, 12345])
def test_bad_course_id_is_refused(value):
    with pytest.raises(ValueError):
        course_url(value)


@pytest.mark.parametrize("url", [
    "https://example.com/course/abc/?type=json",
    "https://programming.pku.edu.cn.evil.test/course/abc/",
    "http://programming.pku.edu.cn/course/abc/",
    "https://programming.pku.edu.cn/home/",
    "https://programming.pku.edu.cn/course/abc/\nX",
    "",
])
def test_traversal_is_limited_to_this_platform_courses(url):
    with pytest.raises(ValueError):
        validate_traversal_url(url)


def test_platform_own_course_url_is_accepted():
    assert validate_traversal_url(course_url(COURSE_ID)).startswith(f"{BASE_URL}/course/")


# ------------------------------------------------------------- reply inspection


@pytest.mark.parametrize("marker", [
    "loginwithcaptcha", "iaaa.pku.edu.cn", "oauth.jsp", "authcallback", "用编程网格账号登录",
])
def test_login_page_markers_are_detected(marker):
    assert looks_like_login(f"<html>...{marker}...</html>") is True
    assert looks_like_login("<html>no markers here</html>") is False


def test_json_detection_is_shallow_and_strict():
    assert looks_like_json('{"a": 1}') is True
    assert looks_like_json('<html><body>{"a": 1}</body></html>') is False
    assert looks_like_json("") is False


def test_parse_document_returns_the_payload_for_valid_json():
    payload = parse_document(json.dumps(GRID_COURSE_JSON, ensure_ascii=False), url=course_url(COURSE_ID))
    assert payload["status"] == "OK"
    assert len(payload["course"]["problemlists"]) == 16


@pytest.mark.parametrize("text,expected", [
    ("", SessionLayoutChanged),
    ("   ", SessionLayoutChanged),
    ("<html>login page loginwithcaptcha</html>", SessionAuthRequired),
    ("<html><body>changed layout</body></html>", SessionLayoutChanged),
    ("{ not json", SessionLayoutChanged),
    ("[1, 2, 3]", SessionLayoutChanged),
])
def test_unusable_replies_never_look_like_an_empty_course(text, expected):
    with pytest.raises(expected):
        parse_document(text, url=course_url(COURSE_ID))


def test_changed_layout_message_does_not_leak_the_page():
    secret = "<html>student 2100012345 personal page</html>"
    with pytest.raises(SessionLayoutChanged) as error:
        parse_document(secret, url=course_url(COURSE_ID))
    assert "2100012345" not in str(error.value)


def test_expired_session_message_tells_the_user_to_log_in_again():
    with pytest.raises(SessionAuthRequired) as error:
        parse_document("<html>loginwithcaptcha</html>", url=course_url(COURSE_ID))
    assert "重新登录" in str(error.value)


# ---------------------------------------------------------------- fetch_courses


def good_reply():
    return json.dumps(GRID_COURSE_JSON, ensure_ascii=False)


def test_every_course_is_fetched_and_read():
    session = FakeSession({course_url(COURSE_ID): good_reply()})
    outcome = fetch_courses(session, [COURSE_ID])
    assert outcome.usable is True
    assert len(outcome.documents) == 1
    assert session.fetch_calls == [course_url(COURSE_ID)]


def test_partial_failure_is_reported_and_not_usable():
    other = "33333333333333333333333333333333"
    session = FakeSession({
        course_url(COURSE_ID): good_reply(),
        course_url(other): SessionNetworkError("连接超时"),
    })
    outcome = fetch_courses(session, [COURSE_ID, other])
    assert len(outcome.documents) == 1
    assert len(outcome.issues) == 1 and "网络或服务端错误" in outcome.issues[0]
    # A partial result must never be treated as complete.
    assert outcome.usable is False


def test_expired_session_stops_the_whole_attempt():
    other = "33333333333333333333333333333333"
    session = FakeSession({
        course_url(COURSE_ID): good_reply(),
        course_url(other): "<html>loginwithcaptcha</html>",
    })
    outcome = fetch_courses(session, [COURSE_ID, other])
    assert outcome.auth_required is True
    assert outcome.usable is False
    # The remaining courses are not attempted once the session is gone.
    assert len(session.fetch_calls) == 2


def test_changed_page_on_one_course_keeps_the_other_readable():
    other = "33333333333333333333333333333333"
    session = FakeSession({
        course_url(COURSE_ID): good_reply(),
        course_url(other): "<html>revamped page</html>",
    })
    outcome = fetch_courses(session, [COURSE_ID, other])
    assert len(outcome.documents) == 1 and len(outcome.issues) == 1
    assert outcome.usable is False


def test_invalid_course_id_is_reported_without_opening_a_page():
    session = FakeSession({})
    outcome = fetch_courses(session, ["not-a-hex-id"])
    assert outcome.documents == [] and outcome.issues
    assert session.fetch_calls == []


def test_no_courses_means_nothing_is_usable():
    session = FakeSession({})
    outcome = fetch_courses(session, [])
    assert outcome.usable is False
    assert session.fetch_calls == []


def test_unexpected_session_error_is_contained_per_course():
    session = FakeSession({course_url(COURSE_ID): SessionError("未知情况")})
    outcome = fetch_courses(session, [COURSE_ID])
    assert outcome.usable is False and "未知情况" in outcome.issues[0]


# ------------------------------------------------- reuse of the tested pipeline


def test_fetched_payload_flows_through_the_existing_parser_and_planner(repo):
    """The whole point of separating retrieval: reuse the tested path unchanged."""
    session = FakeSession({course_url(COURSE_ID): good_reply()})
    outcome = fetch_courses(session, [COURSE_ID])
    assert outcome.usable is True
    payload = outcome.documents[0][1]
    result = parse_grid_payload(payload, ProgrammingGridJsonAdapter)
    plan = plan_sync(result.records, result, repo)
    assert plan.new_count == 16 and plan.trustworthy is True


def test_second_sync_of_the_same_data_plans_nothing(repo):
    from pku_deadline_agent.sync import apply_sync_plan

    session = FakeSession({course_url(COURSE_ID): good_reply()})
    first = fetch_courses(session, [COURSE_ID])
    payload = first.documents[0][1]
    result = parse_grid_payload(payload, ProgrammingGridJsonAdapter)
    plan = plan_sync(result.records, result, repo)
    apply_sync_plan(repo, plan, [d for _, d in plan.actions()], confirmed=True)
    assert len(repo.list_tasks(include_completed=True)) == 16

    again = fetch_courses(session, [COURSE_ID])
    second = parse_grid_payload(again.documents[0][1], ProgrammingGridJsonAdapter)
    plan2 = plan_sync(second.records, second, repo)
    assert plan2.new_count == 0 and plan2.unchanged_count == 16


# --------------------------------------------------------------------- guards


def test_real_browser_requires_explicit_authorisation():
    with pytest.raises(SessionBrowserUnavailable) as error:
        PlaywrightGridSession()
    assert "授权" in str(error.value)


def test_chrome_is_preferred_and_edge_is_the_fallback(monkeypatch, tmp_path):
    """The user browses with Chrome, so that is what sync should drive."""
    from pku_deadline_agent import grid_session as module

    monkeypatch.delenv(module.CHANNEL_ENV, raising=False)
    chrome_dir = tmp_path / "pf" / "Google" / "Chrome" / "Application"
    chrome_dir.mkdir(parents=True)
    (chrome_dir / "chrome.exe").write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "pf86"))
    assert module.detect_channel() == "chrome"


def test_edge_is_used_when_chrome_is_absent(monkeypatch, tmp_path):
    from pku_deadline_agent import grid_session as module

    monkeypatch.delenv(module.CHANNEL_ENV, raising=False)
    edge_dir = tmp_path / "pf86" / "Microsoft" / "Edge" / "Application"
    edge_dir.mkdir(parents=True)
    (edge_dir / "msedge.exe").write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "pf86"))
    assert module.detect_channel() == "msedge"


def test_channel_can_be_set_explicitly(monkeypatch):
    from pku_deadline_agent import grid_session as module

    monkeypatch.setenv(module.CHANNEL_ENV, "msedge")
    assert module.detect_channel() == "msedge"
    monkeypatch.setenv(module.CHANNEL_ENV, "chrome")
    assert module.detect_channel() == "chrome"


def test_an_unsupported_channel_is_refused(monkeypatch):
    from pku_deadline_agent import grid_session as module

    monkeypatch.setenv(module.CHANNEL_ENV, "firefox")
    with pytest.raises(SessionBrowserUnavailable) as error:
        module.detect_channel()
    assert module.CHANNEL_ENV in str(error.value)


def test_each_browser_gets_its_own_profile(monkeypatch, tmp_path):
    """Sharing one profile between browsers would corrupt it."""
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.delenv(grid_session.PROFILE_ENV, raising=False)
    chrome = PlaywrightGridSession(allow_real=True, channel="chrome")
    edge = PlaywrightGridSession(allow_real=True, channel="msedge")
    assert chrome.profile != edge.profile
    assert chrome.profile.parent == edge.profile.parent
    assert chrome.profile.name == "chrome" and edge.profile.name == "msedge"


# ------------------------------------------------------- login-state detection
#
# Reported bug: the first real sync failed in ONE second with "no valid login
# detected". The old code asked the wrong question — it treated "this is not a
# login page" as "you are logged in", so a landing page that was neither let the
# check pass (or fail) immediately and the window closed before anyone could type
# a password. These tests pin the safe rule: only leave the wait once something
# only a logged-in session can show has been seen, or once the deadline passes.


@pytest.mark.parametrize("url,is_form", [
    ("https://programming.pku.edu.cn/x/login.html", True),
    ("https://programming.pku.edu.cn/login", True),
    ("https://iaaa.pku.edu.cn/iaaa/oauth.jsp", True),
    ("https://programming.pku.edu.cn/authcallback?token=x", True),
    # The portal root is NOT a login form, which is what the old check got wrong.
    ("https://programming.pku.edu.cn/", False),
    ("https://programming.pku.edu.cn/home/", False),
    ("https://programming.pku.edu.cn/course/abc/?type=json", False),
    ("", False),
])
def test_only_a_login_form_url_counts_as_a_login_page(url, is_form):
    assert PlaywrightGridSession._url_is_login_form(url) is is_form


def test_the_old_login_url_check_is_gone():
    """The previous rule matched any URL containing "login", including the root."""
    assert not hasattr(PlaywrightGridSession, "_url_is_login")


@pytest.mark.parametrize("text,logged_in", [
    ("退出登录 我的课程 题集", True),
    ("logout  My courses", True),
    ("课程列表", True),
    ("作业列表", True),
    ("", False),
    ("随便一段没有会话标记的文字", False),
])
def test_session_content_markers(text, logged_in):
    assert PlaywrightGridSession._has_session_content(text) is logged_in


def test_a_login_form_body_is_not_session_content():
    body = "用户名 密码 验证码 loginwithcaptcha"
    assert looks_like_login(body) is True
    assert PlaywrightGridSession._has_session_content(body) is False


def test_profile_directory_is_inside_local_data(tmp_path, monkeypatch):
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.delenv(grid_session.PROFILE_ENV, raising=False)
    assert profile_dir() == tmp_path / "grid-session"


def test_profile_directory_can_be_overridden(tmp_path, monkeypatch):
    target = tmp_path / "custom-profile"
    monkeypatch.setenv(grid_session.PROFILE_ENV, str(target))
    assert profile_dir() == target


def test_session_module_needs_no_browser_package_to_import():
    """Importing this module must not require Playwright; only launching does.

    Checked against the source rather than sys.modules, so the result does not
    depend on whether another test happened to import Playwright first.
    """
    import ast
    from pathlib import Path

    text = Path(grid_session.__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    top_level_imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_imports += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            top_level_imports.append(node.module or "")
    assert not any(name.startswith("playwright") for name in top_level_imports), top_level_imports
    # The only import of it lives inside the launch path.
    assert "from playwright.sync_api import sync_playwright" in text


def test_missing_browser_package_reports_how_to_install(monkeypatch, tmp_path):
    """Simulate the package being absent, so this holds whether or not it is here."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("simulated: playwright is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    session = PlaywrightGridSession(allow_real=True, profile=tmp_path / "unused")
    with pytest.raises(SessionBrowserUnavailable) as error:
        session._start()
    message = str(error.value)
    assert "browser" in message
    assert "pip install" in message
    session.close()


def test_close_is_safe_when_nothing_was_started():
    session = PlaywrightGridSession(allow_real=True, profile="unused")
    session.close()
    session.close()


def test_the_session_api_has_nowhere_to_put_a_credential():
    """Not "no word mentions it" but "no parameter or method accepts one"."""
    import inspect

    parameters = set(inspect.signature(PlaywrightGridSession.__init__).parameters)
    parameters |= set(inspect.signature(PlaywrightGridSession.fetch_text).parameters)
    parameters |= set(inspect.signature(FakeSession.__init__).parameters)
    for banned in ("password", "passwd", "token", "cookie", "secret", "api_key", "credentials"):
        assert not any(banned in name.lower() for name in parameters), (banned, parameters)
    assert set(inspect.signature(PlaywrightGridSession.__init__).parameters) == {
        "self", "headless", "allow_real", "channel", "profile",
    }


def test_the_session_protocol_carries_only_read_operations():
    """No submit, no upload, no answer-submission method exists."""
    methods = [name for name in vars(FakeSession) if not name.startswith("_")]
    assert set(methods) == {"open_and_ensure_login", "fetch_text", "close"}
    for name in methods:
        for banned in ("submit", "answer", "upload", "post", "solve"):
            assert banned not in name.lower()
