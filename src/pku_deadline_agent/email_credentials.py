"""Stage 7B credential handling for the read-only mailbox connection.

Rules set by the user:

- Credentials are entered by the user (a page control arrives in 7D; a terminal
  helper is provided here for manual use). They never appear in the agent dialog,
  logs, or source.
- **Default: current session only.** :class:`SessionCredentialStore` keeps the
  authorization code in process memory and forgets it on exit.
- Persisting into the Windows Credential Manager is possible but **disabled
  until the user explicitly confirms it** — :class:`WindowsCredentialStore`
  refuses to run until that decision is made, and says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from getpass import getpass
from typing import Protocol


@dataclass(frozen=True)
class MailCredentials:
    """Campus account + client authorization code for read-only IMAP.

    The code is *not* the webmail password (PKU mandates client authorization
    codes). ``repr``/``str`` never reveal it, so an accidental log line or
    traceback frame cannot leak the secret.
    """

    address: str
    authorization_code: str

    def __str__(self) -> str:
        return f"MailCredentials(address={self.address!r}, authorization_code=***)"

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return self.__str__()


class CredentialStore(Protocol):
    def save(self, credentials: MailCredentials) -> None: ...
    def get(self) -> MailCredentials | None: ...
    def clear(self) -> None: ...


class SessionCredentialStore:
    """In-memory store; lives exactly as long as the process does."""

    def __init__(self) -> None:
        self._credentials: MailCredentials | None = None

    def save(self, credentials: MailCredentials) -> None:
        self._credentials = credentials

    def get(self) -> MailCredentials | None:
        return self._credentials

    def clear(self) -> None:
        self._credentials = None


class WindowsCredentialStore:
    """Windows Credential Manager storage — **intentionally not active**.

    Persisting the authorization code outside the process is a security decision
    the user must make explicitly. Until they do, construction fails with the
    exact question that needs answering, so this path cannot be drifted into by
    accident.
    """

    _CONFIRMATION_MESSAGE = (
        "Windows 凭据管理器持久化尚未获得用户确认：授权码将以系统级加密形式保存在本机，"
        "任何能以当前 Windows 账户运行的程序都可以读取它。确认接受此权衡后才会启用。"
    )

    def __init__(self, *, user_confirmed_persistence: bool = False) -> None:
        if not user_confirmed_persistence:
            raise PermissionError(self._CONFIRMATION_MESSAGE)
        self._user_confirmed = user_confirmed_persistence
        self._fallback = SessionCredentialStore()  # replaced by wincred in the confirmed path

    def save(self, credentials: MailCredentials) -> None:
        raise NotImplementedError(self._CONFIRMATION_MESSAGE)

    def get(self) -> MailCredentials | None:
        raise NotImplementedError(self._CONFIRMATION_MESSAGE)

    def clear(self) -> None:
        raise NotImplementedError(self._CONFIRMATION_MESSAGE)


def read_credentials_interactively() -> MailCredentials:
    """Terminal helper for manual, user-driven entry (the page arrives in 7D).

    The authorization code is read with ``getpass`` so it is never echoed, and
    the caller decides which store — if any — receives it.
    """
    address = input("校园网邮箱地址（完整地址，如 id@stu.pku.edu.cn）：").strip()
    if not address:
        raise ValueError("邮箱地址不能为空。")
    code = getpass("客户端授权码（输入不回显）：")
    if not code:
        raise ValueError("客户端授权码不能为空。")
    return MailCredentials(address=address, authorization_code=code)
