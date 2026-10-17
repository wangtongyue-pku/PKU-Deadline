"""UI writes only to opt-in marked fictional SQLite, default app stays read-only."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from test_zhihuishu_import import setup, tables
from pku_deadline_agent.database import TaskRepository

SCRIPT = '''import streamlit as st
from pku_deadline_agent.zhihuishu_review_page import render_zhihuishu_preview_page
target = st.session_state['fixture_target']
render_zhihuishu_preview_page(target.path, import_target=target, development=True)
'''


def open_page(setup, *, write=True):
    app = AppTest.from_string(SCRIPT,default_timeout=15)
    app.session_state['fixture_target']=setup[0]
    app.run()
    if write: app.radio(key='zhs_mode').set_value('模拟数据库入库').run()
    assert not app.exception
    return app


def choose(app,index,label):
    app.checkbox(key=f'zhs_review_pick_{index}').check().run()
    app.selectbox(key=f'zhs_review_{index}_action').set_value(label).run()
    return app


def ready(app):
    app.button(key='zhs_review_prepare').click().run()
    confirmation(app,'save_confirm').check().run()
    confirmation(app,'partial_confirm').check().run()
    assert not app.exception
    return app


def confirmation(app,name):
    return next(c for c in app.checkbox if c.key.startswith('zhs_review_'+name+'_'))


def test_default_preview_and_simulated_confirmation_never_write(setup):
    target,repo,_ = setup
    before = target.path.read_bytes()
    app = open_page(setup,write=False)
    choose(app,1,'拟新增独立任务')
    app.button(key='zhs_review_confirm').click().run()
    assert not app.exception and app.button(key='zhs_review_production_closed').disabled
    assert target.path.read_bytes()==before and 'zhs_extension' not in tables(target.path)


def test_partial_confirmation_and_full_before_after_flow(setup,monkeypatch):
    target,repo,_ = setup
    def forbidden(*args,**kwargs): raise AssertionError('must not connect to external service')
    monkeypatch.setattr('pku_deadline_agent.ai_client.HTTPSClient.complete',forbidden)
    monkeypatch.setattr('pku_deadline_agent.grid_session.PlaywrightGridSession.__init__',forbidden)
    app = open_page(setup)
    choose(app,1,'拟新增独立任务')
    choose(app,3,'拟修改已有截止')
    app.selectbox(key='zhs_review_3_target').set_value(2).run()
    app.text_input(key='zhs_review_3_reason').set_value('虚构延期入库验收').run()
    app.button(key='zhs_review_prepare').click().run()
    assert app.button(key='zhs_review_save').disabled
    confirmation(app,'save_confirm').check().run()
    assert app.button(key='zhs_review_save').disabled
    confirmation(app,'partial_confirm').check().run()
    assert not app.button(key='zhs_review_save').disabled
    app.button(key='zhs_review_save').click().run()
    assert not app.exception,[e.value for e in app.error]
    assert len(repo.list_tasks(include_completed=True))==8
    assert repo.get_task(2).due_at.day==12 and repo.get_task(2).completed
    assert any('7 → 8' in m.value for m in app.success)
    assert any('入库后 Deadline 看板' in h.value for h in app.subheader)
    assert app.button(key='zhs_review_save').disabled
    assert not any(c.value for c in app.checkbox if c.key.startswith('zhs_review_pick_'))
    assert not any('未执行旧方案' in w.value for w in app.warning)
    assert not confirmation(app,'save_confirm').value and not confirmation(app,'partial_confirm').value


def test_unknown_due_requires_explicit_ack_and_stays_none(setup):
    target,repo,_ = setup
    app = open_page(setup)
    choose(app,4,'拟新增独立任务')
    assert app.button(key='zhs_review_prepare').disabled
    app.checkbox(key='zhs_review_4_unknown').check().run()
    ready(app)
    app.button(key='zhs_review_save').click().run()
    assert not app.exception
    added = next(t for t in repo.list_tasks(include_completed=True) if t.source=='zhihuishu')
    assert added.due_at is None


def test_changing_choices_withdraws_prepared_batch_and_confirmation(setup):
    app = open_page(setup)
    choose(app,1,'拟新增独立任务')
    ready(app)
    app.checkbox(key='zhs_review_pick_1').uncheck().run()
    assert not app.exception
    assert app.button(key='zhs_review_save').disabled
    assert 'zhs_review_pending' not in app.session_state
    assert len(setup[1].list_tasks(include_completed=True))==7


def test_concurrent_edit_before_save_is_reported_without_import(setup):
    target,repo,_ = setup
    app = open_page(setup)
    choose(app,1,'拟新增独立任务')
    ready(app)
    old = repo.get_task(1)
    repo.edit_task(old.id,old.revision,title=old.title+'虚构用户修改',category=old.category,
        task_type=old.task_type,due_at=old.due_at,reason='虚构并发修改',confirmed=True)
    app.button(key='zhs_review_save').click().run()
    assert not app.exception
    assert any('旧入库确认已撤销' in w.value for w in app.warning)
    assert len(repo.list_tasks(include_completed=True))==7


def test_reference_or_mode_switch_disables_write(setup):
    app = open_page(setup)
    choose(app,1,'拟新增独立任务')
    ready(app)
    app.radio(key='zhs_reference').set_value('虚构待办（体验匹配）').run()
    assert not app.exception
    assert app.button(key='zhs_review_production_closed').disabled
    assert 'zhs_review_pending' not in app.session_state


def test_same_task_appears_on_existing_deadline_board_after_import(setup,monkeypatch):
    from pku_deadline_agent.zhihuishu_import import prepare_import,apply_import
    from pku_deadline_agent.zhihuishu_review import build_review
    from pku_deadline_agent.task_management import ImportDecision
    target,repo,report = setup
    batch = prepare_import(target,build_review(report,repo.list_tasks(include_completed=True)),{1:ImportDecision('new')})
    receipt = apply_import(target,batch,confirmed=True,partial_confirmed=True)
    monkeypatch.setenv('PKU_DATABASE_PATH',str(target.path))
    app = AppTest.from_file(Path(__file__).resolve().parents[1]/'app.py',default_timeout=15).run()
    assert not app.exception
    assert any(receipt.task_ids[0] in list(df.value.get('编号',[])) for df in app.dataframe)


def test_production_app_has_no_active_write_or_mode_switch(setup,monkeypatch):
    target,repo,_ = setup
    # A virtual user database without sandbox marker, independent of the target.
    formal = target.path.parent.parent/'formal.sqlite3'
    writer = TaskRepository(formal)
    writer.initialize()
    writer.add_task('虚构正式库待办','虚构课','作业')
    before = formal.read_bytes()
    monkeypatch.setenv('PKU_DATABASE_PATH',str(formal))
    app = AppTest.from_file(Path(__file__).resolve().parents[1]/'app.py',default_timeout=15)
    app.session_state['page']='智慧树任务预览'
    app.run()
    assert not app.exception and any('还没有可用' in i.value for i in app.info)
    assert not any(b.key in ('zhs_review_prepare','zhs_review_save') for b in app.button)
    assert all(r.key!='zhs_mode' for r in app.radio)
    assert formal.read_bytes()==before


def test_new_batch_never_reuses_previous_batch_confirmation(setup):
    app = open_page(setup)
    choose(app,1,'拟新增独立任务')
    ready(app)
    old_key = confirmation(app,'save_confirm').key
    # Re-preparing an otherwise identical plan is a new authorization scope.
    app.button(key='zhs_review_prepare').click().run()
    assert not app.exception
    assert confirmation(app,'save_confirm').key != old_key
    assert not confirmation(app,'save_confirm').value and not confirmation(app,'partial_confirm').value
    assert app.button(key='zhs_review_save').disabled
