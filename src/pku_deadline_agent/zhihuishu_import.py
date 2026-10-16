"""Explicit reviewed PARTIAL import. Currently enabled ONLY for marked sandboxes.

No browser, model, automatic matching or absence-based task changes. The ordinary
sync trustworthy gate remains intact. Local UUIDs identify human associations,
never pretend to be platform IDs. Core schema v4 + opt-in extension version 1.
"""

from dataclasses import dataclass, field, asdict
from pathlib import Path
import hashlib
import json
import sqlite3
import uuid

from pku_deadline_agent.config import database_path
from pku_deadline_agent.database import SCHEMA_VERSION, TaskRepository, TaskChanged
from pku_deadline_agent.extraction import ExtractedTask
from pku_deadline_agent.sources import SourceRejected
from pku_deadline_agent.task_management import ImportDecision, NoticeOrigin
from pku_deadline_agent.time_utils import beijing_now, normalize_datetime
from pku_deadline_agent.zhihuishu_reader import _text, _source_link
from pku_deadline_agent.zhihuishu_review import ReviewSnapshot, build_review, simulate_choices, fingerprint

FATAL_READ_REASONS = {'source_or_structure_changed','browser_read_failed','session_auth_required',
                      'invalid_or_ambiguous_fields','unstable_list'}


def _require_verified_items(report):
    if FATAL_READ_REASONS.intersection(report.coverage.reasons):
        raise ValueError('读取来源或字段发生异常，不能仅按范围不完整放行；请重新核验任务。')


def _not_production(path):
    path, production = Path(path).resolve(), database_path().resolve()
    if path == production or (path.exists() and production.exists() and path.samefile(production)):
        raise PermissionError('正式库写入尚未开放，虚构数据不能写入正式数据库。')
    return path


@dataclass(frozen=True)
class SandboxImportTarget:
    path: Path = field(repr=False)
    database_id: str = field(repr=False)

    @classmethod
    def create(cls, path):
        path = _not_production(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation: never label an existing user database a sandbox.
        with path.open('xb'): pass
        repository = TaskRepository(path)
        repository.initialize()
        database_id = str(uuid.uuid4())
        with repository._connect() as c:
            c.execute('CREATE TABLE zhs_sandbox_guard (id INTEGER PRIMARY KEY CHECK(id=1), database_id TEXT NOT NULL)')
            c.execute('INSERT INTO zhs_sandbox_guard VALUES (1,?)', (database_id,))
        return cls(path, database_id)

    @classmethod
    def open(cls, path):
        path = _not_production(path)
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as c:
            row = c.execute('SELECT database_id FROM zhs_sandbox_guard WHERE id=1').fetchone()
        if row is None: raise PermissionError('缺少专用模拟库标记。')
        return cls(path, row[0])

    def check(self, connection=None):
        _not_production(self.path)
        if connection is None:
            actual = self.open(self.path)
            if actual.database_id != self.database_id: raise PermissionError('模拟库身份已变化。')
        else:
            row = connection.execute('SELECT database_id FROM zhs_sandbox_guard WHERE id=1').fetchone()
            if row is None or row[0] != self.database_id: raise PermissionError('模拟库身份已变化。')


@dataclass(frozen=True)
class ProductionImportTarget:
    """The ordinary user database, usable ONLY with explicit authorization.

    Deliberately the mirror image of :class:`SandboxImportTarget`: it refuses a
    sandbox-marked database, and it cannot even be constructed without
    ``authorized=True``. Real evidence may go here; fictional evidence may not.
    """

    path: Path = field(repr=False)
    database_id: str = field(repr=False)

    @classmethod
    def open(cls, path=None, *, authorized: bool = False):
        if authorized is not True:
            raise PermissionError('正式库写入需要本次明确授权。')
        target = Path(path) if path is not None else database_path()
        target = target.resolve()
        if not target.exists():
            raise PermissionError('正式任务库不存在；请先正常启动应用完成初始化。')
        with sqlite3.connect(target.as_uri() + '?mode=ro', uri=True) as connection:
            if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='zhs_sandbox_guard'"
                    ).fetchone():
                raise PermissionError('这是专用模拟库，不是正式任务库。')
        return cls(target, 'production:' + hashlib.sha256(str(target).encode('utf-8')).hexdigest()[:16])

    def check(self, connection=None):
        """Re-verify under the write lock: still the formal, non-sandbox database."""
        if self.database_id != 'production:' + hashlib.sha256(
                str(self.path.resolve()).encode('utf-8')).hexdigest()[:16]:
            raise PermissionError('正式库身份已变化。')
        if connection is None:
            with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as c:
                self._verify(c)
            return
        self._verify(connection)

    def _verify(self, connection):
        if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='zhs_sandbox_guard'"
                ).fetchone():
            raise PermissionError('这是专用模拟库，不是正式任务库。')
        if connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
            raise ValueError('正式库核心版本不符；本入口不自动升级旧核心库。')


def is_production(target) -> bool:
    return isinstance(target, ProductionImportTarget)


def _require_matching_evidence(target, report) -> None:
    """Fictional material stays in sandboxes; real reads stay out of them."""
    fictional = report.evidence.startswith('synthetic')
    if is_production(target) and fictional:
        raise PermissionError('虚构数据不能写入正式数据库。')
    if not is_production(target) and not fictional:
        raise PermissionError('真实读取结果只能写入正式数据库；沙箱只接受虚构数据。')


@dataclass(frozen=True)
class ReviewedImportBatch:
    snapshot: ReviewSnapshot = field(repr=False)
    choices: tuple[tuple[int, ImportDecision], ...] = field(repr=False)
    unknown_deadlines: frozenset[int]
    associations: tuple[tuple[int, str], ...] = field(repr=False)
    database_id: str = field(repr=False)
    batch_id: str
    digest: str = field(repr=False)


def _batch_payload(snapshot, choices, unknown_deadlines, associations, database_id):
    return {'snapshot': snapshot.digest, 'local': [asdict(t) for t in snapshot.local_tasks], 'report': asdict(snapshot.report),
            'choices': [(i,asdict(c)) for i,c in choices], 'unknown': sorted(unknown_deadlines),
            'associations': associations, 'database_id': database_id}


def prepare_import(target, snapshot, choices, *, unknown_deadlines=frozenset(), associations=None):
    if not isinstance(target, (SandboxImportTarget, ProductionImportTarget)):
        raise PermissionError('未授权的写入目标。')
    target.check()
    _require_matching_evidence(target, snapshot.report)
    _require_verified_items(snapshot.report)
    plan = simulate_choices(snapshot, choices, unknown_deadlines=unknown_deadlines)
    if not plan.operations: raise ValueError('请明确选择至少一项新增、关联或改期。')
    chosen = tuple(sorted(choices.items()))
    supplied = associations or {}
    links = tuple((op.index, supplied.get(op.index) or str(uuid.uuid4())) for op in plan.operations)
    for _,link in links:
        try: uuid.UUID(link)
        except (ValueError, TypeError, AttributeError): raise ValueError('本地来源关联编号无效。') from None
    digest = fingerprint(_batch_payload(snapshot, chosen, unknown_deadlines, links, target.database_id))
    return ReviewedImportBatch(snapshot, chosen, frozenset(unknown_deadlines), links,
                               target.database_id, str(uuid.uuid4()), digest)


@dataclass(frozen=True)
class ImportReceipt:
    batch_id: str
    task_ids: tuple[int, ...]
    actions: tuple[str, ...]
    association_ids: tuple[str, ...] = field(repr=False)
    partial: bool
    already_applied: bool = False
    backup: Path | None = field(default=None, repr=False)


def _receipt(connection, batch):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='zhs_import_receipts'").fetchone()
    if not exists: return None
    row = connection.execute('SELECT * FROM zhs_import_receipts WHERE batch_id=?', (batch.batch_id,)).fetchone()
    if row is None: return None
    if row['payload_digest'] != batch.digest:
        raise ValueError('该批次已提交但内容不同；未重复执行，请重新核对。')
    data = json.loads(row['result_json'])
    return ImportReceipt(batch.batch_id, tuple(data['task_ids']), tuple(data['actions']),
                         tuple(data['association_ids']), bool(data['partial']), True)


def _migrate(repository, connection):
    if connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
        raise ValueError('需先核对核心数据库版本；本入口不自动升级旧核心库。')
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='zhs_extension'").fetchone()
    if exists:
        row = connection.execute('SELECT version FROM zhs_extension WHERE id=1').fetchone()
        if row is None or row[0] != 1: raise ValueError('智慧树扩展版本不兼容，停止写入。')
    repository._migrate_source_check(connection, include_zhihuishu=True)
    connection.execute('CREATE TABLE IF NOT EXISTS zhs_extension (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)')
    connection.execute('INSERT OR IGNORE INTO zhs_extension VALUES (1,1)')
    connection.execute('''CREATE TABLE IF NOT EXISTS zhs_source_links (
        local_id TEXT PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
        course TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
        deadline_text TEXT, source_url TEXT, platform_status TEXT NOT NULL, observed_due_at TEXT,
        first_seen TEXT NOT NULL, last_reviewed TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS zhs_import_receipts (
        batch_id TEXT PRIMARY KEY, payload_digest TEXT NOT NULL, result_json TEXT NOT NULL,
        applied_at TEXT NOT NULL)''')


def apply_import(target, batch, *, confirmed=False, partial_confirmed=False):
    """Backup -> BEGIN IMMEDIATE -> migration/recheck/writes/receipt -> commit.

    Any failure rolls back the entire selected batch (including extension DDL).
    A successful retry returns its original receipt, without replaying writes.
    """
    if not isinstance(target, (SandboxImportTarget, ProductionImportTarget)):
        raise PermissionError('未授权的写入目标。')
    target.check()
    if not confirmed: raise ValueError('请审核最终方案并明确确认入库。')
    _require_matching_evidence(target, batch.snapshot.report)
    _require_verified_items(batch.snapshot.report)
    partial = not batch.snapshot.report.coverage.complete
    if partial and not partial_confirmed:
        raise ValueError('本次仅导入部分任务，并非完整同步；请明确确认。')
    if batch.database_id != target.database_id:
        raise ValueError('方案不属于当前写入目标。')
    expected = fingerprint(_batch_payload(batch.snapshot,batch.choices,batch.unknown_deadlines,
                                          batch.associations,batch.database_id))
    if expected != batch.digest: raise ValueError('保存方案已变化，需重新审核。')
    # Preflight replay lookup is read-only. Still rechecked under the write lock.
    with sqlite3.connect(target.path.as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory = sqlite3.Row
        previous = _receipt(c,batch)
    if previous: return previous
    repository = TaskRepository(target.path)
    backup = repository.backup()  # includes committed WAL; failure prevents ANY DDL/DML.
    with repository._connect() as c:
        c.execute('BEGIN IMMEDIATE')
        target.check(c)
        previous = _receipt(c,batch)
        if previous: return previous
        _migrate(repository,c)
        current = [repository._to_task(row) for row in c.execute('SELECT * FROM tasks WHERE deleted_at IS NULL ORDER BY id')]
        fresh = build_review(batch.snapshot.report, sorted(current,key=lambda t:t.id))
        # Canonical ordering is independent of deadline ordering in the board.
        baseline = build_review(batch.snapshot.report, sorted(batch.snapshot.local_tasks,key=lambda t:t.id))
        if fresh.digest != baseline.digest: raise TaskChanged()
        plan = simulate_choices(fresh, dict(batch.choices), unknown_deadlines=batch.unknown_deadlines)
        ids = dict(batch.associations)
        if len(ids) != len(plan.operations) or len(set(ids.values())) != len(ids):
            raise ValueError('本地来源关联重复或缺失。')
        candidates, decisions, origins, metadata = [], [], [], []
        for op in plan.operations:
            incoming, link = op.incoming, ids[op.index]
            # Reuse the reader's local field/credential validation before storage.
            reason = op.reason or '用户逐项核验后确认智慧树任务导入'
            try:
                title, course = _text(incoming.title,200), _text(incoming.course,100)
                date_text = _text(incoming.deadline_date,80)
                source_url = _source_link(incoming.source_url,'https://wenda.zhihuishu.com/')
                _text(reason,500)
            except SourceRejected:
                raise ValueError('字段或来源链接含风险或无效内容，整批未保存；请修改后重新审核。') from None
            if not title or not course: raise ValueError('标题或课程缺失。')
            existing = c.execute('SELECT * FROM zhs_source_links WHERE local_id=?',(link,)).fetchone()
            if existing and (op.action == 'new' or existing['task_id'] != op.target.id
                             or existing['course'] != course or existing['kind'] != incoming.kind):
                raise ValueError('本地来源关联冲突，不能重绑或重复新增。')
            choices = dict(batch.choices)
            # For a deadline update retain the user's own title/type/category.
            candidates.append(ExtractedTask(op.index,op.target.title if op.action=='update' else title,
                op.target.category if op.action=='update' else course,'作业',date_text,incoming.deadline,
                op.action=='update',('智慧树人工审核；部分导入非完整同步' if partial else '智慧树人工审核导入',)))
            decisions.append(ImportDecision(op.action,op.target.id if op.target else None,
                op.target.revision if op.target else None,reason,choices[op.index].allow_distinct))
            origins.append(NoticeOrigin('zhihuishu',fingerprint(('local-association',link))))
            metadata.append((course,title,date_text,source_url))
        tasks = repository.apply_reviewed_tasks(tuple(candidates),tuple(decisions),tuple(origins),confirmed=True,_connection=c)
        for op,task,fields in zip(plan.operations,tasks,metadata):
            incoming, link = op.incoming, ids[op.index]
            course,title,date_text,source_url = fields
            now = beijing_now().isoformat()
            c.execute('''INSERT INTO zhs_source_links VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(local_id) DO UPDATE SET title=excluded.title,deadline_text=excluded.deadline_text,
                source_url=excluded.source_url,platform_status=excluded.platform_status,
                observed_due_at=excluded.observed_due_at,last_reviewed=excluded.last_reviewed''',
                (link,task.id,course,incoming.kind,title,date_text,source_url,incoming.status,
                 normalize_datetime(incoming.deadline).isoformat() if incoming.deadline else None,now,now))
        if c.execute('PRAGMA foreign_key_check').fetchone(): raise ValueError('数据库关联完整性检查失败。')
        data = {'task_ids':[t.id for t in tasks],'actions':[op.action for op in plan.operations],
                'association_ids':[ids[op.index] for op in plan.operations],'partial':partial}
        c.execute('INSERT INTO zhs_import_receipts VALUES (?,?,?,?)',
                  (batch.batch_id,batch.digest,json.dumps(data),beijing_now().isoformat()))
        return ImportReceipt(batch.batch_id,tuple(data['task_ids']),tuple(data['actions']),
                             tuple(data['association_ids']),partial,False,backup)


def source_associations(target):
    """Human-selectable local links. No automatic match or platform identity claim."""
    target.check()
    with sqlite3.connect(target.path.as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory = sqlite3.Row
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='zhs_source_links'").fetchone(): return ()
        return tuple(dict(row) for row in c.execute('SELECT * FROM zhs_source_links ORDER BY first_seen,local_id'))
