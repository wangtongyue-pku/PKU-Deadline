"""Stage 7 read-only e-mail adapter contract and an offline mock mailbox.

The real connection (7B) will use the standard library's ``imaplib`` against the
verified endpoint. This module defines what any adapter may do: **list folders,
fetch incrementally, close**. There is deliberately no method to mark, delete,
move, send, or upload anything — a read-only guarantee that is enforced by the
shape of the contract, not by discipline alone.

Verified endpoint facts (see docs/stage7_email_research.md):

- The student mailbox (@stu.pku.edu.cn) is hosted on NetEase enterprise mail
  (MX -> mxmail.netease.com; webmail at mail.stu.pku.edu.cn).
- The IMAP endpoint that passes full TLS verification is ``imap.qiye.163.com:993``.
  The university-specific host ``imap.stu.pku.edu.cn:993`` is reachable but serves
  the ``*.qiye.163.com`` certificate, so connecting to it would require disabling
  hostname verification — not acceptable, therefore not used.
- Per NetEase's official client guide: port 993 with SSL/TLS, auth = password,
  username = the full e-mail address.
"""

from __future__ import annotations

from typing import Protocol

from pku_deadline_agent.email_models import EmailFetchResult

# Verified against DNS and a live TLS handshake on 2026-10-11 (no login performed).
IMAP_HOST = "imap.qiye.163.com"
IMAP_PORT = 993
MAIL_DOMAIN = "stu.pku.edu.cn"
WEBMAIL_URL = "https://mail.stu.pku.edu.cn/"

DEFAULT_FOLDER = "INBOX"

# Hard ceiling for one fetch batch: bounds work per run, keeps a runaway mailbox
# from turning one sync into a bulk download.
DEFAULT_FETCH_LIMIT = 50

# Only these operations may ever appear in an adapter's operation log. The mock
# records every call; the real adapter must do the same so tests can prove that
# nothing but reads happened.
ALLOWED_OPERATIONS = ("list_folders", "fetch_since", "close")

# IMAP commands that must never be issued by this application. Kept here so the
# real adapter (7B) can assert against the same list it is tested with.
FORBIDDEN_IMAP_OPERATIONS = (
    "STORE",       # would change \Seen / flags
    "EXPUNGE",     # would delete
    "APPEND",      # would upload mail
    "COPY",        # would move mail between folders
    "DELETE",      # would delete a mailbox
    "CREATE",      # would create a mailbox
    "RENAME",      # would rename a mailbox
    "SUBSCRIBE",
    "UNSUBSCRIBE",
)


class EmailAdapterError(Exception):
    """Base class for adapter failures; never reported as 'no mail'."""


class EmailAuthRequired(EmailAdapterError):
    """Credentials missing, wrong, or IMAP disabled for the account."""


class EmailNetworkError(EmailAdapterError):
    """Host unreachable, TLS failure, or server-side 5xx-like condition."""


class EmailLayoutChanged(EmailAdapterError):
    """Server answered but not in the shape the adapter understands."""


class EmailAdapter(Protocol):
    """What any mail adapter may do — and nothing else.

    Implementations must be safe to discard without side effects beyond closing
    the connection: reading must not change any flag on any message (the real
    adapter fetches with ``BODY.PEEK`` so ``\\Seen`` is never set).
    """

    def list_folders(self) -> tuple[str, ...]:
        """Subscribed folders the user may read; INBOX first when present."""
        ...

    def fetch_since(
        self, folder: str, since_uid: int, *, limit: int = DEFAULT_FETCH_LIMIT
    ) -> EmailFetchResult:
        """Messages in ``folder`` with UID greater than ``since_uid``.

        Returns an :class:`EmailFetchResult` in ascending UID order, including
        per-message warnings and any parse rejections. Raises EmailAdapterError
        subclasses on failure; never returns an empty batch as a way to signal
        an error.
        """
        ...

    def close(self) -> None:
        """Release the connection. Safe to call more than once."""
        ...


class MockEmailAdapter:
    """An offline mailbox for tests and UI work.

    Built from fictional raw RFC-822 bytes; no network, no credentials. Every
    call is recorded in ``operations`` so tests can prove that only reads happen.
    """

    def __init__(self, folders: dict[str, dict[int, bytes]], *, uidvalidity: int = 1) -> None:
        self._folders: dict[str, dict[int, bytes]] = {
            name: dict(messages) for name, messages in folders.items()
        }
        self.uidvalidity = uidvalidity
        self.operations: list[str] = []
        self.closed = False

    # -- contract ---------------------------------------------------------

    def list_folders(self) -> tuple[str, ...]:
        self.operations.append("list_folders")
        names = tuple(self._folders)
        if DEFAULT_FOLDER in self._folders:
            names = (DEFAULT_FOLDER, *(n for n in names if n != DEFAULT_FOLDER))
        return names

    def fetch_since(
        self, folder: str, since_uid: int, *, limit: int = DEFAULT_FETCH_LIMIT
    ) -> "tuple[tuple[RawEmailRecord, ...], int, int, bool]":
        self.operations.append("fetch_since")
        if self.closed:
            raise EmailAdapterError("会话已关闭。")
        if folder not in self._folders:
            raise EmailLayoutChanged(f"文件夹不存在: {folder!r}")
        # Import here to keep the import graph light and avoid cycles.
        from pku_deadline_agent.email_parse import parse_raw_email

        uids = sorted(uid for uid in self._folders[folder] if uid > since_uid)
        batch, rejected = uids[:limit], uids[limit:]
        records: list[RawEmailRecord] = []
        warnings: list[str] = []
        rejections: list[str] = []
        for uid in batch:
            try:
                record, record_warnings = parse_raw_email(
                    self._folders[folder][uid],
                    uid=uid,
                    folder=folder,
                    uidvalidity=self.uidvalidity,
                )
            except ValueError as error:
                # Counted and surfaced, never silent. highest_uid still advances
                # past it: one permanently broken message must not wedge the
                # incremental cursor forever, and the rejection is part of the
                # result so the batch cannot be treated as clean.
                rejections.append(f"UID {uid}: {error}")
                continue
            records.append(record)
            warnings.extend(record_warnings)
        highest = batch[-1] if batch else max(0, since_uid)
        return EmailFetchResult(
            folder=folder,
            uidvalidity=self.uidvalidity,
            records=tuple(records),
            highest_uid=highest,
            has_more=bool(rejected),
            rejections=tuple(rejections),
            warnings=tuple(warnings),
        )

    def close(self) -> None:
        self.operations.append("close")
        self.closed = True
