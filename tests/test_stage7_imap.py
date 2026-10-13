"""Stage 7B tests: the real adapter against an in-process fake IMAP server.

The fake server speaks enough IMAP for ``imaplib`` and records every command it
receives, so the tests can prove wire-level behaviour: EXAMINE instead of
SELECT, BODY.PEEK instead of BODY[], LOGOUT on close, and never a STORE /
EXPUNGE / APPEND / COPY. No real mailbox, no network beyond 127.0.0.1.
"""

import imaplib
import socketserver
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from pku_deadline_agent.email_adapter import DEFAULT_FOLDER
from pku_deadline_agent.email_adapter import EmailAdapterError
from pku_deadline_agent.email_adapter import EmailAuthRequired
from pku_deadline_agent.email_adapter import EmailNetworkError
from pku_deadline_agent.email_credentials import (
    MailCredentials,
    SessionCredentialStore,
    WindowsCredentialStore,
)
from pku_deadline_agent.email_imap import RealImapAdapter
from pku_deadline_agent.email_sync import INITIAL_MAX, EmailSyncService, adapter_factory_from
from pku_deadline_agent.time_utils import BEIJING

FORBIDDEN_ON_WIRE = ("SELECT", "STORE", "EXPUNGE", "APPEND", "COPY", "DELETE", "CREATE", "RENAME")


def build_raw(subject, body, *, sender="jwc@pku.edu.cn", message_id, sent=None) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "student@stu.pku.edu.cn"
    message["Message-ID"] = message_id
    message["Date"] = format_datetime(sent or datetime(2026, 11, 5, 9, 0, tzinfo=BEIJING))
    message.set_content(body)
    return message.as_bytes()


class FakeImapHandler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        server.commands.append("<greeting>")
        self.request.sendall(b"* OK [CAPABILITY IMAP4rev1 UIDPLUS] Fake ready\r\n")
        selected = None
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("ascii", "replace").rstrip("\r\n")
            if not text:
                continue
            server.commands.append(text)
            parts = text.split()
            if len(parts) < 2:
                self.request.sendall(b"* BAD malformed\r\n")
                continue
            tag, command = parts[0], parts[1].upper()
            args = [p.strip('"') for p in parts[2:]]

            if command == "LOGIN":
                if (args[0], args[1]) == server.valid_credentials:
                    self.request.sendall(f"{tag} OK LOGIN done\r\n".encode())
                else:
                    self.request.sendall(
                        f"{tag} NO [AUTHENTICATIONFAILED] Login refused\r\n".encode()
                    )
            elif command == "CAPABILITY":
                self.request.sendall(b"* CAPABILITY IMAP4rev1 UIDPLUS\r\n")
                self.request.sendall(f"{tag} OK CAPABILITY done\r\n".encode())
            elif command == "LIST":
                for name in server.mailboxes:
                    self.request.sendall(
                        f'* LIST (\\HasNoChildren) "/" "{name}"\r\n'.encode()
                    )
                self.request.sendall(f"{tag} OK LIST done\r\n".encode())
            elif command == "EXAMINE":
                selected = args[0]
                messages = server.mailboxes.get(selected, {})
                server.selected.append(selected)
                self.request.sendall(f"* {len(messages)} EXISTS\r\n".encode())
                self.request.sendall(
                    f"* OK [UIDVALIDITY {server.uidvalidity}] UIDs valid\r\n".encode()
                )
                self.request.sendall(f"{tag} OK [READ-ONLY] EXAMINE completed\r\n".encode())
            elif command == "UID" and args and args[0].upper() == "SEARCH":
                uids = server.search(selected, args[1:])
                listing = " ".join(str(u) for u in uids)
                self.request.sendall(f"* SEARCH {listing}\r\n".encode())
                self.request.sendall(f"{tag} OK SEARCH done\r\n".encode())
            elif command == "UID" and args and args[0].upper() == "FETCH":
                uid = int(args[1])
                raw = server.mailboxes[selected][uid][0]
                self.request.sendall(
                    f"* {uid} FETCH (UID {uid} BODY[] {{{len(raw)}}}\r\n".encode() + raw + b")\r\n"
                )
                self.request.sendall(f"{tag} OK FETCH done\r\n".encode())
            elif command == "LOGOUT":
                self.request.sendall(b"* BYE Fake closing\r\n")
                self.request.sendall(f"{tag} OK LOGOUT done\r\n".encode())
                return
            else:
                self.request.sendall(f"{tag} BAD unknown command\r\n".encode())


class FakeImapServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, mailboxes, *, uidvalidity, valid_credentials):
        self.mailboxes = mailboxes          # folder -> {uid: (raw, internal_date)}
        self.uidvalidity = uidvalidity
        self.valid_credentials = valid_credentials
        self.commands: list[str] = []
        self.selected: list[str] = []
        super().__init__(("127.0.0.1", 0), FakeImapHandler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def search(self, folder, criteria) -> list[int]:
        messages = self.mailboxes.get(folder, {})
        all_uids = sorted(messages)
        if not criteria:
            return all_uids
        key = criteria[0].upper()
        if key == "UID":
            floor = int(criteria[1].split(":")[0])
            hits = [u for u in all_uids if u >= floor]
            # RFC quirk: "n:*" also matches the last message when none is >= n.
            return hits or ([all_uids[-1]] if all_uids else [])
        if key == "SINCE":
            since = datetime.strptime(criteria[1].split()[0], "%d-%b-%Y").date()
            return [u for u in all_uids if messages[u][1].date() >= since]
        return all_uids

    def start(self):
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread

    def shutdown(self):
        self.shutdown_event = True
        super().shutdown()
        self.server_close()


CREDENTIALS = MailCredentials(address="student@stu.pku.edu.cn", authorization_code="fictional-code-123")


def recent(days_ago=0, uid=100, message_id=None) -> tuple[bytes, datetime]:
    sent = datetime.now(BEIJING) - timedelta(days=days_ago)
    raw = build_raw(
        subject=f"虚构通知 {uid}",
        body="讲座将于2026年11月20日举行。",
        message_id=message_id or f"<{uid}@pku.edu.cn>",
        sent=sent,
    )
    return raw, sent


@pytest.fixture
def fake_server():
    server = FakeImapServer(
        {DEFAULT_FOLDER: {1: recent(0, 1), 2: recent(1, 2), 3: recent(2, 3)}},
        uidvalidity=777,
        valid_credentials=(CREDENTIALS.address, CREDENTIALS.authorization_code),
    )
    thread = server.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)


def plain_factory(host, port, timeout):
    return imaplib.IMAP4(host, port, timeout=timeout)


def make_service(server, store=None, **service_kwargs):
    factory = adapter_factory_from(
        store or SessionCredentialStore(),
        RealImapAdapter,
        host="127.0.0.1",
        port=server.port,
        timeout=10,
        connection_factory=plain_factory,
    )
    factory()  # fail fast if credentials are missing
    return EmailSyncService(factory, **service_kwargs)


def store_with(server) -> SessionCredentialStore:
    store = SessionCredentialStore()
    store.save(CREDENTIALS)
    return store


# ------------------------------------------------------------------ happy path

def test_initial_sync_over_the_wire_is_read_only(fake_server):
    store = store_with(fake_server)
    service = make_service(fake_server, store)
    report = service.sync()

    assert report.mode == "initial"
    assert len(report.analyses) == 3
    assert report.trustworthy
    assert report.highest_uid == 3

    commands = "\n".join(fake_server.commands)
    assert " EXAMINE " in commands          # opened read-only, never SELECT
    assert "BODY.PEEK[]" in commands        # flags untouched
    assert " LOGOUT" in commands            # clean shutdown
    for forbidden in FORBIDDEN_ON_WIRE:
        for line in fake_server.commands:
            tokens = line.split()
            assert not (len(tokens) >= 2 and tokens[1] == forbidden), line


def test_examine_is_used_and_select_never_appears(fake_server):
    store = store_with(fake_server)
    make_service(fake_server, store).sync()
    examine_lines = [line for line in fake_server.commands if line.split()[1:2] == ["EXAMINE"]]
    select_lines = [line for line in fake_server.commands if line.split()[1:2] == ["SELECT"]]
    # [READ-ONLY] is the server's *answer* to EXAMINE, so the command log cannot
    # show it; the guarantee being tested is that EXAMINE was the only way in.
    assert examine_lines and not select_lines


def test_incremental_fetch_uses_the_uid_cursor(fake_server):
    store = store_with(fake_server)
    service = make_service(fake_server, store)
    assert service.sync().mode == "initial"

    fake_server.mailboxes[DEFAULT_FOLDER][4] = recent(0, 4, message_id="<4@pku.edu.cn>")
    report = service.sync()
    assert report.mode == "incremental"
    assert [a.record.uid for a in report.analyses] == [4]
    assert any("UID SEARCH UID 4:*" in line for line in fake_server.commands)


def test_initial_sync_is_bounded_to_seven_days_and_thirty_messages(fake_server, monkeypatch):
    from pku_deadline_agent import email_sync as sync_module

    fixed_now = datetime(2026, 11, 11, 9, 0, tzinfo=BEIJING)
    monkeypatch.setattr(sync_module, "beijing_now", lambda: fixed_now)

    def message_at(uid, sent):
        raw = build_raw(
            subject=f"虚构通知 {uid}",
            body="讲座将于2026年11月20日举行。",
            message_id=f"<{uid}@pku.edu.cn>",
            sent=sent,
        )
        return raw, sent

    # 35 messages within the window plus one from 10 days ago (out of range).
    mailbox = {
        index: message_at(index, fixed_now - timedelta(days=1)) for index in range(1, 36)
    }
    mailbox[99] = message_at(99, fixed_now - timedelta(days=10))
    fake_server.mailboxes[DEFAULT_FOLDER] = mailbox

    store = store_with(fake_server)
    service = make_service(fake_server, store)
    report = service.sync()

    assert report.mode == "initial"
    assert len(report.analyses) == INITIAL_MAX
    assert report.has_more                  # 5 more recent messages remain
    since_lines = [line for line in fake_server.commands if "SINCE" in line]
    assert since_lines, "初始同步必须带 SINCE 日期过滤"
    expected_day = (fixed_now - timedelta(days=7)).date()
    for line in since_lines:
        token = line.split('"')[1] if '"' in line else line.split()[-1]
        parsed_day = datetime.strptime(token.split()[0], "%d-%b-%Y").date()
        assert parsed_day == expected_day
    # The 10-day-old message must not be in this batch.
    assert all(a.record.uid != 99 for a in report.analyses)

    # Continuing pages pick up the rest — including the old uid 99, because an
    # incremental pass must not silently skip anything above the cursor.
    follow_up = service.sync()
    assert follow_up.mode == "incremental"
    assert [a.record.uid for a in follow_up.analyses] == [*range(31, 36), 99]
    assert not follow_up.has_more


def test_uidvalidity_reset_warns_and_reruns_initial(fake_server):
    store = store_with(fake_server)
    service = make_service(fake_server, store)
    assert service.sync().mode == "initial"

    fake_server.uidvalidity = 999            # server rebuilt the mailbox
    report = service.sync()
    assert report.mode == "initial"
    assert any("UIDVALIDITY" in w for w in report.warnings)
    assert len(report.analyses) == 3


def test_empty_inbox_is_a_normal_clean_result():
    server = FakeImapServer({DEFAULT_FOLDER: {}}, uidvalidity=5,
                            valid_credentials=(CREDENTIALS.address, CREDENTIALS.authorization_code))
    thread = server.start()
    try:
        service = make_service(server, store_with(server))
        report = service.sync()
        assert report.analyses == ()
        assert report.trustworthy
        assert report.highest_uid == 0
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_parse_rejection_is_counted_and_cursor_moves_on(fake_server):
    oversized = b"From: a@b\r\nSubject: x\r\n\r\n" + b"x" * (2 * 1024 * 1024 + 1)
    fake_server.mailboxes[DEFAULT_FOLDER][1] = (oversized, datetime.now(BEIJING))
    fake_server.mailboxes[DEFAULT_FOLDER][2] = recent(0, 2, message_id="<2@pku.edu.cn>")

    service = make_service(fake_server, store_with(fake_server))
    report = service.sync()

    assert [a.record.uid for a in report.analyses] == [2, 3]
    assert len(report.rejections) == 1 and "UID 1" in report.rejections[0]
    assert report.highest_uid == 3 and not report.trustworthy


# ------------------------------------------------------- failure classification

def test_wrong_authorization_code_is_auth_required(fake_server):
    bad = MailCredentials(address=CREDENTIALS.address, authorization_code="wrong-code")
    store = SessionCredentialStore()
    store.save(bad)
    factory = adapter_factory_from(
        store, RealImapAdapter, host="127.0.0.1", port=fake_server.port,
        timeout=10, connection_factory=plain_factory,
    )
    service = EmailSyncService(factory)
    with pytest.raises(EmailAuthRequired) as error:
        service.sync()
    assert "wrong-code" not in str(error.value)   # the secret never surfaces
    assert any("LOGIN" in line for line in fake_server.commands)


def test_unreachable_server_is_a_network_error():
    store = SessionCredentialStore()
    store.save(CREDENTIALS)
    factory = adapter_factory_from(
        store, RealImapAdapter, host="127.0.0.1", port=1,   # nothing listens here
        timeout=5, connection_factory=plain_factory,
    )
    service = EmailSyncService(factory)
    with pytest.raises(EmailNetworkError):
        service.sync()


def test_missing_credentials_stop_before_any_connection(fake_server):
    service = make_service(fake_server, SessionCredentialStore()) \
        if False else EmailSyncService(adapter_factory_from(
            SessionCredentialStore(), RealImapAdapter, host="127.0.0.1",
            port=fake_server.port, timeout=5, connection_factory=plain_factory))
    with pytest.raises(EmailAdapterError) as error:
        service.sync()
    assert "尚未提供" in str(error.value)
    assert fake_server.commands == []      # nothing was even attempted


# ------------------------------------------------------------------ credentials

def test_credentials_never_reveal_the_code():
    credentials = CREDENTIALS
    assert "fictional-code-123" not in repr(credentials)
    assert "fictional-code-123" not in str(credentials)


def test_session_store_roundtrip_and_clear():
    store = SessionCredentialStore()
    assert store.get() is None
    store.save(CREDENTIALS)
    assert store.get() is CREDENTIALS
    store.clear()
    assert store.get() is None


def test_windows_credential_store_stays_locked_without_confirmation():
    with pytest.raises(PermissionError):
        WindowsCredentialStore()
    with pytest.raises(PermissionError):
        WindowsCredentialStore(user_confirmed_persistence=False)
    # Even a constructed one refuses to operate until the feature is actually built.
    store = WindowsCredentialStore(user_confirmed_persistence=True)
    with pytest.raises(NotImplementedError):
        store.save(CREDENTIALS)
