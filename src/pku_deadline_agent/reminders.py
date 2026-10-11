"""Persistent, at-most-once local reminder claims, independent of Streamlit."""

import argparse
import base64
import json
import os
import subprocess
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pku_deadline_agent.config import database_path
from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.time_utils import beijing_now, format_deadline, normalize_datetime


def parse_hours(text: str) -> tuple[int, ...]:
    try:
        values = [Decimal(part.strip()) * 3600 for part in text.replace("，", ",").split(",")]
        if not 1 <= len(values) <= 12 or any(not value.is_finite() or value != value.to_integral_value() or not 60 <= value <= 365 * 86400 for value in values):
            raise ValueError
        return tuple(sorted({int(value) for value in values}, reverse=True))
    except (ValueError, InvalidOperation, OverflowError):
        raise ValueError("请输入 1–12 个提前小时数，用逗号分隔；支持小数，范围为 1 分钟至 365 天。") from None


def _eligible(connection, now):
    config = connection.execute("SELECT * FROM reminder_settings WHERE id=1").fetchone()
    if not config["enabled"]:
        return []
    offsets = json.loads(config["offsets"])
    rows = connection.execute("SELECT * FROM tasks WHERE completed=0 AND deleted_at IS NULL AND due_at IS NOT NULL ORDER BY due_at,id").fetchall()
    result = []
    for row in rows:
        remaining = (normalize_datetime(datetime.fromisoformat(row["due_at"])) - now).total_seconds()
        if remaining <= 0:
            continue
        eligible = [offset for offset in offsets if remaining <= offset]
        if not eligible:
            continue
        nearest = min(eligible)
        previous = connection.execute("SELECT 1 FROM reminder_events WHERE task_id=? AND deadline_revision=? AND lead_seconds=?", (row["id"], row["deadline_revision"], nearest)).fetchone()
        if previous is None:
            result.append((row, nearest, eligible))
    return result


def preview_due(repository: TaskRepository, now=None):
    now = normalize_datetime(now or beijing_now())
    with repository._connect() as connection:
        return [(repository._to_task(row), offset) for row, offset, _ in _eligible(connection, now)]


def reserve_due(repository: TaskRepository, now=None):
    now = normalize_datetime(now or beijing_now())
    stamp = now.isoformat()
    with repository._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        claims = []
        for row, nearest, eligible in _eligible(connection, now):
            # After downtime send only the most urgent due level, not a burst.
            for offset in eligible:
                cursor = connection.execute("""INSERT OR IGNORE INTO reminder_events
                    (task_id,deadline_revision,lead_seconds,due_at,state,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?)""", (row["id"], row["deadline_revision"], offset, row["due_at"], "reserved" if offset == nearest else "superseded", stamp, stamp))
                if offset == nearest and cursor.rowcount:
                    claims.append(cursor.lastrowid)
        connection.execute("UPDATE reminder_settings SET last_check_at=? WHERE id=1", (stamp,))
        return claims


def deliver(repository: TaskRepository, event_id: int, notifier, now=None):
    now = normalize_datetime(now or beijing_now())
    with repository._connect() as connection:
        # Serialize against completion/deletion/deadline edits through the send.
        connection.execute("BEGIN IMMEDIATE")
        event = connection.execute("SELECT * FROM reminder_events WHERE id=?", (event_id,)).fetchone()
        if event is None or event["state"] != "reserved":
            return "skipped"
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (event["task_id"],)).fetchone()
        config = connection.execute("SELECT * FROM reminder_settings WHERE id=1").fetchone()
        if (row is None or row["completed"] or row["deleted_at"] or row["deadline_revision"] != event["deadline_revision"]
                or row["due_at"] != event["due_at"] or not config["enabled"] or event["lead_seconds"] not in json.loads(config["offsets"])
                or datetime.fromisoformat(event["due_at"]) <= now):
            state = "superseded"
        else:
            try:
                notifier(repository._to_task(row), event["lead_seconds"])
                state = "sent"
            except Exception:
                # No automatic resend: the OS may already have accepted the popup.
                state = "failed"
        connection.execute("UPDATE reminder_events SET state=?,updated_at=? WHERE id=?", (state, now.isoformat(), event_id))
        return state


def run_once(repository, notifier, now=None):
    counts = {"sent": 0, "failed": 0, "superseded": 0, "skipped": 0}
    for event_id in reserve_due(repository, now):
        counts[deliver(repository, event_id, notifier, now)] += 1
    return counts


class WindowsNotifier:
    """Built-in Windows Forms balloon; no dependency install or remote service."""

    def __call__(self, task, lead_seconds):
        if os.name != "nt":
            raise RuntimeError("本地弹窗仅支持 Windows。")
        message = f"{task.title}\n截止（北京）：{format_deadline(task.due_at)}\n提醒档位：提前 {lead_seconds / 3600:g} 小时"
        payload = base64.b64encode(message[:240].encode("utf-8")).decode("ascii")
        # User text is base64 data, never interpolated as PowerShell code.
        script = """$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$pkuText=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD'))
$pkuIcon=New-Object System.Windows.Forms.NotifyIcon
try {
    $pkuIcon.Icon=[Drawing.SystemIcons]::Information
    $pkuIcon.Visible=$true
    $pkuIcon.ShowBalloonTip(5000,'Deadline',$pkuText,[Windows.Forms.ToolTipIcon]::Info)
    for($pkuTick=0;$pkuTick -lt 50;$pkuTick++) {
        [Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 100
    }
} finally { $pkuIcon.Dispose() }
""".replace("PAYLOAD", payload)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", encoded],
                       check=True, timeout=12, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                       env={name: os.environ[name] for name in ("SystemRoot", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA") if name in os.environ})


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地 Deadline 提醒；默认只演练，不弹窗、不消耗提醒次数。")
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--notify", action="store_true", help="实际推送 Windows 本地提醒")
    parser.add_argument("--once", action="store_true", help="只检查一次")
    parser.add_argument("--dry-run", action="store_true", help="只演练，不写入提醒记录")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args(argv)
    if not 10 <= args.interval <= 3600:
        parser.error("检查间隔必须为 10–3600 秒。")
    if args.notify and args.dry_run:
        parser.error("--notify 和 --dry-run 不能同时使用。")
    repo = TaskRepository(args.database or database_path())
    try:
        repo.initialize()
        if not args.notify:
            print(f"Dry run: {len(preview_due(repo))} reminder(s) due; nothing sent or claimed.")
            return 0
        if os.name != "nt":
            print("Windows is required for --notify.")
            return 1
        while True:
            result = run_once(repo, WindowsNotifier())
            print(f"sent={result['sent']} failed={result['failed']} cancelled={result['superseded']}", flush=True)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Local reminder/database error; check permissions and version. No automatic retry.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
