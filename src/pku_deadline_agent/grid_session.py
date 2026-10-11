"""Stage 6C-2: fetch the programming-grid JSON through a browser you log into.

Design rules, in order of importance:

1. **This module never sees a password.** It launches its own browser window; you
   log in there yourself. Nothing is read from Chrome, Edge or any other profile
   you already use.
2. **No silent fallback.** A login page, a redirect, an HTTP error and a changed
   page are four different failures with four different messages. None of them
   becomes "this course has no sets".
3. **Retrieval is separated from interpretation.** This module only produces the
   same kind of payload the manual import already reads, so the tested parser,
   dedup and reviewed-apply path are reused unchanged.
4. **Nothing is written to the task database here.** Callers decide, after review.

The browser package is optional: importing this module needs only the standard
library, and the fetch path raises a clear error when the package is absent.
"""

import json
import os
import time
from pathlib import Path
from typing import Protocol

from pku_deadline_agent.config import data_directory

# Where the browser keeps its own profile. Playwright encrypts stored cookies with
# the OS user key; the application never reads or copies those files itself.
PROFILE_ENV = "PKU_BROWSER_PROFILE"
# Which installed browser to drive. "auto" prefers Chrome, then Edge.
CHANNEL_ENV = "PKU_BROWSER_CHANNEL"
BASE_URL = "https://programming.pku.edu.cn"
ALLOWED_HOST = "programming.pku.edu.cn"
LOGIN_WAIT_SECONDS = 300
NAVIGATION_TIMEOUT_MS = 30_000

# Playwright's channel names for browsers people already have installed. Using one
# of these means no bundled Chromium download is needed.
SUPPORTED_CHANNELS = ("chrome", "msedge")
_CHANNEL_RELATIVE = {
    "chrome": r"Google\Chrome\Application\chrome.exe",
    "msedge": r"Microsoft\Edge\Application\msedge.exe",
}


def detect_channel() -> str:
    """Pick a channel to drive: explicit setting, else Chrome, else Edge."""
    configured = os.environ.get(CHANNEL_ENV, "").strip().lower()
    if configured:
        if configured not in SUPPORTED_CHANNELS:
            raise SessionBrowserUnavailable(
                f"{CHANNEL_ENV} 只支持 {SUPPORTED_CHANNELS}；收到 {configured!r}。"
            )
        return configured
    roots = [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]
    # Chrome first: it is the browser this user actually uses.
    for channel in ("chrome", "msedge"):
        for root in roots:
            if root and (Path(root) / _CHANNEL_RELATIVE[channel]).exists():
                return channel
    # Nothing detected: let the browser component report the real problem.
    return "chrome"


class SessionError(RuntimeError):
    """Base class for sync-session failures."""


class SessionAuthRequired(SessionError):
    """Not logged in, or the login expired. The user must log in again."""


class SessionNetworkError(SessionError):
    """The platform could not be reached, or answered with a server error."""


class SessionBrowserUnavailable(SessionError):
    """The optional browser package is missing, or the window could not open."""


class SessionLayoutChanged(SessionError):
    """The reply was readable but is no longer the JSON we expect."""


def profile_dir() -> Path:
    """Persistent browser profile location (gitignored, local to this user)."""
    import os

    configured = os.environ.get(PROFILE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return data_directory() / "grid-session"


def course_url(course_id: str) -> str:
    """Build the read-only course URL for one course id."""
    if not isinstance(course_id, str):
        raise ValueError("课程编号必须是文本。")
    text = course_id.strip()
    if not text or not all(character in "0123456789abcdefABCDEF" for character in text) or len(text) > 64:
        # Course ids are 32-hex strings; anything else is not used to build a URL.
        raise ValueError("课程编号格式无效（应为十六进制）。")
    return f"{BASE_URL}/course/{text}/?type=json"


def validate_traversal_url(url: str) -> str:
    """Only this platform's own course pages may be opened."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("课程地址不能为空。")
    candidate = url.strip()
    if not candidate.startswith(f"{BASE_URL}/course/"):
        raise ValueError("只允许访问编程网格自己的课程页面。")
    if any(character in candidate for character in ("\n", "\r", " ", "\\")):
        raise ValueError("课程地址含非法字符。")
    return candidate


def looks_like_login(html: str) -> bool:
    """Whether a reply is the login page rather than task data."""
    if not isinstance(html, str):
        return False
    lowered = html.lower()
    markers = ("loginwithcaptcha", "iaaa.pku.edu.cn", "oauth.jsp", "authcallback", "用编程网格账号登录")
    return any(marker in lowered for marker in markers)


def looks_like_json(text: str) -> bool:
    stripped = (text or "").strip()
    return stripped.startswith("{") and stripped.endswith("}")


def parse_document(text: str, *, url: str) -> dict:
    """Turn one fetched page into the payload the import path understands."""
    if not isinstance(text, str) or not text.strip():
        raise SessionLayoutChanged(f"{url} 返回了空内容；已中止，未当成没有题集。")
    if looks_like_login(text):
        raise SessionAuthRequired("登录状态已失效，请重新登录后再同步。")
    if not looks_like_json(text):
        # The body is deliberately not included: it can contain personal data.
        raise SessionLayoutChanged(
            f"{url} 返回的不是 JSON（页面结构可能已改版）。已中止，未当成没有题集。"
        )
    try:
        payload = json.loads(text)
    except ValueError:
        raise SessionLayoutChanged(f"{url} 返回的 JSON 无法解析。已中止。") from None
    if not isinstance(payload, dict):
        raise SessionLayoutChanged(f"{url} 返回的 JSON 顶层不是对象。已中止。")
    return payload


class GridSession(Protocol):
    """What the fetch layer needs; the real browser and a fake both satisfy it."""

    def open_and_ensure_login(self) -> str:
        """Open the platform and make sure the user is logged in."""

    def fetch_text(self, url: str) -> str:
        """Return the page body for one already-authorised course URL."""

    def close(self) -> None:
        """Release the browser."""


class FetchOutcome:
    """Result of one sync attempt: documents plus the problems that occurred."""

    __slots__ = ("documents", "issues", "auth_required")

    def __init__(self):
        self.documents: list[tuple[str, dict]] = []
        self.issues: list[str] = []
        self.auth_required = False

    @property
    def usable(self) -> bool:
        """True only when every requested course produced a readable document."""
        return bool(self.documents) and not self.issues and not self.auth_required

    def add_document(self, url: str, payload: dict) -> None:
        self.documents.append((url, payload))

    def add_issue(self, url: str, message: str) -> None:
        self.issues.append(f"{url}：{message}")


def fetch_courses(session: GridSession, course_ids) -> FetchOutcome:
    """Fetch each course independently so one failure cannot hide the others.

    The caller must treat ``auth_required`` and any issue as "stop and report":
    a partial result is never applied, because a missing course would otherwise
    look like a course whose sets were all removed.
    """
    outcome = FetchOutcome()
    for course_id in course_ids:
        try:
            url = course_url(course_id)
        except ValueError as error:
            outcome.add_issue(str(course_id), str(error))
            continue
        try:
            text = session.fetch_text(url)
            payload = parse_document(text, url=url)
        except SessionAuthRequired as error:
            # One expired session invalidates the whole attempt.
            outcome.auth_required = True
            outcome.add_issue(url, str(error))
            return outcome
        except SessionNetworkError as error:
            outcome.add_issue(url, f"网络或服务端错误：{error}")
            continue
        except SessionLayoutChanged as error:
            outcome.add_issue(url, str(error))
            continue
        except SessionError as error:
            outcome.add_issue(url, str(error))
            continue
        outcome.add_document(url, payload)
    return outcome


class PlaywrightGridSession:
    """Drives the user's own installed Edge/Chrome through Playwright.

    The window is visible on purpose: you see exactly which site is opened, and
    the login is yours to complete. ``allow_real=True`` is required so no test or
    accidental call can start a real browser.
    """

    def __init__(self, *, headless: bool = False, allow_real: bool = False,
                 channel: str | None = None, profile=None) -> None:
        if not allow_real:
            raise SessionBrowserUnavailable(
                "真实浏览器会话需要显式授权（allow_real=True）；当前调用未获授权。"
            )
        self.headless = headless
        self.channel = channel or detect_channel()
        self._profile_root = Path(profile) if profile is not None else profile_dir()
        self._playwright = None
        self._context = None

    @property
    def profile(self) -> Path:
        """Per-browser profile directory.

        Chrome and Edge must not share one profile: switching between them would
        corrupt it. The application still keeps its own directory and never touches
        the browser's normal user profile.
        """
        return self._profile_root / self.channel

    def _start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SessionBrowserUnavailable(
                "未安装可选的浏览器组件。请在项目目录执行："
                r".\.venv\Scripts\python.exe -m pip install -e \".[browser]\""
            ) from None
        try:
            self._playwright = sync_playwright().start()
        except Exception as error:
            raise SessionBrowserUnavailable(
                f"无法启动浏览器组件（{error.__class__.__name__}）。"
            ) from None
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile),
                channel=self.channel,
                headless=self.headless,
                args=["--no-first-run", "--no-default-browser-check"],
            )
        except Exception as error:
            self.close()
            other = "msedge" if self.channel == "chrome" else "chrome"
            raise SessionBrowserUnavailable(
                f"无法用 {self.channel} 打开浏览器窗口（{error.__class__.__name__}）。"
                f"若已有同步窗口在运行请先关闭；也可以改用另一个浏览器："
                f'设置环境变量 {CHANNEL_ENV}={other} 后重试。'
            ) from None

    def _page(self):
        if self._context is None:
            self._start()
        pages = self._context.pages
        return pages[0] if pages else self._context.new_page()

    def open_and_ensure_login(self) -> str:
        """Open the platform and keep the window open until you are logged in.

        The old version asked the wrong question: it treated "this is not a login
        page" as "you are logged in". A landing page that is neither gave a false
        pass, and the window was closed before anyone could type a password. The
        rule now is the safe one — only leave this method once something only a
        logged-in session can produce has been seen, or after the deadline.
        """
        page = self._page()
        self._navigate(page, f"{BASE_URL}/")
        reason = self._wait_for_login(page)
        if reason != "logged_in" and self._looks_like_login_page(page):
            # Timed out still on a login page: say so instead of carrying on.
            raise SessionAuthRequired(
                f"等了 {LOGIN_WAIT_SECONDS} 秒仍未检测到登录完成，请重试。"
            )
        return page.url

    def _wait_for_login(self, page, on_tick=None) -> str:
        """Wait for a logged-in session, warming the page if that is needed.

        Bounded by LOGIN_WAIT_SECONDS and re-checked every two seconds. The window
        stays open the whole time, so there is always a chance to log in. An
        authentication guard is a normal answer on first contact, so a rejected
        request means "go to the login form", not "fail".

        Returns why it stopped: "logged_in" or "timeout".
        """
        deadline = time.monotonic() + LOGIN_WAIT_SECONDS
        warm_attempted = False
        while True:
            if self._logged_in(page):
                return "logged_in"
            if not self._looks_like_login_page(page) and not warm_attempted:
                # Absence of a login form is not proof of a session. Try one
                # harmless request: if something is readable, we are in.
                warm_attempted = True
                try:
                    response = page.goto(f"{BASE_URL}/course/", wait_until="domcontentloaded",
                                         timeout=NAVIGATION_TIMEOUT_MS)
                except Exception:
                    response = None
                if response is not None and response.status < 400 and not self._looks_like_login_page(page):
                    return "logged_in"
            if time.monotonic() >= deadline:
                return "timeout"
            page.wait_for_timeout(2000)
            if on_tick is not None:
                on_tick(deadline - time.monotonic())

    def _logged_in(self, page) -> bool:
        """True only when something a logged-in session would show is present."""
        try:
            text = page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            return False
        if not text:
            return False
        if looks_like_login(str(text)):
            return False
        return self._has_session_content(str(text))

    @staticmethod
    def _has_session_content(text: str) -> bool:
        """Markers of a real session: navigation that only exists when logged in."""
        lowered = text.lower()
        markers = ("注销", "登出", "退出登录", "log out", "logout", "我的课程", "我的作业",
                   "题集", "课程列表", "作业列表")
        return any(marker in lowered for marker in markers)

    def _looks_like_login_page(self, page) -> bool:
        if self._url_is_login_form(page.url):
            return True
        try:
            return looks_like_login(page.content())
        except Exception:
            # A missing body is not evidence of a login page; treating it as one
            # would send the user to a pointless re-login.
            return False

    def _navigate(self, page, url: str):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        except Exception as error:
            raise SessionNetworkError(f"无法打开页面（{error.__class__.__name__}）。") from None
        if response is not None:
            status = response.status
            if status >= 500:
                raise SessionNetworkError(f"服务端返回 {status}。")
            if status == 404:
                raise SessionLayoutChanged("页面不存在（404），课程地址可能已失效。")
            if status in (401, 403):
                raise SessionAuthRequired(f"访问被拒绝（{status}），请重新登录。")
        return response

    @staticmethod
    def _url_is_login_form(url: str) -> bool:
        """Only a login *form* URL counts, not any URL containing "login".

        The platform has a /login.html page that answers 200 with the sign-in form,
        which is exactly what this must catch — while the portal root must not.
        """
        lowered = (url or "").lower()
        markers = ("/login", "login.html", "loginwithcaptcha", "iaaa.pku.edu.cn", "oauth.jsp", "authcallback")
        return any(marker in lowered for marker in markers)

    def _is_login_page(self, page) -> bool:
        return self._looks_like_login_page(page)

    def fetch_text(self, url: str) -> str:
        page = self._page()
        self._navigate(page, validate_traversal_url(url))
        if self._looks_like_login_page(page):
            # The session expired between opening the window and reading here.
            raise SessionAuthRequired("登录状态已失效，请重新登录后再同步。")
        try:
            content = page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception as error:
            raise SessionLayoutChanged(f"无法读取页面内容（{error.__class__.__name__}）。") from None
        if content is None:
            raise SessionLayoutChanged("页面没有可读内容。")
        return str(content)

    def close(self) -> None:
        for closer in (self._context, self._playwright):
            if closer is not None:
                try:
                    closer.stop() if closer is self._playwright else closer.close()
                except Exception:
                    pass
        self._context = None
        self._playwright = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False
