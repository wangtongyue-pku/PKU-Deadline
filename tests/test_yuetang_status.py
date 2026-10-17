"""Submission-only visibility rules, fictional DOM and read-only UI regression."""
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from pku_deadline_agent.time_utils import BEIJING
from pku_deadline_agent.yuetang_status import submission_label, submission_status
from test_yuetang import synthetic_report, markup, page


@pytest.mark.parametrize(('label','expected'), [
    ('已提交','已完成'), ('已提交（待批改）','已完成'), ('已批改','已完成'), ('已完成','已完成'),
    ('未提交','未完成'), ('逾期未提交','未完成'), (' 已逾期 未提交 ','未完成'), ('未完成','未完成'),
    ('进行中','未知'), ('已截止','未知'), ('提交作业','未知'),
    ('可重新提交','未知'), ('未知标签','未知'),
])
def test_only_explicit_submission_state_is_recognized(label, expected):
    assert submission_status(label)==expected


def test_conflicting_status_not_hidden():
    assert submission_status(['已提交','未提交'])=='未知'


def test_overdue_does_not_override_submitted_or_fabricate_state():
    now=datetime(2034,6,15,12,tzinfo=BEIJING)
    task=synthetic_report().tasks[0]
    for status,expected in [('已完成','已提交'),('未完成','逾期未提交'),('未知','截止已过 · 提交状态待确认')]:
        assert submission_label(replace(task,status=status,deadline=now-timedelta(hours=1)),now)==expected
    assert submission_label(replace(task,status='未完成',deadline=now+timedelta(hours=1)),now)=='未提交'
    assert submission_label(replace(task,status='未知',deadline=None),now)=='提交状态待确认'


def test_composite_status_remains_card_local(page):
    from pku_deadline_agent.yuetang_reader import read_list
    page.set_content(markup(2).replace('>未提交<','>已提交（待批改）<',1))
    assert [t.status for t in read_list(page).tasks]==['已完成','未完成']


def test_completion_markers_with_icon_do_not_use_description_text(page):
    from pku_deadline_agent.yuetang_reader import read_list
    html=markup(2).replace('>未提交<','><svg width="10" height="10"></svg>已完成<',1)
    html=html.replace('公布时间：2034-01-01 08:00','正文示例：已提交不代表这条作业的状态')
    page.set_content(html)
    assert [t.status for t in read_list(page).tasks]==['已完成','未完成']


def mixed_report():
    report=synthetic_report()
    base=report.tasks[0]
    tasks=tuple(replace(base,title=title,status=status,deadline=due) for title,status,due in (
        ('虚构已交近期任务','已完成',datetime(2034,7,1,20,tzinfo=BEIJING)),
        ('虚构未交近期任务','未完成',datetime(2034,7,1,20,tzinfo=BEIJING)),
        ('虚构逾期未交任务','未完成',datetime(2020,7,1,20,tzinfo=BEIJING)),
        ('虚构未知提交任务','未知',datetime(2020,7,1,20,tzinfo=BEIJING)),
        ('虚构逾期已交任务','已完成',datetime(2020,7,1,20,tzinfo=BEIJING)),
    ))
    return replace(report,tasks=tasks,evidence='authorized-real-validation')


def ui(tmp_path,monkeypatch,report):
    from streamlit.testing.v1 import AppTest
    from pku_deadline_agent.database import TaskRepository
    from pku_deadline_agent.yuetang_validation import encode_report
    db=tmp_path/'formal.sqlite3';TaskRepository(db).initialize()
    payload=tmp_path/'fictional.json';payload.write_text(json.dumps(encode_report(report)),encoding='utf-8')
    monkeypatch.setenv('PKU_DATABASE_PATH',str(db));monkeypatch.setenv('PKU_YUETANG_PAYLOAD',str(payload))
    before=db.read_bytes()
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=15)
    app.session_state['page']='问学阅堂';app.run()
    return app,db,before


def test_default_hidden_submitted_with_stable_indices_and_no_writes(tmp_path,monkeypatch):
    app,db,before=ui(tmp_path,monkeypatch,mixed_report())
    assert not app.exception
    frame=app.dataframe[0].value
    assert list(frame['编号'])==[2,3,4]
    assert '平台状态' not in frame.columns
    assert not any('平台完成状态未知' in warning.value for warning in app.warning)
    assert not any('状态：未知' in item.value for item in app.text)
    assert list(frame['任务标题'])==['虚构未交近期任务','虚构逾期未交任务','虚构未知提交任务']
    assert app.selectbox(key='zhs_review_2_action').value=='拟新增独立任务'
    assert not app.checkbox(key='zhs_review_pick_2').value
    assert not any(b.label=='确认写入正式数据库' for b in app.button)
    assert {c.key for c in app.checkbox if c.key.startswith('zhs_review_pick_')}=={
        'zhs_review_pick_2','zhs_review_pick_3','zhs_review_pick_4'}
    app.checkbox(key='yue_show_submitted').check().run()
    assert not app.exception and len(app.dataframe[0].value)==5
    app.checkbox(key='zhs_review_pick_1').check().run()
    app.checkbox(key='yue_show_submitted').uncheck().run()
    assert not app.exception and list(app.dataframe[0].value['编号'])==[2,3,4]
    assert db.read_bytes()==before


def test_all_submitted_does_not_claim_complete_or_show_import(tmp_path,monkeypatch):
    report=mixed_report();report=replace(report,tasks=tuple(t for t in report.tasks if t.status=='已完成'))
    app,db,before=ui(tmp_path,monkeypatch,report)
    assert not app.exception
    assert any('不代表整课已完整读取' in info.value for info in app.info)
    assert not any(b.key=='zhs_review_prepare' for b in app.button)
    app.checkbox(key='yue_show_submitted').check().run()
    assert not app.exception and len(app.dataframe[0].value)==2
    assert db.read_bytes()==before
