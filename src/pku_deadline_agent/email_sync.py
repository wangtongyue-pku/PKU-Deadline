"""Stage 7B: the e-mail sync service — increment, UID resets, initial limits.

The service owns the *state* (per-folder UIDVALIDITY + highest UID seen); the
adapter owns the *connection*. They are separate so tests can drive the service
with the mock adapter, and 7D can persist the state without touching IMAP code.

Initial-sync constraints set by the user: **INBOX only, last 7 days, at most 30
messages per batch**. Subsequent syncs are UID-incremental with no date filter.
A UIDVALIDITY change means the server renumbered mail: the state is reset, the
user is warned, and the next pass re-runs with the initial constraints — mail is
never assumed lost, and nothing on the local side is deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from pku_deadline_agent.email_adapter import (
    DEFAULT_FETCH_LIMIT,
    DEFAULT_FOLDER,
    EmailAdapterError,
)
from pku_deadline_agent.email_credentials import CredentialStore
from pku_deadline_agent.email_models import EmailAnalysis
from pku_deadline_agent.email_parse import analyze_email
from pku_deadline_agent.time_utils import beijing_now

INITIAL_DAYS = 7
INITIAL_MAX = 30


@dataclass(frozen=True)
class MailboxState:
    uidvalidity: int
    highest_uid: int


class MailboxStateStore:
    """In-memory state. Persistence (7D) will implement this protocol too."""

    def __init__(self) -> None:
        self._states: dict[str, MailboxState] = {}

    def get(self, folder: str) -> MailboxState | None:
        return self._states.get(folder)

    def set(self, folder: str, state: MailboxState) -> None:
        self._states[folder] = state

    def reset(self, folder: str) -> None:
        self._states.pop(folder, None)


@dataclass(frozen=True)
class EmailSyncReport:
    """What one sync pass produced — shown to the user before anything is written."""

    folder: str
    mode: str                      # "initial" or "incremental"
    uidvalidity: int
    highest_uid: int
    analyses: tuple[EmailAnalysis, ...]
    has_more: bool
    rejections: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def trustworthy(self) -> bool:
        return not self.rejections and not self.warnings

    @property
    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for analysis in self.analyses:
            counts[analysis.category] = counts.get(analysis.category, 0) + 1
        return counts

    @property
    def deadline_count(self) -> int:
        return sum(len(analysis.deadlines) for analysis in self.analyses)


class EmailSyncService:
    """One sync pass at a time. Nothing here writes tasks; confirmation stays
    with the user, and the service never deletes local data."""

    def __init__(
        self,
        adapter_factory,                 # () -> EmailAdapter (credentials resolved inside)
        state_store: MailboxStateStore | None = None,
        *,
        initial_days: int = INITIAL_DAYS,
        initial_max: int = INITIAL_MAX,
        batch_limit: int = DEFAULT_FETCH_LIMIT,
    ) -> None:
        self._adapter_factory = adapter_factory
        self._state = state_store or MailboxStateStore()
        self._initial_days = initial_days
        self._initial_max = initial_max
        self._batch_limit = batch_limit

    # -- public -----------------------------------------------------------

    def sync(self, folder: str = DEFAULT_FOLDER) -> EmailSyncReport:
        state = self._state.get(folder)
        if state is None:
            return self._run_initial(folder)
        return self._run_incremental(folder, state)

    def reset_state(self, folder: str = DEFAULT_FOLDER) -> None:
        """Forget the cursor (user action); the next sync re-runs initial."""
        self._state.reset(folder)

    # -- internals --------------------------------------------------------

    def _run_initial(self, folder: str) -> EmailSyncReport:
        since = beijing_now() - timedelta(days=self._initial_days)
        adapter = self._adapter_factory()
        try:
            result = adapter.fetch_since(
                folder, 0, limit=min(self._initial_max, self._batch_limit), since_date=since
            )
        finally:
            adapter.close()
        return self._finish(folder, result, mode="initial")

    def _run_incremental(self, folder: str, state: MailboxState) -> EmailSyncReport:
        adapter = self._adapter_factory()
        try:
            result = adapter.fetch_since(folder, state.highest_uid, limit=self._batch_limit)
        finally:
            adapter.close()

        if result.uidvalidity != state.uidvalidity:
            # Server renumbered the mailbox. Treat as fresh: warn loudly, reset,
            # and re-run with the initial constraints so scope stays bounded.
            self._state.reset(folder)
            report = self._run_initial(folder)
            report = EmailSyncReport(
                folder=report.folder,
                mode=report.mode,
                uidvalidity=report.uidvalidity,
                highest_uid=report.highest_uid,
                analyses=report.analyses,
                has_more=report.has_more,
                rejections=report.rejections,
                warnings=(
                    "UIDVALIDITY 已变化（服务器可能重建了邮箱）；增量游标已重置，"
                    "本次按初始同步范围重新读取。",
                    *report.warnings,
                ),
            )
            return report
        return self._finish(folder, result, mode="incremental")

    def _finish(self, folder: str, result, mode: str) -> EmailSyncReport:
        analyses = tuple(analyze_email(record) for record in result.records)
        self._state.set(folder, MailboxState(result.uidvalidity, result.highest_uid))
        return EmailSyncReport(
            folder=folder,
            mode=mode,
            uidvalidity=result.uidvalidity,
            highest_uid=result.highest_uid,
            analyses=analyses,
            has_more=result.has_more,
            rejections=tuple(result.rejections),
            warnings=tuple(result.warnings),
        )


def adapter_factory_from(
    credential_store: CredentialStore,
    adapter_class,
    *,
    host: str,
    port: int,
    timeout: float = 30.0,
    connection_factory=None,
):
    """Build an adapter factory bound to a credential store.

    Credentials are fetched at sync time and handed only to the adapter; they
    are never copied into the service or any report.
    """

    def factory():
        credentials = credential_store.get()
        if credentials is None:
            raise EmailAdapterError(
                "尚未提供邮箱凭据：请先在本地输入校园网账号与客户端授权码（7D 页面或终端助手）。"
            )
        return adapter_class(
            credentials, host=host, port=port, timeout=timeout,
            connection_factory=connection_factory,
        )

    return factory
