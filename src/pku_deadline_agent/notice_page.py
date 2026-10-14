"""Standalone local privacy check. No simulated send button or outside client."""

from datetime import datetime

import streamlit as st

from pku_deadline_agent.notices import INPUT_MODES, MAX_INPUT_LENGTH
from pku_deadline_agent.preview import ReviewSession
from pku_deadline_agent.time_utils import BEIJING, beijing_now, format_deadline


def _invalidate_review():
    review = st.session_state.get("notice_review")
    if review:
        review.preview = None


def _clear_local_input():
    st.session_state["notice_raw"] = ""
    st.session_state["notice_date"] = None
    st.session_state["notice_time"] = None
    st.session_state.pop("notice_reference", None)
    st.session_state["notice_review"] = ReviewSession()


def render_notice_page():
    st.subheader("通知隐私检查")
    st.caption("本页只做本地检查，不发送、不保存任务。要提取任务，请使用「Deadline 智能解析」。")
    st.warning("自动检测可能遗漏，请只粘贴必要通知并核对脱敏结果；认证凭据和未消除风险不能放行。")
    mode_label = st.radio("输入模式", tuple(INPUT_MODES.values()), key="notice_mode", on_change=_invalidate_review, horizontal=True)
    mode = next(key for key, label in INPUT_MODES.items() if label == mode_label)
    raw = st.text_area("待筛选通知（仅本地）", key="notice_raw", max_chars=MAX_INPUT_LENGTH, height=180, on_change=_invalidate_review)
    with st.expander("历史时间与输入说明", expanded=False):
        date = st.date_input("共同发布日期（选填，北京时间）", value=None, key="notice_date", on_change=_invalidate_review)
        time = st.time_input("共同发布时刻（选填，北京时间）", value=None, key="notice_time", on_change=_invalidate_review)
        st.caption("默认北京时间今天、缺年份按今年。多条通知用独立一行 --- 分隔；结构化格式可用 标题: / 课程: / 类型: / 截止时间:。")
    if "notice_reference" not in st.session_state:
        st.session_state["notice_reference"] = beijing_now().replace(microsecond=0)
    published = datetime.combine(date, time, tzinfo=BEIJING) if date and time else st.session_state["notice_reference"]
    complete = (date is None) == (time is None)
    if not complete:
        st.error("日期和时刻请一起填写或一起留空。")
    if not isinstance(st.session_state.get("notice_review"), ReviewSession):
        st.session_state["notice_review"] = ReviewSession()
    review = st.session_state["notice_review"]
    review.observe(raw, published, "DeepSeek", mode=mode)
    if st.button("本地扫描并生成预览", key="notice_scan", disabled=not complete or not raw.strip()):
        if date is None and time is None:
            published = beijing_now().replace(microsecond=0)
            st.session_state["notice_reference"] = published
        review.prepare(raw, published, "DeepSeek", mode=mode)
    st.button("清空本地输入", key="notice_clear", on_click=_clear_local_input)
    preview = review.preview
    if preview:
        for error in preview.batch.errors:
            st.error(error)
        for warning in preview.batch.warnings:
            st.warning(warning)
        st.caption(f"已排除 {preview.batch.ignored_lines} 个非必要片段、{preview.batch.skipped_notices} 条无可用内容的通知。")
        for notice in preview.batch.notices:
            st.caption(f"通知 {notice.source_index} · 北京时间基准 {format_deadline(notice.published_at)}")
            st.code(notice.sanitized_text if mode == "natural" else str(notice.to_payload()), language="text")
        if preview.request_json is None:
            st.error("存在隐私风险或没有可用内容，请修改后重试。")
        else:
            st.success("本地检查完成。结果仍需人工核对，本页没有任何发送操作。")
