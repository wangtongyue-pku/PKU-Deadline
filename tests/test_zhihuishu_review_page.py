"""All UI interactions use temporary fictional databases, never real accounts."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.zhihuishu_samples import sample_review_data

APP = Path(__file__).resolve().parents[1]/'app.py'


@pytest.fixture
def setup(tmp_path,monkeypatch):
    path = tmp_path/'fictional.sqlite3'
    repo = TaskRepository(path)
    repo.initialize()
    _, tasks = sample_review_data()
    for task in tasks:
        stored = repo.add_task(task.title,task.category,task.task_type,task.due_at)
        if task.completed: repo.set_completed(stored.id,stored.revision,True,confirmed=True)
    monkeypatch.setenv('PKU_DATABASE_PATH',str(path))
    return path,repo


def open_page():
    app = AppTest.from_file(APP,default_timeout=15)
    app.session_state['page']='智慧树任务预览'
    return app.run()


def select_action(app,index,label):
    app.checkbox(key=f'zhs_review_pick_{index}').check().run()
    app.selectbox(key=f'zhs_review_{index}_action').select(label).run()
    return app


def test_page_has_four_groups_full_fields_and_incomplete_warning(setup):
    app = open_page()
    assert not app.exception
    assert len(app.checkbox)==7
    assert {m.label for m in app.metric}=={'待新增','可能重复','疑似截止时间变更','信息不足'}
    assert any('不能推断' in w.value for w in app.warning)
    assert any('虚构数据' in i.value for i in app.info)
    assert {'课程','任务标题','截止时间（北京时间）','平台状态','来源'} <= set(app.dataframe[0].value.columns)


def test_all_buttons_are_preview_only_and_database_unchanged(setup,monkeypatch):
    path,repo = setup
    before = path.read_bytes()
    def forbidden(*args,**kwargs): raise AssertionError('preview must not initialize or write')
    for name in ('initialize','apply_reviewed_tasks','add_task','edit_task','set_completed','set_deleted'):
        monkeypatch.setattr(TaskRepository,name,forbidden)
    monkeypatch.setattr('pku_deadline_agent.ai_client.HTTPSClient.complete',forbidden)
    monkeypatch.setattr('pku_deadline_agent.grid_session.PlaywrightGridSession.__init__',forbidden)
    app = open_page()
    select_action(app,1,'拟新增独立任务')
    app.button(key='zhs_review_confirm').click().run()
    assert not app.exception
    assert any('没有写入 SQLite' in m.value for m in app.success)
    app.button(key='zhs_review_all').click().run()
    assert all(c.value for c in app.checkbox if c.key.startswith('zhs_review_pick_'))
    app.button(key='zhs_review_none').click().run()
    assert all(not c.value for c in app.checkbox if c.key.startswith('zhs_review_pick_'))
    assert 'zhs_review_confirmed' not in app.session_state
    assert path.read_bytes()==before


def test_manual_target_has_no_default_and_change_requires_reason(setup):
    path,repo = setup
    before = repo.list_tasks(include_completed=True)
    app = open_page()
    select_action(app,3,'拟修改已有截止')
    assert app.selectbox(key='zhs_review_3_target').value is None
    assert app.button(key='zhs_review_confirm').disabled
    app.selectbox(key='zhs_review_3_target').select(2).run()
    assert app.button(key='zhs_review_confirm').disabled
    app.text_input(key='zhs_review_3_reason').input('虚构延期通知').run()
    assert not app.button(key='zhs_review_confirm').disabled
    app.button(key='zhs_review_confirm').click().run()
    assert not app.exception and any('模拟方案已核对' in m.value for m in app.success)
    assert repo.list_tasks(include_completed=True)==before
    app.text_input(key='zhs_review_3_reason').input('重新核对的虚构原因').run()
    assert 'zhs_review_confirmed' not in app.session_state


def test_possible_duplicate_new_requires_distinct_acknowledgement(setup):
    app = open_page()
    select_action(app,2,'拟新增独立任务')
    assert app.button(key='zhs_review_confirm').disabled
    app.checkbox(key='zhs_review_2_distinct').check().run()
    assert not app.button(key='zhs_review_confirm').disabled


def test_information_gaps_only_allow_skip(setup):
    app = open_page()
    app.checkbox(key='zhs_review_pick_4').check().run()
    assert app.selectbox(key='zhs_review_4_action').options==['暂不处理']
    assert app.button(key='zhs_review_confirm').disabled


def test_switch_reference_or_leave_page_drops_old_decisions(setup):
    app = open_page()
    select_action(app,1,'拟新增独立任务')
    app.button(key='zhs_review_confirm').click().run()
    app.radio(key='zhs_reference').set_value('虚构待办（体验匹配）').run()
    assert not app.exception
    assert 'zhs_review_confirmed' not in app.session_state
    assert not app.checkbox(key='zhs_review_pick_1').value
    app.session_state['page']='AI 设置'
    app.run()
    assert 'zhs_review_digest' not in app.session_state


def test_external_local_edit_resets_preview_choices(setup):
    _,repo = setup
    app = open_page()
    select_action(app,1,'拟新增独立任务')
    app.button(key='zhs_review_confirm').click().run()
    task = repo.list_tasks(include_completed=True)[0]
    repo.edit_task(task.id,task.revision,title=task.title+'（虚构修改）',category=task.category,
                   task_type=task.task_type,due_at=task.due_at,reason='虚构更新',confirmed=True)
    app.run()
    assert 'zhs_review_confirmed' not in app.session_state
    assert not app.checkbox(key='zhs_review_pick_1').value


def test_missing_database_reports_failure_and_creates_nothing(tmp_path,monkeypatch):
    path = tmp_path/'missing.sqlite3'
    monkeypatch.setenv('PKU_DATABASE_PATH',str(path))
    app = open_page()
    assert not app.exception and any('未将失败当成空库' in e.value for e in app.error)
    assert not path.exists()
    app.radio(key='zhs_reference').set_value('虚构待办（体验匹配）').run()
    assert not app.exception and len(app.checkbox)==7
    assert not path.exists()


def test_latest_real_read_can_be_reviewed_in_the_app_page(tmp_path, monkeypatch):
    """Requirement: the real read shows up in the ordinary app, not a side tool."""
    import json

    from test_zhihuishu_validation import real_report
    from pku_deadline_agent.zhihuishu_validation import encode_report

    payload = tmp_path / 'payload.json'
    payload.write_text(json.dumps(encode_report(real_report()), ensure_ascii=False),
                       encoding='utf-8')
    monkeypatch.setenv('PKU_ZHIHUISHU_PAYLOAD', str(payload))
    formal = tmp_path / 'formal.sqlite3'
    writer = TaskRepository(formal)
    writer.initialize()
    writer.add_task('虚构本地待办', '虚构受限课程', '作业')
    before = formal.read_bytes()
    monkeypatch.setenv('PKU_DATABASE_PATH', str(formal))
    app = AppTest.from_file(APP, default_timeout=15)
    app.session_state['page'] = '智慧树任务预览'
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    assert app.radio(key='zhs_review_source').value == '最近一次真实读取'
    assert any('受限真实读取' in i.value for i in app.info)
    assert any('部分读取' in w.value for w in app.warning)
    assert all(r.key != 'zhs_mode' for r in app.radio)          # no sandbox write mode
    assert app.button(key='zhs_review_production_closed').disabled
    picks = [c for c in app.checkbox if c.key and c.key.startswith('zhs_review_pick_')]
    assert len(picks) == 2                                       # the real read's tasks
    assert any('智慧树（受限真实读取）' in str(df.value.get('来源', [])) for df in app.dataframe)
    assert formal.read_bytes() == before                         # nothing was written

    # Switching to the samples is always available and never writes either.
    app.radio(key='zhs_review_source').set_value('开发样例（虚构）').run()
    assert len([c for c in app.checkbox if c.key and c.key.startswith('zhs_review_pick_')]) == 7
    assert formal.read_bytes() == before


def test_entering_preview_invalidates_other_page_approvals(setup):
    from types import SimpleNamespace
    app = AppTest.from_file(APP,default_timeout=15)
    app.session_state['page']='智慧树任务预览'
    app.session_state['notice_review']=SimpleNamespace(preview='virtual',sent=True)
    app.session_state['notice_confirm']=True
    app.session_state['notice_receipt']='virtual'
    app.session_state['_email_rendered']='学校邮箱'
    app.session_state['email_plan']='virtual'
    app.run()
    assert not app.exception
    assert app.session_state['notice_review'].preview is None
    assert not app.session_state['notice_confirm']
    assert 'notice_receipt' not in app.session_state and 'email_plan' not in app.session_state
