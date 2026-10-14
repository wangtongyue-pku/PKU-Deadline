"""Everyday flow: prepare, approve upload once, save batch once."""

import sqlite3
from datetime import datetime

import streamlit as st

from pku_deadline_agent.config import database_path
from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.notices import INPUT_MODES, MAX_INPUT_LENGTH
from pku_deadline_agent.parse_service import ParseSession
from pku_deadline_agent.parse_controls import local_settings, render_settings, render_preview, render_results
from pku_deadline_agent.time_utils import BEIJING, beijing_now


def invalidate_parse():
    session = st.session_state.get("parse_session")
    if session is not None:
        session.invalidate()
    st.session_state.pop("parse_saved", None)


def _clear():
    invalidate_parse()
    st.session_state["parse_raw"] = ""
    st.session_state["parse_date"] = None
    st.session_state["parse_time"] = None
    st.session_state.pop("parse_reference", None)
    st.session_state["parse_session"] = ParseSession()


def render_parse_page():
    st.subheader("Deadline 智能解析")
    st.caption("粘贴通知 → 自动整理 → 查看脱敏预览后发送 → 批量保存。明确内容优先在本地处理。")
    st.caption("只粘贴必要通知；本地脱敏可能遗漏，请核对发送预览。密码、Cookie、Token 等认证资料会阻断。")
    session = st.session_state.get("parse_session")
    if not isinstance(session, ParseSession):
        session = st.session_state["parse_session"] = ParseSession()
    live_settings, provider, model = render_settings()
    settings = live_settings or local_settings(provider, model)
    with st.expander("输入格式与历史时间（可选）", expanded=False):
        mode_label = st.radio("输入模式", tuple(INPUT_MODES.values()), key="parse_mode", on_change=invalidate_parse, horizontal=True)
        date = st.date_input("通知发布日期（北京，选填）", value=None, key="parse_date", on_change=invalidate_parse)
        time = st.time_input("通知发布时刻（北京，选填）", value=None, key="parse_time", on_change=invalidate_parse)
        st.caption("默认北京时间今天、缺年份按今年；历史通知可改这里。多条通知用独立一行 --- 分隔。")
    mode = next(key for key, label in INPUT_MODES.items() if label == mode_label)
    raw = st.text_area("待解析通知（仅本地）", key="parse_raw", max_chars=MAX_INPUT_LENGTH, height=160, on_change=invalidate_parse, placeholder="直接粘贴微信或课程通知，不必先填写标题、课程或日期。")
    complete = (date is None) == (time is None)
    if not complete:
        st.error("历史日期和时刻请一起填写，或一起留空。")
    if "parse_reference" not in st.session_state:
        st.session_state["parse_reference"] = beijing_now().replace(microsecond=0)
    published = datetime.combine(date, time, tzinfo=BEIJING) if date and time else st.session_state["parse_reference"]
    session.observe(raw, published, mode, settings)
    if st.button("智能提取", key="parse_scan", disabled=not complete or not raw.strip(), type="primary"):
        if date is None and time is None:
            published = beijing_now().replace(microsecond=0)
            st.session_state["parse_reference"] = published
        try:
            plan = session.prepare(raw, published, mode, settings)
            st.session_state["parse_generation"] = st.session_state.get("parse_generation", 0) + 1
            if plan.route == "local":
                session.run(raw, published, mode, settings, None, confirmed=True)
        except ValueError as error:
            st.error(str(error))
    st.button("清空", key="parse_clear", on_click=_clear)
    path = database_path()
    try:
        render_preview(session, settings, raw, published, mode, path, live_available=live_settings is not None)
        if session.results is not None:
            repository = TaskRepository(path)
            repository.initialize()
            render_results(session, repository, raw, published, mode, settings)
    except (sqlite3.Error, OSError, RuntimeError):
        st.error("本地存储暂不可用，未保存任务，请检查权限和空间。")
