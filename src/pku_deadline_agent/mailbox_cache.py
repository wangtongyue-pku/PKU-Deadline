"""Private mailbox snapshot + durable cursor/schedule, separate from task/AI DBs.

Only the encrypted JSON snapshot contains subjects, bodies and identifiers. A
failed sync leaves the last snapshot and its cursor together unchanged. Never
unpickle server or disk content. Task creation is deliberately absent.
"""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

from pku_deadline_agent.email_models import RawEmailRecord, EmailAnalysis, EmailDeadline
from pku_deadline_agent.email_sync import EmailSyncReport, MailboxState, MailboxStateStore
from pku_deadline_agent.local_protection import DataProtection
from pku_deadline_agent.time_utils import beijing_now, normalize_datetime

MAX_CACHED_MESSAGES = 200
CACHE_DAYS = 30


def account_key(address):
    return hashlib.sha256(address.strip().casefold().encode()).hexdigest()


def _encode(report, state):
    data = {"version": 1, "report": asdict(report), "state": asdict(state) if state else None}
    return json.dumps(data, ensure_ascii=False, default=lambda value: value.isoformat()).encode("utf-8")


def _decode(value):
    data = json.loads(value.decode("utf-8"))
    if data["version"] != 1:
        raise ValueError("邮件缓存版本不支持。")
    raw = data["report"]
    analyses = []
    for item in raw.pop("analyses"):
        message = item.pop("record")
        message["sent_at"] = normalize_datetime(datetime.fromisoformat(message["sent_at"])) if message["sent_at"] else None
        message["recipients"] = tuple(message["recipients"])
        deadlines = []
        for deadline in item.pop("deadlines"):
            deadline["due_at"] = normalize_datetime(datetime.fromisoformat(deadline["due_at"])) if deadline["due_at"] else None
            deadlines.append(EmailDeadline(**deadline))
        for key in ("importance_reasons", "action_reasons"):
            item[key] = tuple(item[key])
        analyses.append(EmailAnalysis(record=RawEmailRecord(**message), deadlines=tuple(deadlines), **item))
    for key in ("warnings", "rejections", "retried_uids", "still_failed_uids"):
        raw[key] = tuple(raw[key])
    state = data["state"]
    if state:
        state["failed_uids"] = tuple(state["failed_uids"])
    return EmailSyncReport(analyses=tuple(analyses), **raw), MailboxState(**state) if state else None


class MailboxCache:
    def __init__(self, path, *, protector=None):
        self.path = Path(path)
        self.protector = protector or DataProtection()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS mailbox (
                account TEXT PRIMARY KEY, snapshot BLOB, enabled INTEGER NOT NULL DEFAULT 0,
                interval_hours INTEGER NOT NULL DEFAULT 24, last_success TEXT, last_attempt TEXT,
                next_due TEXT, status TEXT NOT NULL DEFAULT '尚未检查', claim TEXT, claim_until TEXT)""")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def info(self, account):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM mailbox WHERE account=?", (account,)).fetchone()
        return {key: row[key] for key in row.keys() if key != "snapshot"} if row else {"enabled": 0, "interval_hours": 24, "last_success": None, "next_due": None, "status": "尚未检查"}

    def has_enabled_account(self):
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM mailbox WHERE enabled=1 LIMIT 1").fetchone() is not None

    def load(self, account):
        with self.connect() as connection:
            row = connection.execute("SELECT snapshot FROM mailbox WHERE account=?", (account,)).fetchone()
        if not row or row[0] is None:
            return None, None
        try:
            return _decode(self.protector.unprotect(bytes(row[0])))
        except Exception:
            raise RuntimeError("邮件缓存无法读取，已保留原文件；请检查 Windows 账户或重新同步。") from None

    def configure(self, account, *, enabled, interval_hours=24):
        if type(enabled) is not bool or interval_hours not in (24, 48):
            raise ValueError("邮箱检查间隔必须为 24 或 48 小时。")
        with self.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO mailbox(account) VALUES (?)", (account,))
            connection.execute("UPDATE mailbox SET enabled=?,interval_hours=?,next_due=?,claim=NULL,claim_until=NULL WHERE account=?", (int(enabled), interval_hours, beijing_now().isoformat(), account))

    def claim(self, account, *, force=False, now=None):
        now = normalize_datetime(now or beijing_now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR IGNORE INTO mailbox(account) VALUES (?)", (account,))
            row = connection.execute("SELECT * FROM mailbox WHERE account=?", (account,)).fetchone()
            if row["claim_until"] and datetime.fromisoformat(row["claim_until"]) > now:
                return None
            if not force and (not row["enabled"] or row["next_due"] and datetime.fromisoformat(row["next_due"]) > now):
                return None
            token = uuid.uuid4().hex
            connection.execute("UPDATE mailbox SET claim=?,claim_until=?,last_attempt=? WHERE account=?", (token, (now + timedelta(minutes=10)).isoformat(), now.isoformat(), account))
            return token

    def finish(self, account, token, report, state, *, now=None):
        now = normalize_datetime(now or beijing_now())
        previous, _ = self.load(account)
        merged = {item.record.stable_key: item for item in previous.analyses} if previous else {}
        for item in report.analyses:
            merged[item.record.stable_key] = item
        cutoff = now - timedelta(days=CACHE_DAYS)
        ordered = sorted((item for item in merged.values() if item.record.sent_at is None or item.record.sent_at >= cutoff), key=lambda item: item.record.sent_at or now, reverse=True)
        report = replace(report, analyses=tuple(ordered[:MAX_CACHED_MESSAGES]))
        blob = self.protector.protect(_encode(report, state))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM mailbox WHERE account=? AND claim=?", (account, token)).fetchone()
            if row is None:
                raise ValueError("邮箱配置已改变，本次结果未覆盖缓存。")
            connection.execute("UPDATE mailbox SET snapshot=?,last_success=?,next_due=?,status=?,claim=NULL,claim_until=NULL WHERE account=?", (blob, now.isoformat(), (now + timedelta(hours=row["interval_hours"])).isoformat(), "已更新" if report.trustworthy else "已更新，存在待核对内容", account))
        return report

    def fail(self, account, token, status, *, now=None):
        now = normalize_datetime(now or beijing_now())
        safe = status if status in {"授权失效，请更新授权码", "网络失败，保留上次结果", "读取异常，保留上次结果"} else "读取异常，保留上次结果"
        with self.connect() as connection:
            connection.execute("UPDATE mailbox SET status=?,next_due=?,claim=NULL,claim_until=NULL,enabled=CASE WHEN ? THEN 0 ELSE enabled END WHERE account=? AND claim=?", (safe, (now + timedelta(hours=1)).isoformat(), int(safe.startswith("授权失效")), account, token))


def sync_mailbox(cache, credentials, adapter_factory, *, force=False, now=None):
    """One bounded batch. No AI imports, task DB, destructive mail operation."""
    from pku_deadline_agent.email_adapter import EmailAuthRequired, EmailNetworkError
    from pku_deadline_agent.email_sync import EmailSyncService
    account = account_key(credentials.address)
    token = cache.claim(account, force=force, now=now)
    if token is None:
        return None
    try:
        _, state = cache.load(account)
        states = MailboxStateStore()
        if state:
            states.set("INBOX", state)
        service = EmailSyncService(adapter_factory, state_store=states, batch_limit=30)
        report = service.sync(max_total_messages=30)
        return cache.finish(account, token, report, states.get("INBOX"), now=now)
    except Exception as error:
        status = "授权失效，请更新授权码" if isinstance(error, EmailAuthRequired) else "网络失败，保留上次结果" if isinstance(error, EmailNetworkError) else "读取异常，保留上次结果"
        cache.fail(account, token, status, now=now)
        raise RuntimeError(status) from None
