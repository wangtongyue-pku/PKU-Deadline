"""SQLite persistence. SQL parameters keep user text separate from SQL code."""

import sqlite3
import json
import re
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Iterator

from pku_deadline_agent.models import SOURCES, TASK_TYPES, Task
from pku_deadline_agent.time_utils import beijing_now, normalize_datetime
from pku_deadline_agent.task_management import ImportDecision, NoticeOrigin, MatchSuggestion, find_matches

SCHEMA_VERSION = 4

# The exact column list of the tasks table, in creation order. The v3 -> v4
# migration copies rows through this list, so a future column addition must be
# appended here too (the migration test would otherwise fail loudly).
_TASK_COLUMNS = (
    "id", "title", "category", "task_type", "due_at", "source", "completed",
    "created_at", "updated_at", "revision", "deadline_revision", "deleted_at",
)

_SOURCE_VALUES = "'manual', 'wechat', 'teaching_platform', 'programming_grid', 'email'"


class TaskConflict(ValueError):
    def __init__(self, task_ids: tuple[int, ...]):
        self.task_ids = task_ids
        super().__init__("候选任务与已有任务相同或可能涉及改期，请核对；整批未保存，已有任务未覆盖。")


class TaskChanged(ValueError):
    def __init__(self):
        super().__init__("任务在审核后已被修改或删除，请刷新并重新确认；本次未写入。")


def _identity(value: str) -> str:
    return re.sub(r"[\W_]", "", value.casefold())


class ReadOnlyTaskRepository:
    """Preview-only access: no initialization, migration or mutation methods.

    Reuse the existing task query/conversion through a connection SQLite itself
    opens read-only. Do not use immutable=1: committed WAL data must be visible.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA query_only=ON')
            if connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise RuntimeError('预览只接受当前版本数据库，不自动初始化或迁移。')
            yield connection
        finally:
            connection.close()

    def list_tasks(self, include_completed=False, *, include_deleted=False) -> list[Task]:
        return TaskRepository.list_tasks(self, include_completed, include_deleted=include_deleted)

    @staticmethod
    def _to_task(row):
        return TaskRepository._to_task(row)


class TaskRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("数据库版本较新，请使用新版程序，避免破坏数据。")
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
            if exists and version < SCHEMA_VERSION:
                # Online backup includes committed WAL data. Failure aborts migration.
                self.backup()
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
                raise RuntimeError("数据库版本较新，请使用新版程序，避免破坏数据。")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200),
                    category TEXT NOT NULL DEFAULT '' CHECK(length(category) <= 100),
                    task_type TEXT NOT NULL CHECK(task_type IN ('作业', '考试', '活动报名', '其他')),
                    due_at TEXT,
                    source TEXT NOT NULL DEFAULT 'manual'
                        CHECK(source IN ('manual', 'wechat', 'teaching_platform', 'programming_grid', 'email')),
                    completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due_at ON tasks(due_at)")
            self._migrate_source_check(connection)
            # Additive v1 -> v2 migration, preserving every original task/column.
            connection.execute("""CREATE TABLE IF NOT EXISTS task_extractions (
                task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
                source_index INTEGER NOT NULL, deadline_changed INTEGER NOT NULL,
                review_notes TEXT NOT NULL, deadline_text TEXT)""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            for name, definition in (("revision", "INTEGER NOT NULL DEFAULT 0"), ("deadline_revision", "INTEGER NOT NULL DEFAULT 0"), ("deleted_at", "TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            connection.execute("""CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                action TEXT NOT NULL, old_due_at TEXT, new_due_at TEXT,
                reason TEXT NOT NULL, changed_at TEXT NOT NULL, source TEXT NOT NULL,
                notice_hash TEXT NOT NULL, old_state TEXT, new_state TEXT NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_history_task ON task_history(task_id,id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS task_notices (
                task_id INTEGER NOT NULL REFERENCES tasks(id), notice_hash TEXT NOT NULL,
                source TEXT NOT NULL, published_at TEXT, linked_at TEXT NOT NULL,
                PRIMARY KEY(task_id,notice_hash,source))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS reminder_settings (
                id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL,
                offsets TEXT NOT NULL, last_check_at TEXT)""")
            connection.execute("INSERT OR IGNORE INTO reminder_settings VALUES (1,1,'[259200,86400,10800]',NULL)")
            connection.execute("""CREATE TABLE IF NOT EXISTS reminder_events (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                deadline_revision INTEGER NOT NULL, lead_seconds INTEGER NOT NULL,
                due_at TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, UNIQUE(task_id,deadline_revision,lead_seconds))""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_reminder_state ON reminder_events(state)")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_source_check(self, connection, *, include_zhihuishu=False) -> None:
        """v3 -> v4: allow ``email`` as a task source.

        The allowed values live in a CHECK constraint, and SQLite cannot alter a
        constraint in place, so the table is rebuilt: create the new shape, copy
        every row through an explicit column list, drop the old table, rename.

        This runs inside the caller's transaction, which ``initialize`` opens
        only after taking an online backup, so a failure rolls back and leaves the
        previous table intact. The decision is made from the stored DDL, not from
        the version number, so a database already carrying the new constraint is
        left untouched instead of being copied for nothing.

        Only columns that actually exist are copied: a v1/v2 database reaches this
        point before the additive column migration has run, and demanding a column
        it does not have yet would abort the upgrade of a real user's data.
        """
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        required = "'zhihuishu'" if include_zhihuishu else "'email'"
        source_values = _SOURCE_VALUES + (", 'zhihuishu'" if include_zhihuishu else "")
        if ddl is None or required in (ddl[0] or ""):
            return
        present = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        columns = ", ".join(name for name in _TASK_COLUMNS if name in present)
        if not columns:
            raise RuntimeError("tasks 表没有任何已知列，已中止迁移而不是重建空表。")
        connection.execute(f"""
            CREATE TABLE tasks_v4 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200),
                category TEXT NOT NULL DEFAULT '' CHECK(length(category) <= 100),
                task_type TEXT NOT NULL CHECK(task_type IN ('作业', '考试', '活动报名', '其他')),
                due_at TEXT,
                source TEXT NOT NULL DEFAULT 'manual' CHECK(source IN ({source_values})),
                completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                deadline_revision INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT
            )
        """)
        connection.execute(f"INSERT INTO tasks_v4 ({columns}) SELECT {columns} FROM tasks")
        connection.execute("DROP TABLE tasks")
        connection.execute("ALTER TABLE tasks_v4 RENAME TO tasks")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due_at ON tasks(due_at)")

    def backup(self) -> Path:
        """Create an independent, consistent snapshot; never overwrite a backup."""
        directory = self.path.parent / (self.path.name + ".backups")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (beijing_now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex + ".sqlite3")
        # The source is read-only; missing database is an error, not a new empty DB.
        with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(target) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("备份校验失败，未执行迁移。")
        return target

    def add_task(
        self,
        title: str,
        category: str = "",
        task_type: str = "作业",
        due_at: datetime | None = None,
        source: str = "manual",
    ) -> Task:
        title = title.strip()
        category = category.strip()
        if not title or len(title) > 200:
            raise ValueError("标题必须为 1–200 个字符。")
        if len(category) > 100:
            raise ValueError("课程或活动名称不能超过 100 个字符。")
        if task_type not in TASK_TYPES:
            raise ValueError("不支持的任务类型。")
        if source not in SOURCES:
            raise ValueError("不支持的数据来源。")
        due_text = normalize_datetime(due_at).isoformat() if due_at is not None else None
        now = beijing_now().isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO tasks
                   (title, category, task_type, due_at, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (title, category, task_type, due_text, source, now, now),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
            self._record_history(connection, None, row, "create", "手动添加任务" if source == "manual" else "添加任务", NoticeOrigin(source))
        return self._to_task(row)

    def list_tasks(self, include_completed: bool = False, *, include_deleted: bool = False) -> list[Task]:
        conditions = ([] if include_completed else ["completed = 0"]) + ([] if include_deleted else ["deleted_at IS NULL"])
        condition = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM tasks {condition}
                    ORDER BY due_at IS NULL, due_at ASC, id ASC"""
            ).fetchall()
        return [self._to_task(row) for row in rows]

    def add_reviewed_tasks(self, candidates) -> list[Task]:
        """All-or-nothing insert; recheck conflicts under a write transaction."""
        if not 1 <= len(candidates) <= 50:
            raise ValueError("待保存任务数量无效。")
        for item in candidates:
            if not isinstance(item.title, str) or not 1 <= len(item.title.strip()) <= 200 or not isinstance(item.category, str) or len(item.category.strip()) > 100 or item.task_type not in TASK_TYPES:
                raise ValueError("待保存任务字段无效。")
            if item.due_at is not None:
                normalize_datetime(item.due_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM tasks").fetchall()
            seen = set()
            for item in candidates:
                identity = (_identity(item.title), _identity(item.category))
                if identity in seen:
                    raise ValueError("候选任务内部存在重复，请仅选择一项后保存。")
                seen.add(identity)
                conflicts = tuple(row["id"] for row in existing if _identity(row["title"]) == identity[0] or (item.deadline_changed and identity[1] and _identity(row["category"]) == identity[1]))
                if conflicts:
                    raise TaskConflict(conflicts)
            now = beijing_now().isoformat()
            added = []
            for item in candidates:
                due = normalize_datetime(item.due_at).isoformat() if item.due_at else None
                cursor = connection.execute("""INSERT INTO tasks (title,category,task_type,due_at,source,created_at,updated_at)
                    VALUES (?,?,?,?,'wechat',?,?)""", (item.title.strip(), item.category.strip(), item.task_type, due, now, now))
                connection.execute("INSERT INTO task_extractions VALUES (?,?,?,?,?)", (cursor.lastrowid, item.source_index, int(item.deadline_changed), json.dumps(item.review_notes, ensure_ascii=False), item.deadline_text))
                row = connection.execute("SELECT * FROM tasks WHERE id=?", (cursor.lastrowid,)).fetchone()
                self._record_history(connection, None, row, "create", "审核解析结果后新增", NoticeOrigin())
                added.append(self._to_task(row))
            return added

    def get_task(self, task_id: int, *, include_deleted=False) -> Task:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or (row["deleted_at"] and not include_deleted):
            raise TaskChanged()
        return self._to_task(row)

    @staticmethod
    def _record_history(connection, old, new, action, reason, origin):
        connection.execute("""INSERT INTO task_history
            (task_id,action,old_due_at,new_due_at,reason,changed_at,source,notice_hash,old_state,new_state)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                new["id"], action, old["due_at"] if old else None, new["due_at"], reason,
                beijing_now().isoformat(), origin.source, origin.notice_hash,
                json.dumps(dict(old), ensure_ascii=False) if old else None,
                json.dumps(dict(new), ensure_ascii=False)))

    @staticmethod
    def _validate_reason(reason):
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("请填写 1–500 字的修改原因。")

    def history(self, task_id):
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM task_history WHERE task_id=? ORDER BY id DESC", (task_id,))]

    @staticmethod
    def _checked_row(connection, task_id, revision, *, allow_deleted=False):
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or type(revision) is not int or row["revision"] != revision or (row["deleted_at"] and not allow_deleted):
            raise TaskChanged()
        return row

    def _update(self, connection, old, changes, reason, origin, action="edit"):
        self._validate_reason(reason)
        changes = {key: value for key, value in changes.items() if old[key] != value}
        if changes:
            changes["revision"] = old["revision"] + 1
            changes["updated_at"] = beijing_now().isoformat()
            if "due_at" in changes:
                changes["deadline_revision"] = old["deadline_revision"] + 1
                if old["due_at"] and changes["due_at"]:
                    action = "postpone" if datetime.fromisoformat(changes["due_at"]) > datetime.fromisoformat(old["due_at"]) else "advance"
                else:
                    action = "deadline_correction"
            assignments = ",".join(f"{name}=?" for name in changes)
            connection.execute(f"UPDATE tasks SET {assignments} WHERE id=?", (*changes.values(), old["id"]))
            if "due_at" in changes or changes.get("completed") == 1 or changes.get("deleted_at"):
                connection.execute("UPDATE reminder_events SET state='superseded',updated_at=? WHERE task_id=? AND state='reserved'", (beijing_now().isoformat(), old["id"]))
            new = connection.execute("SELECT * FROM tasks WHERE id=?", (old["id"],)).fetchone()
            self._record_history(connection, old, new, action, reason.strip(), origin)
        else:
            new = old
        return self._to_task(new)

    def edit_task(self, task_id, expected_revision, *, title, category, task_type, due_at, reason, confirmed=False):
        if not confirmed:
            raise ValueError("请先确认修改任务。")
        self._validate_fields(title, category, task_type, due_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = self._checked_row(connection, task_id, expected_revision)
            return self._update(connection, old, {"title": title.strip(), "category": category.strip(), "task_type": task_type,
                "due_at": normalize_datetime(due_at).isoformat() if due_at else None}, reason, NoticeOrigin("manual"))

    def set_completed(self, task_id, expected_revision, completed: bool, *, confirmed=False):
        if not confirmed or type(completed) is not bool:
            raise ValueError("请确认完成状态变更。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = self._checked_row(connection, task_id, expected_revision)
            return self._update(connection, old, {"completed": int(completed)}, "用户标记完成" if completed else "用户恢复待办", NoticeOrigin("manual"), "complete" if completed else "reopen")

    def set_deleted(self, task_id, expected_revision, deleted: bool, *, confirmed=False):
        if not confirmed or type(deleted) is not bool:
            raise ValueError("请确认移入回收站或恢复任务。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = self._checked_row(connection, task_id, expected_revision, allow_deleted=True)
            if bool(old["deleted_at"]) == deleted:
                return self._to_task(old)
            return self._update(connection, old, {"deleted_at": beijing_now().isoformat() if deleted else None}, "用户移入回收站" if deleted else "用户恢复任务", NoticeOrigin("manual"), "delete" if deleted else "restore")

    @staticmethod
    def _validate_fields(title, category, task_type, due_at):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError("标题必须为 1–200 个字符。")
        if not isinstance(category, str) or len(category.strip()) > 100 or task_type not in TASK_TYPES:
            raise ValueError("课程或任务类型无效。")
        if due_at is not None:
            normalize_datetime(due_at)

    def _matches(self, connection, candidate, origin):
        tasks = [self._to_task(row) for row in connection.execute("SELECT * FROM tasks WHERE deleted_at IS NULL")]
        if origin.source == "email" and origin.notice_hash:
            linked = {row[0] for row in connection.execute("SELECT task_id FROM task_notices WHERE source='email' AND notice_hash=?", (origin.notice_hash,))}
            if linked:
                matches = []
                for task in tasks:
                    if task.id in linked:
                        equal = (candidate.title, candidate.category, candidate.task_type, candidate.due_at) == (task.title, task.category, task.task_type, task.due_at)
                        matches.append(MatchSuggestion(task, "duplicate" if equal else "deadline_change", "同一邮件任务来源；字段变化需核对，不能因模型改写名称再次新增"))
                return tuple(matches)
        hashes = {}
        for row in connection.execute("SELECT task_id,notice_hash FROM task_notices"):
            hashes.setdefault(row[0], set()).add(row[1])
        return find_matches(candidate, tasks, hashes, origin.notice_hash)

    def suggestions(self, candidate, origin=NoticeOrigin()):
        with self._connect() as connection:
            return self._matches(connection, candidate, origin)

    def source_mappings(self, source: str) -> dict[str, int]:
        """Map stable platform source keys to local task ids.

        Stage 6 uses this to decide new / unchanged / deadline change without ever
        guessing from a title. Raises rather than returning {} for an invalid
        source or key, because an empty mapping would look like "nothing imported".
        """
        if source not in SOURCES:
            raise ValueError("不支持的数据来源。")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT notice_hash, task_id FROM task_notices WHERE source=?", (source,)
            ).fetchall()
        mappings: dict[str, int] = {}
        for row in rows:
            key = row["notice_hash"]
            if not re.fullmatch(r"[0-9a-f]{64}", key or ""):
                raise ValueError("来源映射键无效，已中止读取。")
            existing = mappings.get(key)
            if existing is not None and existing != row["task_id"]:
                raise ValueError("同一个来源标识关联了多个任务，请人工核对后再同步。")
            mappings[key] = row["task_id"]
        return mappings

    def source_keys(self, source: str, task_ids) -> set[str]:
        """Which platform objects are already linked to these tasks."""
        ids = tuple(task_ids)
        if source not in SOURCES or not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT notice_hash FROM task_notices WHERE source=? AND task_id IN ({placeholders})",
                (source, *ids),
            ).fetchall()
        return {row["notice_hash"] for row in rows}


    def apply_reviewed_tasks(self, candidates, decisions, origins, *, confirmed=False, _connection=None):
        """Atomic explicit new/link/update decisions, with optimistic revision checks."""
        if not confirmed:
            raise ValueError("请确认最终保存或更新方案。")
        if not 1 <= len(candidates) <= 50 or len(candidates) != len(decisions) or len(candidates) != len(origins):
            raise ValueError("候选和处理方案数量无效。")
        for item, decision, origin in zip(candidates, decisions, origins):
            self._validate_fields(item.title, item.category, item.task_type, item.due_at)
            self._validate_reason(decision.reason)
            if origin.source not in SOURCES or (origin.notice_hash and not re.fullmatch(r"[0-9a-f]{64}", origin.notice_hash)):
                raise ValueError("通知来源无效。")
            if origin.published_at:
                normalize_datetime(origin.published_at)
        # Internal composition only: the Zhihuishu gateway includes its source
        # associations and receipt in the SAME outer transaction as these writes.
        with (self._connect() if _connection is None else nullcontext(_connection)) as connection:
            if _connection is None:
                connection.execute("BEGIN IMMEDIATE")
            elif not connection.in_transaction:
                raise ValueError("审核写入必须位于已开启的事务中。")
            results, targets = [], set()
            original_ids = {row[0] for row in connection.execute("SELECT id FROM tasks")}
            for item, decision, origin in zip(candidates, decisions, origins):
                if decision.action == "skip":
                    continue
                matches = self._matches(connection, item, origin)
                if decision.action == "new":
                    if origin.source == "email" and origin.notice_hash and connection.execute("SELECT 1 FROM task_notices WHERE source='email' AND notice_hash=?", (origin.notice_hash,)).fetchone():
                        raise ValueError("该邮件任务来源已经关联本地任务（可能在回收站），请选择关联、更新或先恢复原任务；整批未保存。")
                    if matches and not decision.allow_distinct:
                        if any(match.task.id not in original_ids for match in matches):
                            raise ValueError("本批候选之间存在重复或疑似改期，请只选择一项保存或分批核对。")
                        raise TaskConflict(tuple(match.task.id for match in matches))
                    now = beijing_now().isoformat()
                    due = normalize_datetime(item.due_at).isoformat() if item.due_at else None
                    cursor = connection.execute("""INSERT INTO tasks(title,category,task_type,due_at,source,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?)""", (item.title.strip(), item.category.strip(), item.task_type, due, origin.source, now, now))
                    row = connection.execute("SELECT * FROM tasks WHERE id=?", (cursor.lastrowid,)).fetchone()
                    self._record_history(connection, None, row, "create", decision.reason, origin)
                    result = self._to_task(row)
                    connection.execute("INSERT INTO task_extractions VALUES (?,?,?,?,?)", (result.id, item.source_index, int(item.deadline_changed), json.dumps(item.review_notes, ensure_ascii=False), item.deadline_text))
                elif decision.action in {"link", "update"}:
                    if decision.target_id not in original_ids:
                        raise ValueError("目标任务不在本次审核前的已有任务中，请重新核对。")
                    if decision.target_id in targets:
                        raise ValueError("同一批不能多次修改或关联同一个任务，请分开核对。")
                    targets.add(decision.target_id)
                    row = self._checked_row(connection, decision.target_id, decision.expected_revision)
                    if decision.action == "link":
                        # Associating a duplicate never changes any task fields.
                        result = self._to_task(row)
                    else:
                        result = self._update(connection, row, {"title": item.title.strip(), "category": item.category.strip(), "task_type": item.task_type,
                            "due_at": normalize_datetime(item.due_at).isoformat() if item.due_at else None}, decision.reason, origin)
                else:
                    raise ValueError("请明确选择新增、关联重复、更新或忽略。")
                if origin.notice_hash:
                    cursor = connection.execute("INSERT OR IGNORE INTO task_notices VALUES (?,?,?,?,?)", (result.id, origin.notice_hash, origin.source,
                        normalize_datetime(origin.published_at).isoformat() if origin.published_at else None, beijing_now().isoformat()))
                    if decision.action == "link" and cursor.rowcount:
                        row = connection.execute("SELECT * FROM tasks WHERE id=?", (result.id,)).fetchone()
                        self._record_history(connection, row, row, "link_notice", decision.reason, origin)
                results.append(result)
            return results

    def reminder_config(self):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reminder_settings WHERE id=1").fetchone()
        return {"enabled": bool(row["enabled"]), "offsets": tuple(json.loads(row["offsets"])), "last_check_at": row["last_check_at"]}

    def configure_reminders(self, offsets, *, enabled, confirmed=False):
        if not confirmed or type(enabled) is not bool or not 1 <= len(offsets) <= 12 or any(type(value) is not int or not 60 <= value <= 365 * 86400 for value in offsets):
            raise ValueError("请确认提醒配置：1–12 个提前时间，每个为 1 分钟至 365 天。")
        with self._connect() as connection:
            connection.execute("UPDATE reminder_settings SET enabled=?,offsets=? WHERE id=1", (int(enabled), json.dumps(sorted(set(offsets), reverse=True))))

    def reminder_events(self):
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM reminder_events ORDER BY id DESC LIMIT 100")]

    @staticmethod
    def _to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            title=row["title"],
            category=row["category"],
            task_type=row["task_type"],
            due_at=normalize_datetime(datetime.fromisoformat(row["due_at"])) if row["due_at"] else None,
            source=row["source"],
            completed=bool(row["completed"]),
            created_at=normalize_datetime(datetime.fromisoformat(row["created_at"])),
            updated_at=normalize_datetime(datetime.fromisoformat(row["updated_at"])),
            revision=row["revision"],
            deadline_revision=row["deadline_revision"],
            deleted_at=normalize_datetime(datetime.fromisoformat(row["deleted_at"])) if row["deleted_at"] else None,
        )
