"""Real SQLite transactions on marked fictional sandboxes, never user data."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3

import pytest

from pku_deadline_agent.database import TaskRepository, TaskChanged
from pku_deadline_agent.task_management import ImportDecision
from pku_deadline_agent.zhihuishu_import import (
    ProductionImportTarget, SandboxImportTarget, prepare_import, apply_import, source_associations,
)
from pku_deadline_agent.zhihuishu_review import build_review
from pku_deadline_agent.zhihuishu_samples import sample_review_data
from test_zhihuishu_validation import real_report


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('PKU_DATABASE_PATH',str(tmp_path/'formal.sqlite3'))
    target = SandboxImportTarget.create(tmp_path/'sandbox'/'tasks.sqlite3')
    repo = TaskRepository(target.path)
    report, local = sample_review_data()
    for task in local:
        added = repo.add_task(task.title,task.category,task.task_type,task.due_at,
                              source='email' if task.completed else 'manual')
        if task.completed: repo.set_completed(added.id,added.revision,True,confirmed=True)
    for source in ('programming_grid','email','wechat'):
        repo.add_task('虚构未出现的'+source,'虚构另一门课','作业',local[0].due_at,source=source)
    return target,repo,report


def review(setup):
    target,repo,report = setup
    return build_review(report,repo.list_tasks(include_completed=True))


def new_batch(setup, **kwargs):
    return prepare_import(setup[0],review(setup),{1:ImportDecision('new')},**kwargs)


def apply(target,batch):
    return apply_import(target,batch,confirmed=True,partial_confirmed=True)


def tables(path):
    with sqlite3.connect(path) as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_production_target_needs_authorization_and_refuses_sandboxes(tmp_path, monkeypatch):
    formal = tmp_path / 'formal.sqlite3'
    TaskRepository(formal).initialize()
    monkeypatch.setenv('PKU_DATABASE_PATH', str(formal))
    with pytest.raises(PermissionError):
        ProductionImportTarget.open(formal)                       # no authorization
    with pytest.raises(PermissionError):
        ProductionImportTarget.open(tmp_path / 'missing.sqlite3', authorized=True)
    sandbox = SandboxImportTarget.create(tmp_path / 'sandbox' / 'tasks.sqlite3')
    with pytest.raises(PermissionError):
        ProductionImportTarget.open(sandbox.path, authorized=True)


def test_evidence_and_target_must_match(tmp_path, monkeypatch):
    """Fictional material stays in sandboxes; a real read never enters one."""
    formal = tmp_path / 'formal.sqlite3'
    TaskRepository(formal).initialize()
    monkeypatch.setenv('PKU_DATABASE_PATH', str(formal))
    production = ProductionImportTarget.open(formal, authorized=True)
    synthetic, _ = sample_review_data()
    with pytest.raises(PermissionError):
        prepare_import(production, build_review(synthetic, ()), {1: ImportDecision('new', allow_distinct=True)})
    sandbox = SandboxImportTarget.create(tmp_path / 'sandbox' / 'tasks.sqlite3')
    with pytest.raises(PermissionError):
        prepare_import(sandbox, build_review(real_report(), ()), {1: ImportDecision('new', allow_distinct=True)})


def real_read_report():
    """A real-evidence report shaped like the whitelisted payload: NO source links."""
    from datetime import datetime

    from pku_deadline_agent.time_utils import BEIJING
    from pku_deadline_agent.zhihuishu_reader import ReadTask
    from pku_deadline_agent.zhihuishu_scroll import ScrollCoverage, ScrollReport

    tasks = (ReadTask('虚构受限任务甲', '虚构受限课程', datetime(2034, 6, 10, 20, tzinfo=BEIJING),
                      '2034-06-10'),
             ReadTask('虚构受限任务乙', '虚构受限课程', None, '2034-06-15'))
    coverage = ScrollCoverage(5, 5, 2, 2, 0, None, False, False, True, False, 'unverified', 3,
                              ('boundary_unverified', 'fields_incomplete'),
                              (('deadline_missing', 1),))
    return ScrollReport(tasks, coverage, '虚构受限课程', 'authorized-real-validation')


def test_production_import_backs_up_writes_and_never_deletes(tmp_path, monkeypatch):
    formal = tmp_path / 'formal.sqlite3'
    repository = TaskRepository(formal)
    repository.initialize()
    kept = repository.add_task('虚构原有任务', '虚构受限课程', '作业', None)
    other = repository.add_task('虚构另一门课任务', '虚构别的课', '作业', None, source='wechat')
    monkeypatch.setenv('PKU_DATABASE_PATH', str(formal))
    target = ProductionImportTarget.open(formal, authorized=True)

    snapshot = build_review(real_read_report(), repository.list_tasks(include_completed=True))
    batch = prepare_import(target, snapshot, {1: ImportDecision('new', allow_distinct=True)})
    receipt = apply_import(target, batch, confirmed=True, partial_confirmed=True)

    after = repository.list_tasks(include_completed=True)
    assert receipt.partial and len(receipt.task_ids) == 1
    written = repository.get_task(receipt.task_ids[0])
    assert written.source == 'zhihuishu' and written.title == '虚构受限任务甲'
    # Absent tasks are never deleted, completed or otherwise touched.
    assert repository.get_task(kept.id).title == '虚构原有任务'
    assert repository.get_task(other.id).source == 'wechat'
    assert len(after) == 3
    assert {'zhs_extension', 'zhs_source_links', 'zhs_import_receipts'} <= tables(formal)
    assert list(formal.parent.glob(formal.name + '.backups/*.sqlite3'))

    # The same batch replayed returns its receipt and writes nothing again.
    again = apply_import(target, batch, confirmed=True, partial_confirmed=True)
    assert again.already_applied and again.task_ids == receipt.task_ids
    assert len(repository.list_tasks(include_completed=True)) == 3


def test_production_import_rolls_back_when_the_reference_changed(tmp_path, monkeypatch):
    formal = tmp_path / 'formal.sqlite3'
    repository = TaskRepository(formal)
    repository.initialize()
    existing = repository.add_task('虚构词汇作业', '虚构受限课程', '作业', None)
    monkeypatch.setenv('PKU_DATABASE_PATH', str(formal))
    target = ProductionImportTarget.open(formal, authorized=True)
    snapshot = build_review(real_read_report(), repository.list_tasks(include_completed=True))
    batch = prepare_import(target, snapshot, {1: ImportDecision('new', allow_distinct=True)})
    # Someone edits the local database after the plan was prepared.
    repository.edit_task(existing.id, existing.revision, title='虚构用户改名',
                         category=existing.category, task_type=existing.task_type,
                         due_at=existing.due_at, reason='虚构并发修改', confirmed=True)
    with pytest.raises(TaskChanged):
        apply_import(target, batch, confirmed=True, partial_confirmed=True)
    assert repository.get_task(existing.id).title == '虚构用户改名'
    assert len(repository.list_tasks(include_completed=True)) == 1


def test_partial_new_change_unknown_deadline_and_other_sources_preserved(setup):
    target,repo,_ = setup
    before = repo.list_tasks(include_completed=True)
    old = next(t for t in before if t.id==2)
    batch = prepare_import(target,review(setup),{
        1:ImportDecision('new'),3:ImportDecision('update',2,old.revision,'虚构延期核对'),
        4:ImportDecision('new')},unknown_deadlines=frozenset({4}))
    receipt = apply(target,batch)
    after = repo.list_tasks(include_completed=True)
    assert len(before)==7 and len(after)==9 and receipt.partial
    assert receipt.actions==('new','update','new')
    assert all(repo.get_task(t.id)==t for t in before if t.id!=2)
    changed = repo.get_task(2)
    assert changed.completed and changed.source=='email' and changed.due_at.day==12
    assert changed.revision==old.revision+1 and changed.deadline_revision==old.deadline_revision+1
    unknown = repo.get_task(receipt.task_ids[-1])
    assert unknown.source=='zhihuishu' and unknown.due_at is None and unknown.needs_confirmation
    assert repo.history(2)[0]['source']=='zhihuishu'
    assert len(source_associations(target))==3
    assert next(l for l in source_associations(target) if l['task_id']==unknown.id)['deadline_text']=='2034-06-15'
    assert receipt.backup.exists()
    with sqlite3.connect(receipt.backup) as c:
        assert c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==7
        assert c.execute('PRAGMA user_version').fetchone()[0]==4
        assert 'zhs_extension' not in tables(receipt.backup)
    with sqlite3.connect(target.path) as c:
        assert c.execute('SELECT version FROM zhs_extension').fetchone()[0]==1
        assert c.execute('PRAGMA user_version').fetchone()[0]==4
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


@pytest.mark.parametrize('confirmed,partial',[(False,False),(False,True),(True,False)])
def test_both_final_and_partial_confirmation_required_before_any_ddl(setup,confirmed,partial):
    target,repo,_ = setup
    batch = new_batch(setup)
    before = target.path.read_bytes()
    with pytest.raises(ValueError):
        apply_import(target,batch,confirmed=confirmed,partial_confirmed=partial)
    assert target.path.read_bytes()==before
    assert 'zhs_extension' not in tables(target.path)


def test_exact_retry_and_parallel_retry_never_duplicate(setup):
    target,repo,_ = setup
    batch = new_batch(setup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _:apply(target,batch),range(2)))
    assert sum(not r.already_applied for r in receipts)==1
    assert receipts[0].task_ids==receipts[1].task_ids
    assert len(repo.list_tasks(include_completed=True))==8
    count = len(list(target.path.parent.glob('tasks.sqlite3.backups/*.sqlite3')))
    assert apply(target,batch).already_applied
    assert len(list(target.path.parent.glob('tasks.sqlite3.backups/*.sqlite3')))==count
    with sqlite3.connect(target.path) as c:
        assert c.execute('SELECT COUNT(*) FROM zhs_import_receipts').fetchone()[0]==1


def test_same_submission_id_changed_payload_is_conflict(setup):
    target,repo,_ = setup
    first = new_batch(setup)
    apply(target,first)
    second = prepare_import(target,review(setup),{1:ImportDecision('new',allow_distinct=True)})
    with pytest.raises(ValueError,match='内容不同'):
        apply(target,replace(second,batch_id=first.batch_id))
    assert len(repo.list_tasks(include_completed=True))==8


@pytest.mark.parametrize('change',['edit','delete','new_match'])
def test_stale_review_aborts_entire_selected_batch(setup,change):
    target,repo,_ = setup
    old = repo.get_task(2)
    batch = prepare_import(target,review(setup),{1:ImportDecision('new'),
        3:ImportDecision('update',2,old.revision,'虚构延期')})
    if change=='edit':
        repo.edit_task(2,old.revision,title=old.title+'虚构修订',category=old.category,
            task_type=old.task_type,due_at=old.due_at,reason='虚构用户修改',confirmed=True)
    elif change=='delete': repo.set_deleted(2,old.revision,True,confirmed=True)
    else: repo.add_task('虚构阅读练习','虚构翻转课甲','作业',old.due_at)
    before = repo.list_tasks(include_completed=True,include_deleted=True)
    with pytest.raises(TaskChanged): apply(target,batch)
    assert repo.list_tasks(include_completed=True,include_deleted=True)==before
    assert 'zhs_extension' not in tables(target.path)


def test_failed_backup_prevents_migration_and_write(setup,monkeypatch):
    target,repo,_ = setup
    batch = new_batch(setup)
    before = target.path.read_bytes()
    def fail(*args): raise OSError('fictional backup failure')
    monkeypatch.setattr(TaskRepository,'backup',fail)
    with pytest.raises(OSError): apply(target,batch)
    assert target.path.read_bytes()==before


def test_migration_failure_rolls_back_constraint_and_extension(setup,monkeypatch):
    target,repo,_ = setup
    batch = new_batch(setup)
    before = repo.list_tasks(include_completed=True)
    original = TaskRepository._migrate_source_check
    def fail(self,c,**options):
        original(self,c,**options)
        raise RuntimeError('fictional DDL failure')
    monkeypatch.setattr(TaskRepository,'_migrate_source_check',fail)
    with pytest.raises(RuntimeError): apply(target,batch)
    assert repo.list_tasks(include_completed=True)==before
    assert 'zhs_extension' not in tables(target.path)
    with sqlite3.connect(target.path) as c:
        assert "'zhihuishu'" not in c.execute("SELECT sql FROM sqlite_master WHERE name='tasks'").fetchone()[0]


def test_receipt_failure_rolls_back_shared_task_transaction(setup,monkeypatch):
    import pku_deadline_agent.zhihuishu_import as service
    target,repo,_ = setup
    batch = new_batch(setup)
    before = repo.list_tasks(include_completed=True)
    original = service._migrate
    def fail_receipt(repository,c):
        original(repository,c)
        c.execute("CREATE TRIGGER fictional_failure BEFORE INSERT ON zhs_import_receipts BEGIN SELECT RAISE(ABORT,'fictional receipt failure'); END")
    monkeypatch.setattr(service,'_migrate',fail_receipt)
    with pytest.raises(sqlite3.Error): apply(target,batch)
    assert repo.list_tasks(include_completed=True)==before
    assert 'zhs_extension' not in tables(target.path)
    assert len(repo.history(1))==1


def test_backup_includes_committed_wal(setup):
    target,repo,report = setup
    anchor = sqlite3.connect(target.path)
    try:
        anchor.execute('PRAGMA journal_mode=WAL')
        anchor.execute('BEGIN')
        anchor.execute('SELECT COUNT(*) FROM tasks').fetchone()
        task = repo.add_task('虚构 WAL 新任务','虚构课','作业')
        receipt = apply(target,new_batch(setup))
        with sqlite3.connect(receipt.backup) as c:
            assert c.execute('SELECT title FROM tasks WHERE id=?',(task.id,)).fetchone()[0]==task.title
    finally: anchor.close()


def test_local_association_is_reused_only_after_explicit_manual_selection(setup):
    target,repo,report = setup
    first = apply(target,new_batch(setup))
    task = repo.get_task(first.task_ids[0])
    moved = replace(report,tasks=(replace(report.tasks[0],deadline=report.tasks[0].deadline.replace(day=17)),))
    snapshot = build_review(moved,repo.list_tasks(include_completed=True))
    assert prepare_import(target,snapshot,{1:ImportDecision('link',task.id,task.revision)}).associations[0][1] != first.association_ids[0]
    second = prepare_import(target,snapshot,{1:ImportDecision('update',task.id,task.revision,'虚构再次延期')},
                            associations={1:first.association_ids[0]})
    apply(target,second)
    assert len(source_associations(target))==1
    assert repo.get_task(task.id).due_at.day==17
    assert source_associations(target)[0]['observed_due_at'].startswith('2034-06-17')


def test_source_association_cannot_be_rebound_to_another_task(setup):
    target,repo,_ = setup
    first = apply(target,new_batch(setup))
    task = repo.get_task(1)
    batch = prepare_import(target,review(setup),{2:ImportDecision('link',task.id,task.revision)},
                           associations={2:first.association_ids[0]})
    with pytest.raises(ValueError,match='关联冲突'): apply(target,batch)
    assert len(source_associations(target))==1


def test_unknown_date_cannot_clear_existing_deadline_or_save_without_ack(setup):
    target,repo,_ = setup
    for choices,unknown in (({4:ImportDecision('new')},frozenset()),
                            ({4:ImportDecision('update',1,0,'虚构原因')},frozenset({4})),
                            ({5:ImportDecision('new')},frozenset({5}))):
        with pytest.raises(ValueError): prepare_import(target,review(setup),choices,unknown_deadlines=unknown)
    assert len(repo.list_tasks(include_completed=True))==7


def test_default_core_initialize_never_activates_zhihuishu_extension(setup):
    target,repo,_ = setup
    repo.initialize()
    assert 'zhs_extension' not in tables(target.path)
    apply(target,new_batch(setup))
    before = source_associations(target)
    repo.initialize()
    assert source_associations(target)==before


def test_formal_database_or_unmarked_existing_database_refused(setup,tmp_path,monkeypatch):
    target,repo,_ = setup
    batch = new_batch(setup)
    monkeypatch.setenv('PKU_DATABASE_PATH',str(target.path))
    before = target.path.read_bytes()
    with pytest.raises(PermissionError): apply(target,batch)
    with pytest.raises(PermissionError): SandboxImportTarget.create(target.path)
    assert target.path.read_bytes()==before
    other = tmp_path/'unmarked.sqlite3'
    TaskRepository(other).initialize()
    with pytest.raises(FileExistsError): SandboxImportTarget.create(other)
    with pytest.raises(sqlite3.Error): SandboxImportTarget.open(other)


def test_batch_tampering_and_database_switch_are_rejected(setup,tmp_path):
    target,repo,_ = setup
    batch = new_batch(setup)
    with pytest.raises(ValueError,match='已变化'):
        apply(target,replace(batch,choices=((1,ImportDecision('new',allow_distinct=True)),)))
    other = SandboxImportTarget.create(tmp_path/'another.sqlite3')
    with pytest.raises(ValueError,match='不属于'): apply(other,batch)


def test_partial_gateway_does_not_promote_reader_or_sync_trust(setup):
    from pku_deadline_agent.sync import SyncPlan,apply_sync_plan
    target,repo,report = setup
    apply(target,new_batch(setup))
    assert not report.coverage.complete and not report.ready_for_sync
    with pytest.raises(ValueError,match='可信'):
        apply_sync_plan(repo,SyncPlan('zhihuishu',warnings=('虚构未完整',)),(),confirmed=True)


def test_credentials_in_fields_are_rejected_before_persistence(setup):
    target,repo,report = setup
    unsafe = replace(report,tasks=(replace(report.tasks[0],title='密码: FictionalPassword123!'),))
    snapshot = build_review(unsafe,repo.list_tasks(include_completed=True))
    batch = prepare_import(target,snapshot,{1:ImportDecision('new')})
    with pytest.raises(ValueError): apply(target,batch)
    assert len(repo.list_tasks(include_completed=True))==7
    assert 'zhs_extension' not in tables(target.path)


def test_update_retains_user_title_when_manually_matching_different_wording(setup):
    target,repo,report = setup
    old = repo.get_task(2)
    changed = replace(report,tasks=(replace(report.tasks[2],title='虚构写作任务补充要求'),))
    batch = prepare_import(target,build_review(changed,repo.list_tasks(include_completed=True)),
                           {1:ImportDecision('update',old.id,old.revision,'虚构人工改期')})
    apply(target,batch)
    assert repo.get_task(old.id).title==old.title and repo.get_task(old.id).due_at.day==12


def test_unknown_extension_version_refuses_changes(setup):
    target,repo,_ = setup
    apply(target,new_batch(setup))
    with sqlite3.connect(target.path) as c: c.execute('UPDATE zhs_extension SET version=99')
    before = repo.list_tasks(include_completed=True)
    batch = prepare_import(target,review(setup),{2:ImportDecision('link',1,0)})
    with pytest.raises(ValueError,match='扩展版本'): apply(target,batch)
    assert repo.list_tasks(include_completed=True)==before


def test_extension_does_not_enable_old_normalized_adapter_sqlite_route(setup):
    from types import SimpleNamespace
    from pku_deadline_agent.zhihuishu_adapter import plan_zhihuishu
    from pku_deadline_agent.sources import SourceUnsupported
    target,repo,_ = setup
    apply(target,new_batch(setup))
    snapshot = SimpleNamespace(result=SimpleNamespace(require_trustworthy=lambda:None))
    with pytest.raises(SourceUnsupported,match='SQLite'): plan_zhihuishu(snapshot,repo)


@pytest.mark.parametrize('reason',['source_or_structure_changed','session_auth_required','invalid_or_ambiguous_fields'])
def test_partial_import_does_not_override_broken_source_evidence(setup,reason):
    target,repo,report = setup
    bad = replace(report,coverage=replace(report.coverage,reasons=(reason,)))
    snapshot = build_review(bad,repo.list_tasks(include_completed=True))
    with pytest.raises(ValueError,match='重新核验'):
        prepare_import(target,snapshot,{1:ImportDecision('new')})
    assert 'zhs_extension' not in tables(target.path)
