"""The daily 30-message ceiling includes retries and a UIDVALIDITY reset."""

from dataclasses import replace

from pku_deadline_agent.email_models import EmailFetchResult
from pku_deadline_agent.email_sync import EmailSyncService, MailboxStateStore, MailboxState
from test_email_ai_flow import message


def test_retry_budget_does_not_drop_untried_or_new_failed_uids():
    state = MailboxStateStore()
    state.set("INBOX", MailboxState(1, 40, tuple(range(1, 36))))
    calls = []
    class Adapter:
        def fetch_since(self, folder, since_uid, **kw):
            calls.append(("new", kw["limit"]))
            return EmailFetchResult(folder="INBOX", uidvalidity=1, records=tuple(message(uid=uid) for uid in range(41, 65)), highest_uid=65, has_more=False, rejections=("fictional parse failure",), rejected_uids=(65,))
        def fetch_uids(self, folder, uids):
            calls.append(("retry", tuple(uids)))
            return EmailFetchResult(folder="INBOX", uidvalidity=1, records=tuple(message(uid=uid) for uid in uids), highest_uid=max(uids), has_more=False)
        def close(self): pass
    report = EmailSyncService(Adapter, state_store=state, batch_limit=30).sync(max_total_messages=30)
    assert calls == [("new", 30), ("retry", (1, 2, 3, 4, 5))]
    assert len(report.analyses) == 29
    assert state.get("INBOX").failed_uids == (*range(6, 36), 65)


def test_full_new_batch_leaves_all_retries_for_next_pass():
    state = MailboxStateStore()
    state.set("INBOX", MailboxState(1, 40, (1, 2, 3)))
    class Adapter:
        def fetch_since(self, folder, since_uid, **kw):
            return EmailFetchResult(folder="INBOX", uidvalidity=1, records=tuple(message(uid=uid) for uid in range(41, 71)), highest_uid=70, has_more=False)
        def fetch_uids(self, *args): raise AssertionError("read budget exhausted")
        def close(self): pass
    report = EmailSyncService(Adapter, state_store=state).sync(max_total_messages=30)
    assert len(report.analyses) == 30
    assert state.get("INBOX").failed_uids == (1, 2, 3)


def test_uid_reset_cannot_double_read_budget_or_reuse_old_retry_ids():
    state = MailboxStateStore()
    state.set("INBOX", MailboxState(1, 40, (1, 2, 3)))
    calls = []
    class Adapter:
        def fetch_since(self, folder, since_uid, **kw):
            calls.append((since_uid, kw["limit"]))
            return EmailFetchResult(folder="INBOX", uidvalidity=2, records=tuple(replace(message(uid=uid), uidvalidity=2) for uid in range(41, 71)), highest_uid=70, has_more=False)
        def fetch_uids(self, *args): raise AssertionError("old UID identity must not be reused")
        def close(self): pass
    report = EmailSyncService(Adapter, state_store=state).sync(max_total_messages=30)
    assert calls == [(40, 30)]
    assert not report.trustworthy and report.warnings
    assert state.get("INBOX") is None
