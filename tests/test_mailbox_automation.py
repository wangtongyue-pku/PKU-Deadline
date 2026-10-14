"""All credentials and mailbox contents fictional; no real storage/network/jobs."""

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest

from pku_deadline_agent.email_credentials import MailCredentials, WindowsCredentialStore
from pku_deadline_agent.email_demo import demo_mailbox
from pku_deadline_agent.email_sync import MailboxState
from pku_deadline_agent.mailbox_cache import MailboxCache, account_key, sync_mailbox
from pku_deadline_agent.time_utils import beijing_now


class FakeVault:
    def __init__(self): self.values = {}
    def set_password(self, service, account, value): self.values[service, account] = value
    def get_password(self, service, account): return self.values.get((service, account))
    def delete_password(self, service, account): self.values.pop((service, account), None)


class FakeProtection:
    def __init__(self): self.values = {}
    def protect(self, value):
        token = b"fictional-encrypted-" + str(len(self.values)).encode()
        self.values[token] = value
        return token
    def unprotect(self, value): return self.values[value]


@pytest.fixture
def cache(tmp_path): return MailboxCache(tmp_path / "mailbox.sqlite3", protector=FakeProtection())


CREDS = MailCredentials("fictional@stu.pku.edu.cn", "FICTIONAL_AUTH_CODE")


def test_credentials_survive_new_instance_without_plaintext_file(tmp_path):
    backend = FakeVault()
    first = WindowsCredentialStore(user_confirmed_persistence=True, backend=backend)
    first.save(CREDS)
    second = WindowsCredentialStore(user_confirmed_persistence=True, backend=backend)
    assert second.get() == CREDS
    assert list(tmp_path.iterdir()) == []
    assert CREDS.authorization_code not in repr(second)
    assert CREDS.authorization_code not in repr(CREDS)
    second.clear()
    assert first.get() is None


@pytest.mark.parametrize("address,code", [("not-mail", "fake"), ("fictional@evil.invalid", "fake"), (CREDS.address, "code\n"), (CREDS.address, "")])
def test_invalid_credentials_never_reach_vault(address, code):
    backend = FakeVault()
    with pytest.raises(ValueError):
        WindowsCredentialStore(user_confirmed_persistence=True, backend=backend).save(MailCredentials(address, code))
    assert not backend.values


def test_broken_vault_error_does_not_echo_secret():
    class Broken(FakeVault):
        def set_password(self, *args): raise RuntimeError(CREDS.authorization_code)
    with pytest.raises(RuntimeError) as error:
        WindowsCredentialStore(user_confirmed_persistence=True, backend=Broken()).save(CREDS)
    assert CREDS.authorization_code not in str(error.value)


def test_daily_sync_persists_cursor_and_board_no_ai_or_task_writes(cache):
    account = account_key(CREDS.address)
    cache.configure(account, enabled=True)
    now = beijing_now()
    report = sync_mailbox(cache, CREDS, demo_mailbox, now=now)
    assert report.analyses
    loaded, state = cache.load(account)
    assert loaded == report and isinstance(state, MailboxState)
    assert CREDS.address.encode() not in cache.path.read_bytes()
    assert report.analyses[0].record.subject.encode() not in cache.path.read_bytes()
    def forbidden(): raise AssertionError("must not connect before next due")
    assert sync_mailbox(cache, CREDS, forbidden, now=now + timedelta(hours=23)) is None
    after = sync_mailbox(cache, CREDS, demo_mailbox, now=now + timedelta(hours=24))
    assert len(after.analyses) == len(report.analyses)
    assert not cache.path.with_name("deadlines.sqlite3").exists()
    assert not cache.path.with_name("ai_usage.sqlite3").exists()


def test_concurrent_jobs_claim_only_once(cache):
    account = account_key(CREDS.address)
    cache.configure(account, enabled=True)
    now = beijing_now()
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: cache.claim(account, now=now), range(4)))
    assert sum(item is not None for item in claims) == 1


def test_network_failure_preserves_snapshot_and_cursor(cache):
    from pku_deadline_agent.email_adapter import EmailNetworkError
    first = sync_mailbox(cache, CREDS, demo_mailbox, force=True)
    snapshot = cache.load(account_key(CREDS.address))
    def broken(): raise EmailNetworkError("fictional private error")
    with pytest.raises(RuntimeError, match="网络失败"):
        sync_mailbox(cache, CREDS, broken, force=True)
    assert cache.load(account_key(CREDS.address)) == snapshot
    assert first.analyses


def test_auth_failure_disables_automatic_retries(cache):
    from pku_deadline_agent.email_adapter import EmailAuthRequired
    account = account_key(CREDS.address)
    cache.configure(account, enabled=True)
    def broken(): raise EmailAuthRequired("fictional secret")
    with pytest.raises(RuntimeError, match="授权失效"):
        sync_mailbox(cache, CREDS, broken, force=True)
    assert cache.info(account)["enabled"] == 0
    assert sync_mailbox(cache, CREDS, broken) is None


def test_unknown_cache_or_config_change_cannot_overwrite(cache):
    account = account_key(CREDS.address)
    token = cache.claim(account, force=True)
    cache.configure(account, enabled=False)
    from pku_deadline_agent.email_sync import EmailSyncService
    report = EmailSyncService(demo_mailbox).sync()
    with pytest.raises(ValueError): cache.finish(account, token, report, None)
    assert cache.load(account) == (None, None)


def test_background_scheduler_command_contains_no_credentials(tmp_path):
    from pku_deadline_agent.email_schedule import WindowsMailSchedule
    calls = []
    (tmp_path / ".venv/Scripts").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / ".venv/Scripts/pythonw.exe").touch()
    (tmp_path / "scripts/mailbox_job.py").touch()
    job = WindowsMailSchedule(project_root=tmp_path, task_path=tmp_path / "private/tasks.sqlite3", runner=lambda *a, **kw: calls.append((a, kw)))
    job.enable()
    job.disable()
    arguments = calls[0][0][0]
    assert "/IT" in arguments and "LIMITED" in arguments and "HOURLY" in arguments
    assert "--task-database" in arguments[arguments.index("/TR") + 1]
    assert CREDS.authorization_code not in str(calls)
    assert CREDS.address not in str(calls)
    assert calls[0][1]["capture_output"] is True


def test_disabled_background_job_does_not_even_read_vault(tmp_path, monkeypatch):
    from pku_deadline_agent.email_schedule import main
    def forbidden(*args, **kwargs): raise AssertionError("must not read secrets")
    monkeypatch.setattr("pku_deadline_agent.email_schedule.WindowsCredentialStore", forbidden)
    assert main(["--task-database", str(tmp_path / "tasks.sqlite3")]) == 0


def test_unreadable_encrypted_cache_is_not_silently_reset(cache):
    sync_mailbox(cache, CREDS, demo_mailbox, force=True)
    original = cache.path.read_bytes()
    cache.protector = FakeProtection()
    with pytest.raises(RuntimeError): cache.load(account_key(CREDS.address))
    assert cache.path.read_bytes() == original
