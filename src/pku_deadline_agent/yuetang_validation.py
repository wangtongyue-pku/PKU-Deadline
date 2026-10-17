"""Separate minimized local payload; never accepts another platform's report."""
from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
from pku_deadline_agent.zhihuishu_validation import encode_report as _encode, decode_report as _decode
from pku_deadline_agent.zhihuishu_reader import _text
from pku_deadline_agent.sources import SourceRejected
from pku_deadline_agent.yuetang_reader import SOURCE, safe_list_url

def payload_path():
    return Path(os.environ.get('PKU_YUETANG_PAYLOAD') or Path(__file__).resolve().parents[2]/'.local-tmp/yuetang-current.json')

def encode_report(report):
    if report.source!=SOURCE:raise ValueError('问学阅堂来源不匹配。')
    return {'source':SOURCE,'report':_encode(report),'links':[safe_list_url(t.source_url) for t in report.tasks]}

def decode_report(data):
    if not isinstance(data,dict) or set(data)!={'source','report','links'} or data['source']!=SOURCE:
        raise ValueError('问学阅堂载荷来源或结构不匹配。')
    report=_decode(data['report']);links=data['links']
    for name in ('loaded_peak','loaded_final','observed_count','read_count','scroll_count','unread_minimum'):
        value=getattr(report.coverage,name)
        if type(value) is not int or value<0:raise ValueError('读取统计无效。')
    for name in ('bottom_settled','inventory_complete','fields_complete','complete'):
        if type(getattr(report.coverage,name)) is not bool:raise ValueError('读取完整性标记无效。')
    if report.coverage.scroll_count>12:raise ValueError('超出本次滚动范围。')
    if not isinstance(links,list) or len(links)!=len(report.tasks) or len(links)>20:raise ValueError('读取项数无效。')
    tasks=[]
    for task,link in zip(report.tasks,links):
        title=_text(task.title,200) or '';course=_text(task.course,100)
        if task.deadline_date is not None:
            date.fromisoformat(task.deadline_date)
        if task.status not in ('未知','未完成','已完成') or task.kind!='homework':raise ValueError('作业字段无效。')
        tasks.append(replace(task,title=title,course=course,source_url=safe_list_url(link)))
    return replace(report,tasks=tuple(tasks),course=_text(report.course,100),source=SOURCE)

def load_latest(path=None):
    try:
        path=Path(path) if path is not None else payload_path()
        if path.stat().st_size>256000:return None
        return decode_report(json.loads(path.read_text(encoding='utf-8')))
    except (OSError,ValueError,TypeError,SourceRejected):return None
