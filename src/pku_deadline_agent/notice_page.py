"""Streamlit UI for local review. No database writes or remote API clients."""

from datetime import datetime

import streamlit as st

from pku_deadline_agent.notices import INPUT_MODES, MAX_INPUT_LENGTH
from pku_deadline_agent.preview import MOCK_MODELS, ReviewSession
from pku_deadline_agent.time_utils import BEIJING, beijing_now, format_deadline


def _invalidate_review() -> None:
    review = st.session_state.get("notice_review")
    if review is not None:
        review.preview = None
        review.sent = False
    st.session_state["notice_confirm"] = False
    st.session_state.pop("notice_receipt", None)


def _clear_local_input() -> None:
    st.session_state["notice_raw"] = ""
    st.session_state["notice_date"] = None
    st.session_state["notice_time"] = None
    st.session_state.pop("notice_reference", None)
    st.session_state["notice_review"] = ReviewSession()
    _invalidate_review()


def render_notice_page() -> None:
    st.subheader("通知隐私过滤与发送预览")
    st.info("当前仅使用本地模拟 API，不读取密钥、不请求 DeepSeek / MiMo，也不消耗模型额度。模拟发送不会创建待办。")
    st.warning("自动检测可能遗漏或误报，请逐项审核。请勿粘贴真实认证凭据或整段私人聊天；只选择必要的通知。")
    st.caption("通知和预览仅在当前会话内存中处理，不写入 SQLite 或应用日志。离开此页会撤销预览；可用“清空本地输入”清除会话内容。")
    mode_label = st.radio("输入模式", tuple(INPUT_MODES.values()), key="notice_mode", on_change=_invalidate_review, horizontal=True)
    mode = next(key for key, label in INPUT_MODES.items() if label == mode_label)
    if mode == "natural":
        st.write("直接粘贴课程或活动通知即可，无需预填标题。程序先移除常见姓名、联系方式和明显闲聊，保留任务、日期及延期片段供审核。")
        st.caption("姓名检测依赖上下文，可能遗漏或误删。未能安全处理的片段必须修改原文或切换结构化输入，不能勾选确认强制放行。")
    with st.expander("白名单格式与批量输入说明"):
        st.write("两种模式均支持用单独一行 --- 分隔多条通知。自然语言模式保留本地筛选后的通知片段；结构化模式只提取以下明确字段，可作为隐私保护的备用方案。")
        st.code(
            "标题: 完成虚构课程练习\n课程: 虚构课程\n类型: 作业\n截止时间: 下周三 20:00\n发布时间: 2026-10-11 09:00\n---\n标题: 虚构活动报名\n活动: 虚构社团\n类型: 活动报名\n截止时间: 2026-10-22 18:00",
            language="text",
        )
        st.caption("类型仅允许：作业、考试、活动报名、其他。发布时间可省略，默认生成预览时的北京时间；每条通知内明确写出的发布时间优先于共同时间。")

    if not isinstance(st.session_state.get("notice_review"), ReviewSession):
        st.session_state["notice_review"] = ReviewSession()
        st.session_state["notice_confirm"] = False
        st.session_state.pop("notice_receipt", None)
    if st.session_state.pop("notice_clear_confirm", False):
        st.session_state["notice_confirm"] = False
    review = st.session_state["notice_review"]
    raw = st.text_area(
        "待筛选通知（仅本地）", key="notice_raw", height=220, max_chars=MAX_INPUT_LENGTH,
        placeholder="例如：大家好，《虚构课程》第三次作业延期至下周三晚20:00，请在教学平台提交。",
        on_change=_invalidate_review,
    )
    provider = st.selectbox("未来模型提供方（当前均为本地模拟）", tuple(MOCK_MODELS), key="notice_provider", on_change=_invalidate_review)
    st.caption("无需填写发布时间：默认按生成预览时的北京时间。原文中的明确发布时间优先。")
    with st.expander("历史通知：可选修改共同时间基准"):
        date_column, time_column = st.columns(2)
        publication_date = date_column.date_input("共同发布日期（选填，北京时间）", value=None, key="notice_date", on_change=_invalidate_review)
        publication_time = time_column.time_input("共同发布时刻（选填，北京时间）", value=None, key="notice_time", on_change=_invalidate_review)
    publication_complete = (publication_date is None) == (publication_time is None)
    if "notice_reference" not in st.session_state:
        st.session_state["notice_reference"] = beijing_now().replace(microsecond=0)
    published_at = (
        datetime.combine(publication_date, publication_time, tzinfo=BEIJING)
        if publication_date is not None and publication_time is not None else st.session_state["notice_reference"]
    )
    if not publication_complete:
        st.error("指定时间不完整：请补全日期和时刻，或同时清空以使用默认北京时间。")
    if review.observe(raw, published_at, provider, mode=mode):
        st.session_state["notice_confirm"] = False
        st.session_state.pop("notice_receipt", None)
    if st.button("本地扫描并生成预览", key="notice_scan", disabled=not publication_complete, type="primary"):
        if publication_date is None and publication_time is None:
            published_at = beijing_now().replace(microsecond=0)
            st.session_state["notice_reference"] = published_at
        review.prepare(raw, published_at, provider, mode=mode)
        st.session_state["notice_confirm"] = False
        st.session_state.pop("notice_receipt", None)
    st.button("清空本地输入", key="notice_clear", on_click=_clear_local_input)

    preview = review.preview
    if preview is not None:
        kinds = sorted({finding.kind for finding in preview.batch.findings})
        if kinds:
            st.warning("本地检测到：" + "、".join(kinds) + "。风险提示不回显匹配到的内容。")
        for error in preview.batch.errors:
            st.error(error)
        for warning in preview.batch.warnings:
            st.warning(warning)
        st.caption(f"已排除 {preview.batch.ignored_lines} 个非必要片段或行、{preview.batch.skipped_notices} 条无可发送内容的通知。")
        if mode == "natural" and preview.batch.notices:
            st.write("本地筛选后的完整通知片段：" if preview.request_json else "本地待修改草稿（存在风险，不能发送）：")
            for notice in preview.batch.notices:
                st.caption(f"通知 {notice.source_index} · 解析时间基准（北京）：{format_deadline(notice.published_at)}")
                st.code(notice.sanitized_text, language="text")
            if preview.request_json is None:
                st.info("请在上方输入框修改后重新扫描，或切换到结构化输入，仅填写必要任务信息。")
        if preview.request_json is not None and mode == "structured":
            st.write("提取后供审核的任务字段：")
            st.dataframe([
                {"通知": notice.source_index, "标题": notice.title, "课程/活动": notice.category,
                 "类型": notice.task_type, "截止原文（尚未解析）": notice.deadline_text or "待确认",
                 "解析时间基准（北京）": format_deadline(notice.published_at)}
                for notice in preview.batch.notices
            ], hide_index=True, use_container_width=True)
        if preview.request_json is not None:
            st.write("实际准备提交的完整 JSON 请求（当前仅交给本地模拟器）：")
            st.code(preview.request_json, language="json")
            st.caption("修改通知、输入模式、发布时间或提供方会撤销预览与确认。请检查所有待发送内容是否夹带私人信息；原始通知不会作为兜底内容加入请求。")

    ready = preview is not None and preview.request_json is not None and publication_complete and not review.sent
    confirmed = st.checkbox("我已逐项审核上方完整请求，仅同意本次模拟发送", key="notice_confirm", disabled=not ready)
    if st.button("确认后发送到本地模拟 API", key="notice_send", disabled=not (ready and confirmed)):
        try:
            receipt = review.send(raw, published_at, provider, confirmed=confirmed, mode=mode)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state["notice_receipt"] = receipt
            st.session_state["notice_clear_confirm"] = True
            st.rerun()
    if "notice_receipt" in st.session_state:
        receipt = st.session_state["notice_receipt"]
        st.success(f"本地模拟器已接收 {receipt.notice_count} 条任务信息（{receipt.request_id}）。未访问外部服务、未创建待办；本次确认已使用。")
