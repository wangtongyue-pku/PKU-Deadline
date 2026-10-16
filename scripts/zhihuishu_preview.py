"""Throwaway UI inspection server with only fictional tasks and read-only access.

Run: python -m streamlit run scripts/zhihuishu_preview.py --server.port 8506
The temporary SQLite is built here, never by the preview page itself.
"""

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.zhihuishu_review_page import render_zhihuishu_preview_page
from pku_deadline_agent.zhihuishu_samples import sample_review_data

PREVIEW_DB = Path(__file__).resolve().parents[1]/'.local-tmp'/'zhihuishu-ui-preview.sqlite3'


@st.cache_resource
def prepare_fictional_database():
    repository = TaskRepository(PREVIEW_DB)
    repository.initialize()
    if not repository.list_tasks(include_completed=True):
        _, local = sample_review_data()
        for task in local:
            stored = repository.add_task(task.title,task.category,task.task_type,task.due_at)
            if task.completed:
                repository.set_completed(stored.id,stored.revision,True,confirmed=True)
    return PREVIEW_DB


st.set_page_config(page_title='智慧树差异预览 · 虚构数据',layout='wide')
st.caption('此独立实例的“本地任务”也来自虚构库，不访问正式待办；仅用于界面验收。')
render_zhihuishu_preview_page(prepare_fictional_database())
