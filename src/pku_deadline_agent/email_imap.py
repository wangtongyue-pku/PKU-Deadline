"""Stage 7B: the real read-only IMAP adapter (standard library ``imaplib``).

Read-only guarantees, enforced here rather than promised:

- Mailboxes are opened with ``EXAMINE`` (``select(readonly=True)``), never
  ``SELECT`` — the server is told this session must not change state.
- Messages are fetched with ``BODY.PEEK[]``, so the ``\\Seen`` flag is never set.
- No ``STORE`` / ``EXPUNGE`` / ``APPEND`` / ``COPY`` / mailbox-management
  command exists in this file; the contract test scans for them, and the fake
  IMAP server in the test suite fails the suite if one is ever issued.

Failure taxonomy (requirement 9): authentication problems raise
:class:`EmailAuthRequired`, connectivity problems :class:`EmailNetworkError`,
unexpected server answers :class:`EmailLayoutChanged`. An empty mailbox is none
of these — it is a normal, clean result. Error messages never contain the
authorization code.
"""

from __future__ import annotations

import imaplib
import re
import socket
import ssl
from datetime import datetime
from typing import Callable

from pku_deadline_agent.email_adapter import (
    DEFAULT_FETCH_LIMIT,
    EmailAdapterError,
    EmailAuthRequired,
    EmailLayoutChanged,
    EmailNetworkError,
)
from pku_deadline_agent.email_credentials import MailCredentials
from pku_deadline_agent.email_models import EmailFetchResult
from pku_deadline_agent.time_utils import normalize_datetime

MAX_LINE_INFLUENCE = 200  # how much server text may be echoed in an error


def _brief(text: str) -> str:
    text = " ".join(str(text).split())
    return text[:MAX_LINE_INFLUENCE]


def _default_connection_factory(host: str, port: int, timeout: float):
    return imaplib.IMAP4_SSL(host, port, timeout=timeout)


class RealImapAdapter:
    """Read-only adapter against the verified NetEase/PKI endpoint."""

    def __init__(
        self,
        credentials: MailCredentials,
        *,
        host: str,
        port: int,
        timeout: float = 30.0,
        connection_factory: Callable[[str, int, float], imaplib.IMAP4] | None = None,
    ) -> None:
        self._credentials = credentials
        self._host = host
        self._port = port
        self._timeout = timeout
        self._factory = connection_factory or _default_connection_factory
        self._imap: imaplib.IMAP4 | None = None
        self.operations: list[str] = []
        self.closed = False

    # -- helpers ----------------------------------------------------------

    def _connection(self) -> imaplib.IMAP4:
        if self._imap is None:
            try:
                self._imap = self._factory(self._host, self._port, self._timeout)
                self._imap.login(self._credentials.address, self._credentials.authorization_code)
            except imaplib.IMAP4.error as error:
                self._imap = None
                text = _brief(error)
                if any(word in text.upper() for word in ("LOGIN", "AUTH", "PASSWORD", "AUTHORIZATION")):
                    raise EmailAuthRequired(
                        "登录被拒绝：请核对校园网账号与客户端授权码（不是网页登录密码），"
                        f"并确认已在网页端生成授权码。服务器返回：{text}"
                    ) from None
                raise EmailLayoutChanged(f"服务器拒绝了连接请求：{text}") from None
            except ssl.SSLError as error:
                self._imap = None
                raise EmailNetworkError(f"TLS 连接失败：{_brief(error)}") from None
            except (OSError, socket.timeout) as error:
                self._imap = None
                raise EmailNetworkError(
                    f"无法连接到邮件服务器（{error.__class__.__name__}）。"
                ) from None
        return self._imap

    @staticmethod
    def _raise_for_status(typ: bytes, what: str) -> None:
        if typ != "OK":
            raise EmailLayoutChanged(f"{what} 未被服务器接受（{_brief(typ)}）。")

    @staticmethod
    def _uidvalidity(imap: imaplib.IMAP4) -> int:
        typ, data = imap.response("UIDVALIDITY")
        if typ != "UIDVALIDITY" or not data or data[0] is None:
            raise EmailLayoutChanged("服务器未返回 UIDVALIDITY，无法安全增量。")
        try:
            return int(data[0])
        except (TypeError, ValueError) as error:
            raise EmailLayoutChanged("UIDVALIDITY 不是数字。") from error

    @staticmethod
    def _search_uids(imap: imaplib.IMAP4, criteria: tuple[str, ...]) -> list[int]:
        typ, data = imap.uid("SEARCH", None, *criteria)
        RealImapAdapter._raise_for_status(typ, "UID SEARCH")
        blob = data[0] or b""
        return sorted(int(piece) for piece in blob.split())

    @staticmethod
    def _fetch_raw(imap: imaplib.IMAP4, uid: int) -> bytes | None:
        typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        RealImapAdapter._raise_for_status(typ, f"UID FETCH {uid}")
        for element in data or ():
            if isinstance(element, tuple) and len(element) >= 2 and isinstance(element[1], (bytes, bytearray)):
                return bytes(element[1])
        return None  # server answered OK but sent no body — treated as rejection upstream

    # -- contract ---------------------------------------------------------

    def list_folders(self) -> tuple[str, ...]:
        self.operations.append("list_folders")
        imap = self._connection()
        try:
            typ, data = imap.list()
            self._raise_for_status(typ, "LIST")
        except (imaplib.IMAP4.error, OSError, socket.timeout) as error:
            raise self._as_adapter_error(error) from None
        names: list[str] = []
        for line in data or ():
            text = line.decode("ascii", "replace") if isinstance(line, bytes) else str(line)
            matches = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
            if matches:
                names.append(matches[-1])  # the last quoted token is the folder name
        if "INBOX" in names:
            names = ["INBOX", *(n for n in names if n != "INBOX")]
        return tuple(names)

    def fetch_since(
        self,
        folder: str,
        since_uid: int,
        *,
        limit: int = DEFAULT_FETCH_LIMIT,
        since_date: datetime | None = None,
    ) -> EmailFetchResult:
        self.operations.append("fetch_since")
        imap = self._connection()
        try:
            typ, _data = imap.select(folder, readonly=True)  # EXAMINE — read-only
            self._raise_for_status(typ, f"EXAMINE {folder}")
            uidvalidity = self._uidvalidity(imap)

            if since_uid > 0:
                criteria = ("UID", f"{since_uid + 1}:*")
            elif since_date is not None:
                # Time2Internaldate needs an aware datetime and returns a quoted
                # "dd-Mon-yyyy hh:mm:ss +zzzz" string; only the date part
                # matters to SINCE.
                internal = imaplib.Time2Internaldate(normalize_datetime(since_date))
                criteria = ("SINCE", str(internal).strip('"').split()[0])
            else:
                criteria = ("ALL",)
            uids = [u for u in self._search_uids(imap, criteria) if u > since_uid]

            batch, beyond = uids[:limit], uids[limit:]
            records, warnings, rejections = [], [], []
            from pku_deadline_agent.email_parse import parse_raw_email

            for uid in batch:
                try:
                    raw = self._fetch_raw(imap, uid)
                    if raw is None:
                        rejections.append(f"UID {uid}: 服务器未返回邮件内容。")
                        continue
                    record, record_warnings = parse_raw_email(
                        raw, uid=uid, folder=folder, uidvalidity=uidvalidity
                    )
                except ValueError as error:
                    rejections.append(f"UID {uid}: {error}")
                    continue
                records.append(record)
                warnings.extend(record_warnings)
        except imaplib.IMAP4.error as error:
            raise self._as_adapter_error(error) from None
        except (OSError, socket.timeout) as error:
            raise EmailNetworkError(
                f"与邮件服务器的连接中断（{error.__class__.__name__}）。"
            ) from None

        return EmailFetchResult(
            folder=folder,
            uidvalidity=uidvalidity,
            records=tuple(records),
            highest_uid=batch[-1] if batch else max(0, since_uid),
            has_more=bool(beyond),
            rejections=tuple(rejections),
            warnings=tuple(warnings),
            rejected_uids=tuple(
                uid for uid in batch
                if uid not in {record.uid for record in records}
            ),
        )

    def fetch_uids(self, folder: str, uids) -> EmailFetchResult:
        """Re-read exactly the named messages (retry path); read-only."""
        self.operations.append("fetch_uids")
        imap = self._connection()
        try:
            typ, _data = imap.select(folder, readonly=True)  # EXAMINE — read-only
            self._raise_for_status(typ, f"EXAMINE {folder}")
            uidvalidity = self._uidvalidity(imap)

            records, warnings, rejections = [], [], []
            from pku_deadline_agent.email_parse import parse_raw_email

            for uid in sorted(set(uids)):
                try:
                    raw = self._fetch_raw(imap, uid)
                    if raw is None:
                        rejections.append(f"UID {uid}: 服务器未返回邮件内容。")
                        continue
                    record, record_warnings = parse_raw_email(
                        raw, uid=uid, folder=folder, uidvalidity=uidvalidity
                    )
                except ValueError as error:
                    rejections.append(f"UID {uid}: {error}")
                    continue
                records.append(record)
                warnings.extend(record_warnings)
        except imaplib.IMAP4.error as error:
            raise self._as_adapter_error(error) from None
        except (OSError, socket.timeout) as error:
            raise EmailNetworkError(
                f"与邮件服务器的连接中断（{error.__class__.__name__}）。"
            ) from None

        requested = sorted(set(uids))
        return EmailFetchResult(
            folder=folder,
            uidvalidity=uidvalidity,
            records=tuple(records),
            highest_uid=max(requested) if requested else 0,
            has_more=False,
            rejections=tuple(rejections),
            warnings=tuple(warnings),
            rejected_uids=tuple(
                uid for uid in requested
                if uid not in {record.uid for record in records}
            ),
        )

    def close(self) -> None:
        self.operations.append("close")
        self.closed = True
        if self._imap is None:
            return
        try:
            self._imap.logout()
        except Exception:
            pass  # closing must never raise over a connection the server already dropped
        finally:
            self._imap = None

    # -- errors -----------------------------------------------------------

    @staticmethod
    def _as_adapter_error(error: Exception) -> EmailAdapterError:
        text = _brief(error)
        upper = text.upper()
        if any(word in upper for word in ("AUTH", "LOGIN", "PASSWORD", "AUTHORIZATION")):
            return EmailAuthRequired(f"登录状态失效或凭据错误：{text}")
        return EmailLayoutChanged(f"邮件服务器返回了意料之外的应答：{text}")
