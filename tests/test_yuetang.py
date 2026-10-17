"""Fictional DOM and temporary databases only; no account, AI or network."""
from dataclasses import replace
import pytest
from pku_deadline_agent.yuetang_reader import read_list, parse_due, safe_list_url
from pku_deadline_agent.yuetang_validation import encode_report, decode_report
from pku_deadline_agent.sources import SourceRejected

def markup(count=3):
    cards=''.join(f'''<li class="ant-list-item" style="height:240px"><h4 class="ant-list-item-meta-title"><a>虚构作业{i}</a></h4>
     <div class="homework-description">公布时间：2034-01-01 08:00</div>
     <ul class="ant-list-item-action"><span class="ant-typography-secondary">截止时间：2034-06-{i+10:02d} 20:00</span><span class="ant-tag">未提交</span></ul></li>''' for i in range(count))
    return f'''<h2 class="ant-typography">虚构课程</h2><div class="ant-tabs-tab-active"><div class="ant-tabs-tab-btn">作业</div></div>
     <div class="ant-tabs-tabpane-active"><ul class="ant-list">{cards}</ul></div>'''

@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    from pku_deadline_agent.grid_session import detect_channel
    with sync_playwright() as p:
        browser=p.chromium.launch(channel=detect_channel(),headless=True)
        page=browser.new_page(viewport={'width':1000,'height':650})
        page.route('**/*',lambda route:route.abort())
        yield page
        browser.close()

def test_card_binding_scroll_and_codec(page):
    page.set_content(markup())
    report=read_list(page)
    assert len(report.tasks)==3
    assert [(t.title,t.course,t.deadline.day,t.status) for t in report.tasks]==[(f'虚构作业{i}','虚构课程',i+10,'未完成') for i in range(3)]
    assert not report.coverage.complete
    assert decode_report(encode_report(report))==replace(report,tasks=tuple(replace(t,scan_position=None) for t in report.tasks))

def test_limits_and_ambiguous_or_wrong_tab(page):
    page.set_content(markup(25))
    report=read_list(page,max_tasks=2)
    assert len(report.tasks)<=2 and 'task_limit' in report.coverage.reasons
    page.set_content(markup().replace('>作业<','>考试<'))
    with pytest.raises(Exception):read_list(page)

def test_missing_and_unsupported_fields_remain_unknown(page):
    page.set_content(markup(1).replace('未提交','已截止').replace('2034-06-10 20:00','2034-06-10'))
    task=read_list(page).tasks[0]
    assert task.status=='未知' and task.deadline is None and task.deadline_date=='2034-06-10'

def test_dates_links_and_cross_platform_payload():
    assert parse_due('公布时间：2034-01-01 08:00')==(None,None)
    assert parse_due('截止：2034-02-30 20:00')==(None,None)
    assert safe_list_url('https://class.pku.edu.cn/courses/fake?tab=homework')=='https://class.pku.edu.cn/courses/fake'
    for url in ('http://class.pku.edu.cn/courses/fake','https://class.pku.edu.cn/exam/fake','https://evil.example/courses/fake'):
        with pytest.raises(SourceRejected):safe_list_url(url)
    with pytest.raises(ValueError):decode_report({'source':'zhihuishu','report':{},'links':[]})


def synthetic_report():
    from pku_deadline_agent.zhihuishu_samples import sample_review_data
    report,_=sample_review_data()
    task=replace(report.tasks[0],title='虚构问学作业',source_url='https://class.pku.edu.cn/courses/fake')
    return replace(report,tasks=(task,),source='pku_yuetang')


def test_shared_import_migration_idempotence_and_no_absence_deletion(tmp_path,monkeypatch):
    import sqlite3
    from pku_deadline_agent.database import TaskRepository
    from pku_deadline_agent.zhihuishu_import import SandboxImportTarget, prepare_import, apply_import, source_associations
    from pku_deadline_agent.zhihuishu_review import build_review
    from pku_deadline_agent.task_management import ImportDecision
    monkeypatch.setenv('PKU_DATABASE_PATH',str(tmp_path/'formal.sqlite3'))
    target=SandboxImportTarget.create(tmp_path/'sandbox.sqlite3');repo=TaskRepository(target.path)
    old=repo.add_task('虚构保留任务','其他课程',source='programming_grid')
    report=synthetic_report()
    batch=prepare_import(target,build_review(report,repo.list_tasks()),{1:ImportDecision('new')})
    with pytest.raises(ValueError):apply_import(target,batch,confirmed=True)
    result=apply_import(target,batch,confirmed=True,partial_confirmed=True)
    assert result.backup.exists()
    assert apply_import(target,batch,confirmed=True,partial_confirmed=True).already_applied
    assert len(TaskRepository(target.path).list_tasks())==2
    assert repo.get_task(old.id)==old
    added=repo.get_task(result.task_ids[0]);assert added.source=='pku_yuetang'
    assert len(source_associations(target,'pku_yuetang'))==1 and not source_associations(target)
    # Each opt-in migration preserves the other platform and unrelated rows.
    with repo._connect() as c:
        c.execute('BEGIN IMMEDIATE')
        from pku_deadline_agent.zhihuishu_import import _migrate
        _migrate(repo,c)
    zhs=repo.add_task(added.title,added.category,source='zhihuishu')
    snapshot=build_review(report,repo.list_tasks())
    assert all(m.task.source!='zhihuishu' for m in snapshot.items[0].matches)
    with pytest.raises(ValueError,match='不同平台'):
        prepare_import(target,snapshot,{1:ImportDecision('link',zhs.id,zhs.revision)})
    assert repo.get_task(added.id)==added
    with sqlite3.connect(target.path) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==4
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_import_revision_conflict_rolls_back_extension(tmp_path,monkeypatch):
    from pku_deadline_agent.database import TaskRepository,TaskChanged
    from pku_deadline_agent.zhihuishu_import import SandboxImportTarget,prepare_import,apply_import
    from pku_deadline_agent.zhihuishu_review import build_review
    from pku_deadline_agent.task_management import ImportDecision
    monkeypatch.setenv('PKU_DATABASE_PATH',str(tmp_path/'formal.sqlite3'))
    target=SandboxImportTarget.create(tmp_path/'sandbox.sqlite3');repo=TaskRepository(target.path)
    batch=prepare_import(target,build_review(synthetic_report(),()),{1:ImportDecision('new')})
    repo.add_task('预览之后新增的虚构任务')
    with pytest.raises(TaskChanged):apply_import(target,batch,confirmed=True,partial_confirmed=True)
    assert len(repo.list_tasks())==1
    with repo._connect() as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='yue_extension'").fetchone()


def test_deadline_update_keeps_completion_and_existing_fields(tmp_path,monkeypatch):
    from datetime import timedelta
    from pku_deadline_agent.database import TaskRepository
    from pku_deadline_agent.zhihuishu_import import SandboxImportTarget,prepare_import,apply_import,source_associations
    from pku_deadline_agent.zhihuishu_review import build_review
    from pku_deadline_agent.task_management import ImportDecision
    monkeypatch.setenv('PKU_DATABASE_PATH',str(tmp_path/'formal.sqlite3'))
    target=SandboxImportTarget.create(tmp_path/'sandbox.sqlite3');repo=TaskRepository(target.path)
    report=synthetic_report()
    first=prepare_import(target,build_review(report,()),{1:ImportDecision('new')})
    receipt=apply_import(target,first,confirmed=True,partial_confirmed=True)
    original=repo.get_task(receipt.task_ids[0]);repo.set_completed(original.id,original.revision,True,confirmed=True)
    original=repo.get_task(original.id)
    later=replace(report,tasks=(replace(report.tasks[0],deadline=report.tasks[0].deadline+timedelta(days=1)),))
    snapshot=build_review(later,repo.list_tasks(include_completed=True))
    assert snapshot.items[0].category=='疑似截止时间变更'
    link=source_associations(target,'pku_yuetang')[0]['local_id']
    batch=prepare_import(target,snapshot,{1:ImportDecision('update',original.id,original.revision,'用户核对的延期')},associations={1:link})
    apply_import(target,batch,confirmed=True,partial_confirmed=True)
    after=repo.get_task(original.id)
    assert after.completed and after.title==original.title and after.due_at==later.tasks[0].deadline
    assert len(repo.list_tasks(include_completed=True))==1


def test_unknown_platform_state_is_not_fabricated_or_blocked(tmp_path,monkeypatch):
    from pku_deadline_agent.database import TaskRepository
    from pku_deadline_agent.zhihuishu_import import SandboxImportTarget,prepare_import,apply_import,source_associations
    from pku_deadline_agent.zhihuishu_review import build_review
    from pku_deadline_agent.task_management import ImportDecision
    monkeypatch.setenv('PKU_DATABASE_PATH',str(tmp_path/'formal.sqlite3'))
    target=SandboxImportTarget.create(tmp_path/'sandbox.sqlite3')
    report=synthetic_report();report=replace(report,tasks=(replace(report.tasks[0],status='未知'),))
    snapshot=build_review(report,())
    assert '平台完成状态未知' in snapshot.items[0].warnings
    batch=prepare_import(target,snapshot,{1:ImportDecision('new')})
    apply_import(target,batch,confirmed=True,partial_confirmed=True)
    assert source_associations(target,'pku_yuetang')[0]['platform_status']=='未知'
    assert not TaskRepository(target.path).list_tasks()[0].completed


def test_production_page_wiring_reads_only_and_has_no_demo(tmp_path,monkeypatch):
    import json
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from pku_deadline_agent.database import TaskRepository
    db=tmp_path/'formal.sqlite3';TaskRepository(db).initialize()
    before=db.read_bytes()
    report=replace(synthetic_report(),evidence='authorized-real-validation')
    payload=tmp_path/'read.json';payload.write_text(json.dumps(encode_report(report)),encoding='utf-8')
    monkeypatch.setenv('PKU_DATABASE_PATH',str(db));monkeypatch.setenv('PKU_YUETANG_PAYLOAD',str(payload))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=15)
    app.session_state['page']='问学阅堂';app.run()
    assert not app.exception
    assert any('虚构问学作业' in str(frame.value) for frame in app.dataframe)
    assert any(b.label=='读取问学阅堂' for b in app.button)
    assert not any(b.label=='确认写入正式数据库' for b in app.button)
    assert not any(r.label in ('对比对象','运行方式') for r in app.radio)
    assert db.read_bytes()==before


def test_browser_bridge_separates_platform_and_environment(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    from pku_deadline_agent.zhihuishu_browser import BrowserRead
    calls=[]
    monkeypatch.setenv('UNRELATED_SECRET','fake-secret')
    monkeypatch.setattr('pku_deadline_agent.zhihuishu_browser.subprocess.Popen',lambda args,**kwargs:(calls.append((args,kwargs)) or SimpleNamespace(poll=lambda:0)))
    with pytest.raises(PermissionError):BrowserRead.start(source='pku_yuetang',root=tmp_path)
    job=BrowserRead.start(authorized=True,source='pku_yuetang',root=tmp_path)
    assert calls[0][0][1].endswith('yuetang_real_read.py')
    assert 'UNRELATED_SECRET' not in calls[0][1]['env']
    (job.directory/'status.txt').write_text('done',encoding='utf-8')
    report=replace(synthetic_report(),evidence='authorized-real-validation')
    (job.directory/'report.json').write_text(json.dumps(encode_report(report)),encoding='utf-8')
    assert job.result().source=='pku_yuetang'
    # The ordinary ZHS decoder refuses this platform envelope.
    other=BrowserRead(job.directory,job.process)
    with pytest.raises(ValueError):other.result()


def test_repository_cannot_bypass_cross_platform_guard(tmp_path):
    from pku_deadline_agent.database import TaskRepository
    from pku_deadline_agent.zhihuishu_import import _migrate
    from pku_deadline_agent.extraction import ExtractedTask
    from pku_deadline_agent.task_management import ImportDecision,NoticeOrigin
    repo=TaskRepository(tmp_path/'db.sqlite3');repo.initialize()
    with repo._connect() as c:
        c.execute('BEGIN IMMEDIATE');_migrate(repo,c)
    old=repo.add_task('虚构同名','虚构同课',source='zhihuishu')
    incoming=ExtractedTask(1,old.title,old.category,'作业',None,None,False,())
    assert not repo.suggestions(incoming,NoticeOrigin('pku_yuetang'))
    for action in ('link','update'):
        with pytest.raises(ValueError,match='不同平台'):
            repo.apply_reviewed_tasks((incoming,),(ImportDecision(action,old.id,old.revision,'虚构原因'),),(NoticeOrigin('pku_yuetang'),),confirmed=True)
    assert repo.get_task(old.id)==old


def test_nested_cards_and_ambiguous_titles_fail_closed(page):
    page.set_content(markup(1).replace('</a>','</a><a>虚构第二名称</a>'))
    with pytest.raises(Exception):read_list(page)


def test_missing_course_refuses_guessing(page):
    page.set_content(markup(1).replace('h2 class="ant-typography"','h2'))
    with pytest.raises(Exception):read_list(page)


def test_production_contract_on_fictional_formal_db_preserves_zhihuishu(tmp_path,monkeypatch):
    from pku_deadline_agent.database import TaskRepository
    from pku_deadline_agent.zhihuishu_import import ProductionImportTarget,prepare_import,apply_import,_migrate
    from pku_deadline_agent.zhihuishu_review import build_review
    from pku_deadline_agent.task_management import ImportDecision
    path=tmp_path/'fictional-formal.sqlite3';monkeypatch.setenv('PKU_DATABASE_PATH',str(path))
    repo=TaskRepository(path);repo.initialize()
    with repo._connect() as c:c.execute('BEGIN IMMEDIATE');_migrate(repo,c)
    sample=synthetic_report();t=sample.tasks[0]
    retained=repo.add_task(t.title,t.course,'作业',t.deadline,source='zhihuishu')
    target=ProductionImportTarget.open(path,authorized=True)
    with pytest.raises(PermissionError):prepare_import(target,build_review(sample,repo.list_tasks()),{1:ImportDecision('new')})
    report=replace(sample,evidence='authorized-real-validation')
    snapshot=build_review(report,repo.list_tasks())
    assert not snapshot.items[0].matches
    batch=prepare_import(target,snapshot,{1:ImportDecision('new')})
    receipt=apply_import(target,batch,confirmed=True,partial_confirmed=True)
    assert receipt.backup.exists() and repo.get_task(retained.id)==retained
    assert {t.source for t in TaskRepository(path).list_tasks()}=={'zhihuishu','pku_yuetang'}
    with pytest.raises(ValueError):
        prepare_import(target,build_review(report,repo.list_tasks()),{1:ImportDecision('new')})


def test_invalid_codec_counts_and_credential_title_refused():
    data=encode_report(synthetic_report())
    data['report']['coverage']['scroll_count']=-1
    with pytest.raises(ValueError):decode_report(data)
    data=encode_report(synthetic_report());data['report']['tasks'][0]['title']='password=fictional-password'
    with pytest.raises(SourceRejected):decode_report(data)
