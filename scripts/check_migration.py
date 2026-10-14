r"""Pre-flight check for the schema migration, run on a read-only copy.

Why this exists: the app upgrades the task database the first time the new
version opens it. That happens to real user data, so it is worth proving it
first on a copy that cannot be affected by the check itself.

What it does:

1. opens the real database **read-only** and copies it with SQLite's own online
   backup API (so committed WAL content is included);
2. runs the migration on the copy only;
3. compares every table row by row, before and after;
4. reports whether a backup was taken and whether a second run is a no-op.

The original file is never written to. Nothing here prints task titles.

Usage:

    .\.venv\Scripts\python.exe -X utf8 scripts\check_migration.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pku_deadline_agent.config import database_path  # noqa: E402
from pku_deadline_agent.database import SCHEMA_VERSION, TaskRepository  # noqa: E402

TABLES = ("tasks", "task_history", "task_notices", "task_extractions",
          "reminder_settings", "reminder_events")


def _rows(connection, table: str) -> list[tuple]:
    return [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]


def _snapshot(connection) -> dict[str, list[tuple]]:
    return {table: sorted(_rows(connection, table)) for table in TABLES}


def main() -> int:
    source = Path(database_path()).resolve()
    if not source.exists():
        print(f"NO_DATABASE: {source} 不存在；首次启动会新建一个 v{SCHEMA_VERSION} 库，无需迁移。")
        return 0

    scratch = Path(tempfile.mkdtemp(prefix="pku-migration-check-"))
    copy = scratch / source.name
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as origin:
        origin_version = origin.execute("PRAGMA user_version").fetchone()[0]
        with sqlite3.connect(copy) as destination:
            origin.backup(destination)
    print(f"source      : {source}")
    print(f"copy        : {copy}")
    print(f"source version: {origin_version} (original file is opened read-only)")

    with sqlite3.connect(copy) as connection:
        before = _snapshot(connection)

    repository = TaskRepository(copy)
    repository.initialize()

    with sqlite3.connect(copy) as connection:
        after_version = connection.execute("PRAGMA user_version").fetchone()[0]
        after = _snapshot(connection)

    print(f"after version : {after_version}")
    for table in TABLES:
        same = before[table] == after[table]
        print(f"  [{'PASS' if same else 'FAIL'}] {table}: {len(before[table])} -> "
              f"{len(after[table])} rows, byte-identical={same}")
        if not same:
            print(f"      before={before[table][:3]}")
            print(f"      after ={after[table][:3]}")

    backups = sorted((copy.parent / (copy.name + ".backups")).glob("*.sqlite3"))
    print(f"  [{'PASS' if backups else 'FAIL'}] migration backup taken: "
          f"{[b.name for b in backups]}")

    repository.initialize()
    with sqlite3.connect(copy) as connection:
        idempotent = connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        second = _snapshot(connection) == after
    print(f"  [{'PASS' if idempotent and second else 'FAIL'}] second initialize changes nothing")

    accepted = _accepts_email_source(repository)
    print(f"  [{'PASS' if accepted else 'FAIL'}] the new email source is accepted")

    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as origin:
        untouched = origin.execute("PRAGMA user_version").fetchone()[0] == origin_version
    print(f"  [{'PASS' if untouched else 'FAIL'}] original file still at version {origin_version}")

    identical = all(before[table] == after[table] for table in TABLES)
    print("MIGRATION_SAFE" if (identical and backups and accepted and untouched)
          else "MIGRATION_UNSAFE")
    return 0 if identical and backups and accepted and untouched else 1


def _accepts_email_source(repository: TaskRepository) -> bool:
    """The whole point of v4; verified on the real table shape, then rolled back."""
    task = repository.add_task("迁移检查虚构任务（可删除）", task_type="其他", source="email")
    try:
        return task.source == "email"
    finally:
        # Only touches the scratch copy, never the original.
        with sqlite3.connect(repository.path) as connection:
            connection.execute("DELETE FROM task_history WHERE task_id=?", (task.id,))
            connection.execute("DELETE FROM tasks WHERE id=?", (task.id,))


if __name__ == "__main__":
    sys.exit(main())
