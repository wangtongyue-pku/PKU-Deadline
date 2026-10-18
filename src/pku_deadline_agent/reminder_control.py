"""Start and stop the local reminder process from the app.

The reminder used to need a second PowerShell window. This module lets the page
start the same CLI as a detached, invisible process and stop it again - a manual
switch only: nothing is added to Task Scheduler, nothing starts with Windows, and
no credentials or external services are involved.

All side effects are injectable so tests never spawn a real process.
"""

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

from pku_deadline_agent.config import database_path
from pku_deadline_agent.time_utils import beijing_now

PID_ENV = "PKU_REMINDER_PID_FILE"
WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class ReminderStatus:
    running: bool
    pid: int | None = None
    started_at: str = ""


def pid_path() -> Path:
    override = os.environ.get(PID_ENV, "").strip()
    if override:
        return Path(override)
    return database_path().parent / "reminders.pid"


def _alive(pid: int) -> bool:
    """Is this pid a live process? tasklist is used so no extra dependency is added."""
    if not WINDOWS:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True
    try:
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return f'"{pid}"' in (result.stdout or "")


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def status(*, path: Path | None = None, probe=_alive) -> ReminderStatus:
    """Report the process this app started; a stale pid file is cleaned up."""
    file = Path(path) if path is not None else pid_path()
    data = _read(file)
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        pid = None
    if pid is None or not probe(pid):
        if file.exists():
            try:
                file.unlink()
            except OSError:
                pass
        return ReminderStatus(False)
    return ReminderStatus(True, pid, str(data.get("started_at") or ""))


def _command() -> list[str]:
    # The same entry point the README documents; the database is passed explicitly
    # so the detached process does not depend on the caller's environment.
    return [sys.executable, "-m", "pku_deadline_agent.reminders", "--notify",
            "--database", str(database_path())]


def start(*, path: Path | None = None, spawn=None, probe=_alive) -> ReminderStatus:
    """Start the reminder process once. Returns the resulting status."""
    file = Path(path) if path is not None else pid_path()
    current = status(path=file, probe=probe)
    if current.running:
        return current
    if not WINDOWS:
        raise OSError("提醒目前只在 Windows 上支持。")
    launcher = spawn if spawn is not None else _spawn_detached
    process = launcher(_command())
    pid = int(getattr(process, "pid"))
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({"pid": pid, "started_at": beijing_now().isoformat(timespec="seconds"),
                                "database": str(database_path())}, ensure_ascii=False),
                    encoding="utf-8")
    return ReminderStatus(True, pid, _read(file).get("started_at", ""))


def _spawn_detached(command: list[str]):
    flags = 0
    if WINDOWS:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(  # noqa: S603 - fixed command, no shell, no user text
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, close_fds=True)


def stop(*, path: Path | None = None, kill=None, probe=_alive) -> bool:
    """Stop the process this app started. True when it was running and was stopped."""
    file = Path(path) if path is not None else pid_path()
    current = status(path=file, probe=probe)
    if not current.running:
        return False
    killer = kill if kill is not None else _kill
    killer(current.pid)
    try:
        file.unlink()
    except OSError:
        pass
    return True


def _kill(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, timeout=15, check=False)


def describe(moment: str) -> str:
    """Render a stored ISO timestamp as a short Beijing time, or nothing."""
    try:
        return datetime.fromisoformat(moment).strftime("%m-%d %H:%M")
    except ValueError:
        return ""
