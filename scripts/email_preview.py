r"""A throwaway launcher for the 学校邮箱 page, used to check the real browser.

It runs the page against a fictional database in a separate file so the check
never touches the user's tasks, and it never opens a mailbox: the demo adapter is
the only factory wired in here.

Usage (from the project root):

    .\.venv\Scripts\python.exe -m streamlit run scripts/email_preview.py --server.port 8504
"""

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pku_deadline_agent.database import TaskRepository  # noqa: E402
from pku_deadline_agent.email_page import render_email_page  # noqa: E402

DEMO_DATABASE = Path(__file__).resolve().parents[1] / ".local-tmp" / "email-preview.sqlite3"

st.set_page_config(page_title="学校邮箱预览", page_icon="📬", layout="wide")
st.title("学校邮箱（预览）")
st.caption("这是用于本地检查的独立实例：数据库是虚构的，邮箱是离线演示收件箱。")

repository = TaskRepository(DEMO_DATABASE)
repository.initialize()
render_email_page(repository)
