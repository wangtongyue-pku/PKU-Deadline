"""Mailbox credentials: current-user Windows vault, no plaintext fallback.

Persistence and daily checks were explicitly requested on 2026-10-13. Session
storage remains for injectable offline tests; production uses the Windows vault.
"""

from __future__ import annotations

from dataclasses import dataclass
from getpass import getpass
from typing import Protocol
import json
import os
import re


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
    """One replaceable mailbox, encrypted by the current Windows account.

    No plaintext fallback. The user has authorized persistence in the product;
    callers still pass the explicit gate. Tests inject an in-memory backend.
    """

    SERVICE = "PKU Deadline Agent Mail"
    ACCOUNT = "default-mailbox"

    _CONFIRMATION_MESSAGE = (
        "Windows 凭据管理器持久化尚未获得用户确认：授权码将以系统级加密形式保存在本机，"
        "任何能以当前 Windows 账户运行的程序都可以读取它。确认接受此权衡后才会启用。"
    )

    def __init__(self, *, user_confirmed_persistence: bool = False, backend=None) -> None:
        if not user_confirmed_persistence:
            raise PermissionError(self._CONFIRMATION_MESSAGE)
        self._user_confirmed = user_confirmed_persistence
        self._backend = backend

    def _storage(self):
        if self._backend is None:
            if os.name != "nt":
                raise RuntimeError("邮箱安全保存仅支持 Windows 凭据管理器。")
            try:
                from keyring.backends.Windows import WinVaultKeyring
                self._backend = WinVaultKeyring()
            except Exception:
                raise RuntimeError('无法使用 Windows 凭据管理器；请确认已安装可选依赖 .[secrets]。') from None
        return self._backend

    def save(self, credentials: MailCredentials) -> None:
        validate_credentials(credentials)
        value = json.dumps({"address": credentials.address, "code": credentials.authorization_code})
        try:
            self._storage().set_password(self.SERVICE, self.ACCOUNT, value)
        except Exception:
            raise RuntimeError("邮箱凭据保存失败，请检查 Windows 凭据管理器；未写入明文文件。") from None

    def get(self) -> MailCredentials | None:
        try:
            value = self._storage().get_password(self.SERVICE, self.ACCOUNT)
            if value is None:
                return None
            data = json.loads(value)
            credentials = MailCredentials(data["address"], data["code"])
            validate_credentials(credentials)
            return credentials
        except Exception:
            raise RuntimeError("无法读取已保存的邮箱凭据，请重新保存或检查 Windows 凭据管理器。") from None

    def clear(self) -> None:
        try:
            backend = self._storage()
            if backend.get_password(self.SERVICE, self.ACCOUNT) is not None:
                backend.delete_password(self.SERVICE, self.ACCOUNT)
        except Exception:
            raise RuntimeError("无法移除已保存的邮箱凭据，请检查 Windows 凭据管理器。") from None


def validate_credentials(credentials: MailCredentials) -> None:
    if not isinstance(credentials.address, str) or not re.fullmatch(r"[A-Za-z0-9._+\-]{1,100}@(?:stu\.)?pku\.edu\.cn", credentials.address):
        raise ValueError("请填写完整的北大邮箱地址。")
    code = credentials.authorization_code
    if not isinstance(code, str) or not 1 <= len(code) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in code):
        raise ValueError("客户端授权码不能为空，且不能含空格、换行或非 ASCII 字符。")


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
