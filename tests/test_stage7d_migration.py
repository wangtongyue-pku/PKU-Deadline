"""Stage 7D schema migration: tasks.source gains the ``email`` value.

The allowed values are enforced by a CHECK constraint, which SQLite cannot alter
in place, so v3 -> v4 rebuilds the table. These tests pin the two things that
matter: every existing row survives with its identifiers and history intact, and
the rebuild needs no second run.
"""

import sqlite3

import pytest

from pku_deadline_agent.database import SCHEMA_VERSION, TaskRepository
from pku_deadline_agent.time_utils import BEIJING
from datetime import datetime

DUE = datetime(2026, 12, 1, 23, 59, tzinfo=BEIJING)

# The tasks table exactly as v3 created it: same columns, narrower source CHECK.
_V3_TASKS_DDL = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200),
    category TEXT NOT NULL DEFAULT '' CHECK(length(category) <= 100),
    task_type TEXT NOT NULL CHECK(task_type IN ('作业', '考试', '活动报名', '其他')),
    due_at TEXT,
    source TEXT NOT NULL DEFAULT 'manual'
        CHECK(source IN ('manual', 'wechat', 'teaching_platform', 'programming_grid')),
    completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    deadline_revision INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT
)
"""

_COLUMNS = ("id,title,category,task_type,due_at,source,completed,created_at,updated_at,"
            "revision,deadline_revision,deleted_at")


def _make_v3_directly(path):
    """Create a v3 database without going through the v4 code path at all."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(_V3_TASKS_DDL)
    connection.execute("""CREATE TABLE task_extractions (
        task_id INTEGER PRIMARY KEY, source_index INTEGER, deadline_changed INTEGER,
        review_notes TEXT, deadline_text TEXT)""")
    connection.execute("""CREATE TABLE task_history (
        id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, action TEXT NOT NULL,
        old_due_at TEXT, new_due_at TEXT, reason TEXT NOT NULL, changed_at TEXT NOT NULL,
        source TEXT NOT NULL, notice_hash TEXT NOT NULL, old_state TEXT, new_state TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE task_notices (
        task_id INTEGER NOT NULL, notice_hash TEXT NOT NULL, source TEXT NOT NULL,
        published_at TEXT, linked_at TEXT NOT NULL, PRIMARY KEY(task_id,notice_hash,source))""")
    connection.execute("""CREATE TABLE reminder_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, offsets TEXT NOT NULL,
        last_check_at TEXT)""")
    connection.execute("INSERT INTO reminder_settings VALUES (1,1,'[259200,86400,10800]',NULL)")
    connection.execute("""CREATE TABLE reminder_events (
        id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, deadline_revision INTEGER NOT NULL,
        lead_seconds INTEGER NOT NULL, due_at TEXT NOT NULL, state TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(task_id,deadline_revision,lead_seconds))""")
    now = "2026-10-01T09:00:00+08:00"
    connection.execute(
        "INSERT INTO tasks VALUES (7,'虚构旧作业','虚构课程','作业',?,'wechat',0,?,?,3,2,NULL)",
        (DUE.isoformat(), now, now))
    connection.execute(
        "INSERT INTO tasks VALUES (9,'虚构旧活动','虚构社团','活动报名',NULL,'manual',1,?,?,0,0,?)",
        (now, now, now))
    connection.execute("INSERT INTO task_extractions VALUES (7,1,0,'[]','12月1日')")
    connection.execute(
        "INSERT INTO task_history VALUES (1,7,'create',NULL,?,'虚构历史','2026-10-01T09:00:00+08:00','wechat','c" + "0" * 63 + "',NULL,'{}')",
        (DUE.isoformat(),))
    connection.execute("INSERT INTO task_notices VALUES (7,?,?,?,?)",
                       ("b" * 64, "wechat", DUE.isoformat(), now))
    connection.execute("PRAGMA user_version=3")
    connection.commit()
    connection.close()


def test_v3_database_migrates_to_v4_and_keeps_every_row(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    _make_v3_directly(path)
    before = sqlite3.connect(path).execute(
        f"SELECT {_COLUMNS} FROM tasks ORDER BY id").fetchall()

    repo = TaskRepository(path)
    repo.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        after = connection.execute(f"SELECT {_COLUMNS} FROM tasks ORDER BY id").fetchall()
    assert after == before
    # Identifiers, revisions and completion state are what the rest of the app
    # relies on; check them through the API too, not only as raw rows.
    assert repo.get_task(7).revision == 3
    assert repo.get_task(7).deadline_revision == 2
    # Task 9 is soft-deleted in the fixture: its deleted_at must survive the rebuild.
    assert repo.get_task(9, include_deleted=True).completed is True
    assert repo.get_task(9, include_deleted=True).deleted_at is not None
    assert repo.list_tasks(include_completed=True)[0].title == "虚构旧作业"


def test_migration_preserves_history_and_source_mappings(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    _make_v3_directly(path)
    TaskRepository(path).initialize()
    repo = TaskRepository(path)
    history = repo.history(7)
    assert [row["action"] for row in history] == ["create"]
    assert repo.source_keys("wechat", (7,)) == {"b" * 64}
    assert repo.source_mappings("wechat") == {"b" * 64: 7}


def test_migration_takes_a_backup_holding_the_old_version(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    _make_v3_directly(path)
    TaskRepository(path).initialize()
    backups = list((tmp_path / "tasks.sqlite3.backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
        assert backup.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


def test_email_source_is_only_accepted_after_migration(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    _make_v3_directly(path)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks (title,task_type,source,created_at,updated_at) "
                "VALUES ('迁移前写入','其他','email','x','x')")

    TaskRepository(path).initialize()

    repo = TaskRepository(path)
    task = repo.add_task("虚构邮件任务", task_type="其他", source="email")
    assert task.source == "email"


def test_migration_is_idempotent_and_does_not_copy_again(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    _make_v3_directly(path)
    repo = TaskRepository(path)
    repo.initialize()
    first_backups = len(list((tmp_path / "tasks.sqlite3.backups").glob("*.sqlite3")))
    with sqlite3.connect(path) as connection:
        rows_before = connection.execute("SELECT count(*) FROM tasks").fetchone()[0]

    repo.initialize()
    repo.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == rows_before
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # Only the first call crossed a version boundary, so only one backup exists.
    assert len(list((tmp_path / "tasks.sqlite3.backups").glob("*.sqlite3"))) == first_backups


def test_failed_source_migration_rolls_back_to_the_v3_shape(tmp_path, monkeypatch):
    """A rebuild that fails must leave the previous table and version in place."""
    path = tmp_path / "tasks.sqlite3"
    _make_v3_directly(path)
    repo = TaskRepository(path)
    monkeypatch.setattr(
        repo, "_migrate_source_check",
        lambda connection: (_ for _ in ()).throw(sqlite3.OperationalError("fictional interruption")),
    )
    with pytest.raises(sqlite3.OperationalError):
        repo.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
        # The narrow constraint is still the one in force.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks (title,task_type,source,created_at,updated_at) "
                "VALUES ('回滚后写入','其他','email','x','x')")


def test_unknown_source_is_still_rejected_after_migration(tmp_path):
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    with pytest.raises(ValueError):
        repo.add_task("虚构任务", source="pku3b")
    with sqlite3.connect(repo.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks (title,task_type,source,created_at,updated_at) "
                "VALUES ('绕过校验','其他','pku3b','x','x')")
