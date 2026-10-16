"""Preview-only review, fictional SQLite, no platform/browser/AI connection."""

from dataclasses import replace
import sqlite3

import pytest

from pku_deadline_agent.database import TaskRepository, ReadOnlyTaskRepository
from pku_deadline_agent.task_management import ImportDecision
from pku_deadline_agent.zhihuishu_review import build_review, simulate_choices
from pku_deadline_agent.zhihuishu_samples import sample_review_data


@pytest.fixture
def snapshot():
    report, local = sample_review_data()
    return build_review(report, local)


def test_four_groups_and_namesakes_are_preserved(snapshot):
    assert [i.category for i in snapshot.items[:5]] == [
        '待新增', '可能重复', '疑似截止时间变更', '信息不足', '信息不足']
    assert len(snapshot.items) == 7
    assert snapshot.items[5].batch_peers == (7,)
    assert len(snapshot.items[5].matches) == 2
    assert not snapshot.report.coverage.complete


def test_fuzzy_existing_match_reuses_common_matcher(snapshot):
    t = snapshot.report.tasks[0]
    local = replace(snapshot.local_tasks[0], title=t.title+'补充')
    result = build_review(replace(snapshot.report, tasks=(t,)), [local])
    assert result.items[0].category == '可能重复'
    assert result.items[0].matches[0].kind == 'possible'


def test_incoming_or_missing_task_does_not_infer_local_completion(snapshot):
    untouched = replace(snapshot.local_tasks[0], id=99, title='虚构未出现作业')
    result = build_review(snapshot.report, snapshot.local_tasks+(untouched,))
    plan = simulate_choices(result, {1: ImportDecision('new')})
    assert all(op.target is None for op in plan.operations)
    assert not untouched.completed and result.local_tasks[-1] == untouched
    assert plan.simulation_only and not plan.list_complete
    assert not hasattr(plan, 'actions')  # Not an actionable SyncPlan.


def test_missing_date_and_course_are_not_filled(snapshot):
    assert snapshot.items[3].task.deadline is None
    assert snapshot.items[3].task.deadline_date == '2034-06-15'
    assert snapshot.items[4].task.course is None
    with pytest.raises(ValueError, match='信息不足'):
        simulate_choices(snapshot, {4: ImportDecision('new')})


@pytest.mark.parametrize('change', [{'course':None}, {'course':'虚构另课'}])
def test_uncertain_batch_context_is_not_trusted(snapshot, change):
    result = build_review(replace(snapshot.report, **change), snapshot.local_tasks)
    assert all(i.category == '信息不足' for i in result.items)


def test_unknown_status_and_unverified_task_kind_are_information_gaps(snapshot):
    for task in (replace(snapshot.items[0].task, status=''), replace(snapshot.items[0].task, kind='exam')):
        result = build_review(replace(snapshot.report, tasks=(task,)), snapshot.local_tasks)
        assert result.items[0].category == '信息不足'


def test_observed_id_never_selects_a_target_or_merges(snapshot):
    t = replace(snapshot.items[1].task, observed_task_id='aaaa', identity_verified=False)
    result = build_review(replace(snapshot.report, tasks=(t,t)), snapshot.local_tasks)
    assert len(result.items) == 2
    assert all(i.category == '可能重复' for i in result.items)
    assert simulate_choices(result, {}).operations == ()


def test_duplicate_independent_new_needs_explicit_distinct_decision(snapshot):
    with pytest.raises(ValueError, match='独立新增'):
        simulate_choices(snapshot, {2: ImportDecision('new')})
    result = simulate_choices(snapshot, {2: ImportDecision('new', allow_distinct=True)})
    assert result.operations[0].target is None


def test_deadline_change_simulation_keeps_existing_completed_state(snapshot):
    old = snapshot.local_tasks[1]
    result = simulate_choices(snapshot, {3: ImportDecision('update', old.id, old.revision, '虚构延期通知')})
    op = result.operations[0]
    assert old.completed and op.target == old
    assert op.target.due_at.day == 10 and op.incoming.deadline.day == 12
    assert snapshot.local_tasks[1] == old


def test_link_differs_from_update_and_keeps_original_fields(snapshot):
    old = snapshot.local_tasks[1]
    plan = simulate_choices(snapshot, {3: ImportDecision('link', old.id, old.revision)})
    assert plan.operations[0].action == 'link'
    assert plan.operations[0].target.due_at != plan.operations[0].incoming.deadline


@pytest.mark.parametrize('decision, message', [
    (ImportDecision('update',2,0,''), '原因'),
    (ImportDecision('update',999,0,'虚构原因'), '目标'),
    (ImportDecision('link',2,99), '版本'),
    (ImportDecision('delete'), '无效'),
    (ImportDecision('new',2), '拟新增'),
])
def test_invalid_decisions_are_rejected(snapshot,decision,message):
    with pytest.raises(ValueError, match=message):
        simulate_choices(snapshot, {3:decision})


def test_one_local_target_cannot_receive_two_incoming_items(snapshot):
    with pytest.raises(ValueError, match='同一本地任务'):
        simulate_choices(snapshot, {6:ImportDecision('link',3,0),7:ImportDecision('update',3,0,'虚构原因')})


def test_same_deadline_has_no_update_and_cross_course_cannot_link(snapshot):
    with pytest.raises(ValueError,match='截止相同'):
        simulate_choices(snapshot,{2:ImportDecision('update',1,0,'虚构原因')})
    local = replace(snapshot.local_tasks[0],category='虚构另一门课')
    changed = build_review(snapshot.report,(local,))
    with pytest.raises(ValueError,match='课程或类型'):
        simulate_choices(changed,{2:ImportDecision('link',1,0)})


def test_revisions_and_data_changes_invalidate_snapshot_digest(snapshot):
    local = (replace(snapshot.local_tasks[0],revision=1),*snapshot.local_tasks[1:])
    assert build_review(snapshot.report,local).digest != snapshot.digest
    assert '虚构' not in repr(snapshot)
    assert '虚构' not in repr(simulate_choices(snapshot,{1:ImportDecision('new')}))


def test_deleted_local_tasks_excluded_and_completed_kept(snapshot):
    local = tuple(replace(t, deleted_at=t.updated_at) if t.id==1 else t for t in snapshot.local_tasks)
    result = build_review(snapshot.report,local)
    assert len(result.local_tasks)==3 and any(t.completed for t in result.local_tasks)


def test_limit_and_naive_deadline_are_rejected(snapshot):
    with pytest.raises(ValueError,match='20'):
        build_review(replace(snapshot.report,tasks=(snapshot.items[0].task,)*21),())
    task = replace(snapshot.items[0].task,deadline=snapshot.items[0].task.deadline.replace(tzinfo=None))
    with pytest.raises(ValueError,match='时区'):
        build_review(replace(snapshot.report,tasks=(task,)),())


def test_readonly_repository_reuses_query_without_writes(tmp_path):
    path = tmp_path/'tasks.sqlite3'
    writer = TaskRepository(path)
    writer.initialize()
    writer.add_task('虚构待办','虚构课','作业')
    before = path.read_bytes()
    reader = ReadOnlyTaskRepository(path)
    assert reader.list_tasks(include_completed=True) == writer.list_tasks(include_completed=True)
    with reader._connect() as connection:
        with pytest.raises(sqlite3.OperationalError,match='readonly'):
            connection.execute("UPDATE tasks SET title='不应写入'")
    assert path.read_bytes() == before
    assert not hasattr(reader,'initialize') and not hasattr(reader,'apply_reviewed_tasks')


def test_missing_or_old_database_is_not_created_or_migrated(tmp_path):
    missing = tmp_path/'missing.sqlite3'
    with pytest.raises(sqlite3.Error): ReadOnlyTaskRepository(missing).list_tasks()
    assert not missing.exists()
    old = tmp_path/'old.sqlite3'
    with sqlite3.connect(old) as c:
        c.execute('PRAGMA user_version=3')
    before = old.read_bytes()
    with pytest.raises(RuntimeError,match='迁移'): ReadOnlyTaskRepository(old).list_tasks()
    assert old.read_bytes() == before
    assert not list(tmp_path.glob('backups/*'))


def test_readonly_includes_committed_wal_tasks(tmp_path):
    path = tmp_path/'wal.sqlite3'
    repo = TaskRepository(path)
    repo.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('BEGIN')
        connection.execute('SELECT COUNT(*) FROM tasks').fetchone()
        task = repo.add_task('虚构 WAL 待办','虚构课','作业')
        assert (tmp_path/'wal.sqlite3-wal').exists()
        assert ReadOnlyTaskRepository(path).list_tasks()[0].id == task.id
