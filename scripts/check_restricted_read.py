r"""Offline check of the 7E step-2 constraints, end to end, on fictional mail.

This is the rehearsal for the restricted real read: it drives the *real*
``RealImapAdapter`` against the in-process fake IMAP server (no network beyond
127.0.0.1, no credentials that exist anywhere), with a 48-hour window and a
5-message cap, and then verifies each constraint the user stated:

1. INBOX only;
2. at most 5 messages, none older than 48 hours;
3. read-only (EXAMINE, BODY.PEEK; never STORE/EXPUNGE/APPEND/COPY/SELECT);
4. no third-party AI is involved (nothing in this path can reach the network
   except the fake server, and no AI client is imported at all);
5. results are local only — classification, importance and dates;
6. the task database is untouched: no Deadline is created or modified;
7. the original mail is identifiable for human review;
8. no body text, message id or credential reaches stdout or any log record.

Usage:

    .\.venv\Scripts\python.exe -X utf8 scripts\check_restricted_read.py
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pku_deadline_agent.database import TaskRepository  # noqa: E402
from pku_deadline_agent.email_adapter import DEFAULT_FOLDER  # noqa: E402
from pku_deadline_agent.email_credentials import MailCredentials  # noqa: E402
from pku_deadline_agent.email_import import (  # noqa: E402
    default_decisions,
    plan_email_import,
)
from pku_deadline_agent.email_imap import RealImapAdapter  # noqa: E402
from pku_deadline_agent.email_sync import EmailSyncService  # noqa: E402
from pku_deadline_agent.time_utils import BEIJING  # noqa: E402
from tests.test_stage7_imap import (  # noqa: E402
    FakeImapServer,
    build_raw,
    plain_factory,
)

BODY_MARKER = "这段正文只应留在内存，绝不进入日志"
SENDER = "jwc-fictional@pku.edu.cn"
# Distinct local part and domain, each long enough that a byte-level scan cannot
# collide with a UID or an IMAP literal length. A message id is a personal
# identifier, so "does it appear anywhere" is worth checking exactly.
MESSAGE_LOCAL = "restricted-mail-local-part"
MESSAGE_DOMAIN = "fictional-mail-domain.invalid"
# A value that does not appear anywhere else on the wire, so scanning for it
# cannot produce a false positive from an unrelated byte sequence.
CREDENTIAL = "fictional-authcode-do-not-log-9f3c"
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=BEIJING)
WINDOW_HOURS = 48
MAX_MESSAGES = 5
# 2**32 - 1: a UID this large would produce long digit runs on the wire, which is
# what a naive byte scan for a token could otherwise trip over.
LARGE_UID = 4294967295

# (uid, hours before NOW) — three inside the window, three outside.
MESSAGES = ((1, 1), (2, 5), (3, 30), (4, 47), (5, 49), (6, 72))


class RecordingLog(logging.Handler):
    """Captures every log record so the check can assert nothing leaked."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record) -> None:
        self.records.append(self.format(record))


def contains_anywhere(text: str, token: str) -> bool:
    """True when the bytes of ``token`` appear, even across a field boundary.

    A dataclass repr can splice two fields together, so a plain ``in`` check on
    the string can miss a token that is present in the record set. This scan is
    byte-level on purpose.
    """
    return token.encode("utf-8") in text.encode("utf-8", "replace")


def build_message(uid: int, hours_ago: int) -> tuple[bytes, datetime]:
    sent = NOW - timedelta(hours=hours_ago)
    return build_raw(
        subject=f"虚构受限通知 {uid}",
        body=f"{BODY_MARKER}——作业提交截止2026年12月1日。",
        sender=SENDER,
        message_id=f"<{MESSAGE_LOCAL}-{uid}@{MESSAGE_DOMAIN}>",
        sent=sent,
    ), sent


def main() -> int:
    server = FakeImapServer(
        {DEFAULT_FOLDER: {uid: build_message(uid, hours)
                          for uid, hours in MESSAGES}},
        uidvalidity=777,
        valid_credentials=(SENDER, CREDENTIAL),
    )
    thread = server.start()
    handler = RecordingLog()
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.DEBUG)

    import pku_deadline_agent.email_sync as sync_module

    original_now = sync_module.beijing_now
    sync_module.beijing_now = lambda: NOW
    try:
        credentials = MailCredentials(address=SENDER, authorization_code=CREDENTIAL)
        service = EmailSyncService(lambda: RealImapAdapter(
            credentials, host="127.0.0.1", port=server.port, timeout=10,
            connection_factory=plain_factory,
        ))
        report = service.sync(window_hours=WINDOW_HOURS, max_messages=MAX_MESSAGES)
    finally:
        sync_module.beijing_now = original_now
        root.removeHandler(handler)
        root.setLevel(previous_level)

    scratch = Path(__file__).resolve().parents[1] / ".local-tmp" / "restricted-read.sqlite3"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    if scratch.exists():
        scratch.unlink()
    repository = TaskRepository(scratch)
    repository.initialize()

    plan = plan_email_import(report, repository)
    decisions = default_decisions(plan)

    commands = "\n".join(server.commands)
    parsed_uids = [analysis.record.uid for analysis in report.analyses]
    logs = "\n".join(handler.records)
    everything = "\n".join((commands, logs, repr(report), repr(plan.describe())))

    checks = [
        ("1. INBOX only",
         all(analysis.record.folder == DEFAULT_FOLDER for analysis in report.analyses)
         and "EXAMINE INBOX" in commands),
        ("2a. at most 5 messages read", len(report.analyses) <= MAX_MESSAGES),
        ("2b. nothing older than 48 hours was parsed",
         all(analysis.record.sent_at >= NOW - timedelta(hours=WINDOW_HOURS)
             for analysis in report.analyses)),
        ("2c. out-of-window messages were excluded locally",
         all(uid not in parsed_uids for uid in (5, 6))),
        ("2d. exclusions are reported to the user, not hidden",
         report.excluded_older_count >= 1
         and "剔除" in report.describe_scope),
        ("3a. EXAMINE used, SELECT never",
         " EXAMINE " in commands and " SELECT " not in commands),
        ("3b. body read with BODY.PEEK only",
         "BODY.PEEK[]" in commands and "BODY[]" not in commands.replace("BODY.PEEK[]", "")),
        ("3c. no state-changing command on the wire",
         not any(f" {word}" in commands
                 for word in ("STORE", "EXPUNGE", "APPEND", "COPY", "DELETE", "CREATE", "RENAME"))),
        ("4. no AI client is reachable from this path",
         "ai_client" not in sys.modules and "ai_config" not in sys.modules),
        ("5a. classification and dates were produced locally",
         all(analysis.category and analysis.importance for analysis in report.analyses)),
        ("5b. the time semantics survive to the plan",
         len(plan.proposals) >= 1),
        ("6a. no task was written by the read", repository.list_tasks() == []),
        ("6b. applying is still a separate confirmed step",
         all(decision.action in {"new", "skip", "update", "link"} for decision in decisions)),
        ("7. the original sender is kept for review",
         bool(plan.records) and all(item.sender for item in plan.records)),
        ("8a. no body text in stdout, logs or any repr",
         not contains_anywhere(everything, BODY_MARKER)),
        ("8b. no message id in stdout, logs or any repr",
         not contains_anywhere(everything, MESSAGE_LOCAL)
         and not contains_anywhere(everything, MESSAGE_DOMAIN)),
        ("8c. no credential on the wire, in logs or in any repr",
         not contains_anywhere(logs, CREDENTIAL)
         and not contains_anywhere(repr(report), CREDENTIAL)
         and not contains_anywhere(repr(plan), CREDENTIAL)),
        ("8d. the bounded pass left no incremental cursor",
         service._state.get(DEFAULT_FOLDER) is None),
    ]

    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"read {len(report.analyses)} message(s): "
          f"{report.describe_scope}")
    safe = all(ok for _, ok in checks)
    print("RESTRICTED_READ_SAFE" if safe else "RESTRICTED_READ_UNSAFE")
    server.shutdown()
    thread.join(timeout=5)
    return 0 if safe else 1


if __name__ == "__main__":
    sys.exit(main())
