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

from dataclasses import dataclass, field, replace
from datetime import timedelta

from pku_deadline_agent.email_adapter import (
    DEFAULT_FETCH_LIMIT,
    DEFAULT_FOLDER,
    EmailAdapterError,
)
from pku_deadline_agent.email_credentials import CredentialStore
from pku_deadline_agent.email_intel import analyze_email
from pku_deadline_agent.email_models import EmailAnalysis
from pku_deadline_agent.time_utils import beijing_now

INITIAL_DAYS = 7
INITIAL_MAX = 30


@dataclass(frozen=True)
class MailboxState:
    uidvalidity: int
    highest_uid: int
    failed_uids: tuple[int, ...] = ()   # parse failures recorded for retry


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
    mode: str                      # "initial", "incremental", "retry" or "bounded"
    uidvalidity: int
    highest_uid: int
    analyses: tuple[EmailAnalysis, ...] = field(repr=False)
    has_more: bool
    rejections: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    retried_uids: tuple[int, ...] = field(default_factory=tuple)
    still_failed_uids: tuple[int, ...] = field(default_factory=tuple)
    window_hours: float | None = None   # set only for a bounded test pass
    excluded_older_count: int = 0    # dropped locally as outside the stated window

    @property
    def describe_scope(self) -> str:
        """What this pass actually asked the server for, in one line."""
        if self.mode == "bounded":
            hours = int(self.window_hours) if self.window_hours else 0
            text = f"受限读取：最近 {hours} 小时、最多 {len(self.analyses)} 封"
            if self.excluded_older_count:
                text += f"（另有 {self.excluded_older_count} 封超出该范围，已在本地剔除）"
            return text
        return f"模式 {self.mode}｜读取 {len(self.analyses)} 封"

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

    def sync(self, folder: str = DEFAULT_FOLDER, *, retry_failed: bool = True,
             window_hours: float | None = None,
             max_messages: int | None = None) -> EmailSyncReport:
        """One pass. Failed-parse messages from earlier passes are retried first
        (recorded, never silently skipped), then the normal incremental run.

        Passing ``window_hours`` switches to a **bounded** pass instead: it reads
        at most ``max_messages`` messages from that many hours back, ignores the
        stored cursor, and does not touch it afterwards. That is what a first
        real-mailbox test needs — a scope the user can state in one sentence —
        while the normal modes keep their own fixed limits (initial: 7 days / 30
        messages; incremental: by UID).
        """
        if window_hours is not None:
            return self._run_bounded(folder, window_hours, max_messages)
        state = self._state.get(folder)
        if state is None:
            report = self._run_initial(folder)
        else:
            report = self._run_incremental(folder, state)
        if retry_failed and state is not None and state.failed_uids:
            report = self._retry(folder, report, state.failed_uids)
        return report

    def reset_state(self, folder: str = DEFAULT_FOLDER) -> None:
        """Forget the cursor (user action); the next sync re-runs initial."""
        self._state.reset(folder)

    def _run_bounded(self, folder: str, window_hours: float,
                     max_messages: int | None) -> EmailSyncReport:
        """A pass with an explicit, one-off window and message cap.

        Deliberately leaves the incremental cursor untouched: this is a test of a
        stated scope, not the beginning of ongoing synchronisation, and it must
        not affect what a later normal sync reads.

        Two independent guards apply, because they answer different questions:
        the server-side ``SINCE`` bounds which messages are *asked for*, and the
        per-message check below drops anything outside the window that the server
        returned anyway (``SINCE`` matches on the arrival date at day
        granularity, so it can hand back a message sent slightly earlier).
        """
        limit = max(1, int(max_messages)) if max_messages else self._batch_limit
        since = beijing_now() - timedelta(hours=float(window_hours))
        adapter = self._adapter_factory()
        try:
            result = adapter.fetch_since(folder, 0, limit=limit, since_date=since)
        finally:
            adapter.close()

        inside = tuple(
            record for record in result.records
            if record.sent_at is None or record.sent_at >= since
        )
        excluded = len(result.records) - len(inside)
        # The exclusion is *not* a warning: the user asked for this window, so
        # dropping an older message is the scope working, not a data problem.
        # A warning here would mark the batch untrustworthy and block the whole
        # import plan — including the messages that are inside the window.
        filtered = replace(result, records=inside)
        return self._finish(folder, filtered, mode="bounded",
                            window_hours=window_hours, keep_state=True,
                            excluded_older_count=excluded)

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

    def _finish(self, folder: str, result, mode: str,
                window_hours: float | None = None,
                keep_state: bool = False,
                excluded_older_count: int = 0) -> EmailSyncReport:
        analyses = tuple(analyze_email(record) for record in result.records)
        if keep_state:
            # A bounded test pass observes the mailbox; it must not move the
            # cursor or inherit failures into the ongoing state.
            return EmailSyncReport(
                folder=folder,
                mode=mode,
                uidvalidity=result.uidvalidity,
                highest_uid=result.highest_uid,
                analyses=analyses,
                has_more=result.has_more,
                rejections=tuple(result.rejections),
                warnings=tuple(result.warnings),
                window_hours=window_hours,
                excluded_older_count=excluded_older_count,
            )
        # Cursor advances past failures, but they are *recorded* on the state so
        # a later pass retries them: one bad message never blocks the others,
        # and none is silently lost.
        previous_failed = (self._state.get(folder).failed_uids
                           if self._state.get(folder) else ())
        merged_failed = tuple(dict.fromkeys(
            (*previous_failed, *result.rejected_uids)
        )) if previous_failed or result.rejected_uids else ()
        succeeded_now = {record.uid for record in result.records}
        merged_failed = tuple(uid for uid in merged_failed if uid not in succeeded_now)
        self._state.set(folder, MailboxState(
            result.uidvalidity, result.highest_uid, failed_uids=merged_failed
        ))
        return EmailSyncReport(
            folder=folder,
            mode=mode,
            uidvalidity=result.uidvalidity,
            highest_uid=result.highest_uid,
            analyses=analyses,
            has_more=result.has_more,
            rejections=tuple(result.rejections),
            warnings=tuple(result.warnings),
            still_failed_uids=merged_failed,
        )

    def _retry(self, folder: str, report: EmailSyncReport, failed_uids) -> EmailSyncReport:
        """Re-read the recorded failures; successes leave the retry list."""
        adapter = self._adapter_factory()
        try:
            result = adapter.fetch_uids(folder, failed_uids)
        finally:
            adapter.close()

        retried = [record.uid for record in result.records]
        still = [uid for uid in failed_uids if uid not in retried]
        state = self._state.get(folder)
        if state is not None:
            self._state.set(folder, MailboxState(
                state.uidvalidity, state.highest_uid, failed_uids=tuple(still)
            ))
        analyses = tuple(analyze_email(record) for record in result.records)
        warnings = (*report.warnings, *result.warnings)
        if still:
            warnings = (*warnings, f"仍有 {len(still)} 封邮件解析失败，已记录待重试：{still}。")
        return EmailSyncReport(
            folder=report.folder,
            mode="retry",
            uidvalidity=result.uidvalidity,
            highest_uid=report.highest_uid,
            analyses=(*report.analyses, *analyses),
            has_more=report.has_more,
            rejections=(*report.rejections, *result.rejections),
            warnings=warnings,
            retried_uids=tuple(retried),
            still_failed_uids=tuple(still),
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
