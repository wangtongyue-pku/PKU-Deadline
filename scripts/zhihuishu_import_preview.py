"""Opt-in REAL SQLite writes to an exclusively created, fictional sandbox ONLY.

Run: python -m streamlit run scripts/zhihuishu_import_preview.py --server.port 8507
Never change PKU_DATABASE_PATH or touch the ordinary application database here.
"""

import sys
from pathlib import Path
import uuid

import streamlit as st

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.zhihuishu_import import SandboxImportTarget
from pku_deadline_agent.zhihuishu_samples import sample_review_data
from pku_deadline_agent.zhihuishu_review_page import render_zhihuishu_preview_page


@st.cache_resource
def sandbox():
    path = Path(__file__).resolve().parents[1]/'.local-tmp'/'zhihuishu-import'/str(uuid.uuid4())/'tasks.sqlite3'
    target = SandboxImportTarget.create(path)
    repo = TaskRepository(path)
    _, local = sample_review_data()
    for task in local:
        saved = repo.add_task(task.title,task.category,task.task_type,task.due_at,
                              source='email' if task.completed else 'manual')
        if task.completed: repo.set_completed(saved.id,saved.revision,True,confirmed=True)
    for source in ('programming_grid','email','wechat'):
        repo.add_task('虚构未在列表出现的'+source,'虚构其他课程','作业',local[0].due_at,source=source)
    return target


st.set_page_config(page_title='智慧树人工入库验收 · 模拟库',layout='wide')
st.caption('独立验收实例：真实 SQLite 事务只操作虚构库，正式任务、邮件和账号均不接触。')
target = sandbox()
render_zhihuishu_preview_page(target.path,import_target=target, development=True)
