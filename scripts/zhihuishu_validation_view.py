"""Show ONE authorized real read in the existing review page. Writes nothing.

This instance is deliberately one-way: it loads the whitelisted payload written by
``scripts/zhihuishu_real_read.py`` and renders the ordinary review page with
``import_target=None``. That means no sandbox write mode exists here, no database
is migrated, the local comparison is read-only, and no model is called.

Run: .\\.venv\\Scripts\\python.exe -m streamlit run scripts/zhihuishu_validation_view.py --server.port 8510
"""

import json
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from pku_deadline_agent.zhihuishu_review_page import render_zhihuishu_preview_page  # noqa: E402
from pku_deadline_agent.zhihuishu_validation import decode_report  # noqa: E402

PAYLOAD = ROOT / '.local-tmp' / 'zhs-validation' / 'report.json'

st.set_page_config(page_title='智慧树受限真实读取 · 只读预览', layout='wide')
st.caption('独立验证实例：只显示一次获授权的受限真实读取结果。不写正式库或沙箱库，'
           '不保存登录状态，不再连接智慧树，不调用 AI。')

if not PAYLOAD.exists():
    st.info('还没有读取结果。请先完成一次受限读取（读取开始前会等待你确认列表已打开）。')
    st.stop()

try:
    report = decode_report(json.loads(PAYLOAD.read_text(encoding='utf-8')))
except (ValueError, OSError) as error:
    st.error(f'读取结果无法核验：{error}')
    st.stop()

render_zhihuishu_preview_page(report=report)
