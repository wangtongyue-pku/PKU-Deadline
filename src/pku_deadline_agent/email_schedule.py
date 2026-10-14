"""Windows current-user task registration and a one-shot, rule-only mailbox job.

No credential in commands, no elevated service, no AI call or task DB write.
Windows wakes the job hourly; the durable due-time gate permits one read/day.
"""

import argparse
import hashlib
import os
import subprocess
from pathlib import Path

from pku_deadline_agent.config import PROJECT_ROOT, database_path
from pku_deadline_agent.email_adapter import IMAP_HOST, IMAP_PORT
from pku_deadline_agent.email_credentials import WindowsCredentialStore
from pku_deadline_agent.email_imap import RealImapAdapter
from pku_deadline_agent.mailbox_cache import MailboxCache, account_key, sync_mailbox


def cache_path(task_path=None):
    return Path(task_path or database_path()).parent / "mailbox.sqlite3"


class WindowsMailSchedule:
    def __init__(self, *, project_root=PROJECT_ROOT, task_path=None, runner=None):
        self.root = Path(project_root).resolve()
        self.task_path = Path(task_path or database_path()).resolve()
        self.runner = runner or subprocess.run
        identity = str(self.task_path) + os.environ.get("USERNAME", "")
        self.name = "Deadline-Mail-" + hashlib.sha256(identity.encode()).hexdigest()[:16]

    def _command(self, args):
        if os.name != "nt":
            raise RuntimeError("自动邮箱检查需要 Windows 任务计划程序。")
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/schtasks.exe"
        try:
            self.runner([str(executable), *args], check=True, timeout=20, capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            raise RuntimeError("Windows 任务计划配置失败；请检查当前账户权限。未连接邮箱，也未上传 AI。") from None

    def enable(self):
        python = self.root / ".venv/Scripts/pythonw.exe"
        script = self.root / "scripts/mailbox_job.py"
        if not python.is_file() or not script.is_file():
            raise RuntimeError("找不到项目 Python 或邮箱任务入口，请勿移动已配置的项目目录。")
        command = subprocess.list2cmdline([str(python), str(script), "--task-database", str(self.task_path)])
        self._command(["/Create", "/TN", self.name, "/TR", command, "/SC", "HOURLY", "/MO", "1", "/IT", "/RL", "LIMITED", "/F"])

    def disable(self):
        # Keeping the disabled task makes this operation reversible and needs no
        # deletion privileges. Durable enabled=False is the independent gate.
        self._command(["/Change", "/TN", self.name, "/DISABLE"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读邮箱到期检查；不调用 AI，不创建任务。")
    parser.add_argument("--task-database", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        cache = MailboxCache(cache_path(args.task_database))
        if not cache.has_enabled_account():
            return 0
        store = WindowsCredentialStore(user_confirmed_persistence=True)
        credentials = store.get()
        if credentials is None:
            return 0
        # Do not authenticate unless the configured account is enabled and due.
        sync_mailbox(cache, credentials, lambda: RealImapAdapter(credentials, host=IMAP_HOST, port=IMAP_PORT, timeout=30.0))
        return 0
    except Exception:
        # No traceback, mailbox header, body, credential or server error output.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
