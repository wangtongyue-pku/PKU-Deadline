"""Run with: python -m streamlit run app.py"""

import sqlite3
from datetime import datetime

import streamlit as st

from pku_deadline_agent.config import database_path
from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.email_page import render_email_page
from pku_deadline_agent.grid_page import render_grid_import_page
from pku_deadline_agent.models import TASK_TYPES
from pku_deadline_agent.notice_page import render_notice_page
from pku_deadline_agent.parse_page import invalidate_parse, render_parse_page
from pku_deadline_agent.time_utils import BEIJING, beijing_now, format_deadline
from pku_deadline_agent.extraction import ExtractedTask
from pku_deadline_agent.management_page import render_import_choice, render_task_board, render_reminder_settings
from pku_deadline_agent.task_management import ImportDecision, NoticeOrigin


def main() -> None:
    st.set_page_config(page_title="PKU Deadline", page_icon="📅", layout="wide")
    st.title("PKU Deadline")
    st.caption("所有日期均为北京时间 Asia/Shanghai")

    with st.sidebar:
        page = st.radio("功能", ("手动待办", "通知隐私预览", "Deadline 智能解析", "编程网格导入",
                                 "学校邮箱", "AI 设置", "提醒与备份"), key="page")
        st.divider()
        with st.expander("这个程序做什么", expanded=False):
            st.write(
                "本地优先的 Deadline 助手。数据只存在这台电脑的 SQLite 里，"
                "邮箱只读同步；AI 提取需查看脱敏预览后点击发送。"
            )
            st.write("**可用**：手动添加与编辑、完成状态、回收站、重复核对、截止变更历史、"
                     "独立本地提醒、微信通知导入、编程网格「一键同步」、"
                     "学校邮箱只读分类与 Deadline 导入。")
            st.write("**不在范围内**：教学网与 pku3b。学校邮箱支持每天自动检查；编程网格定时同步尚未实现。")
        with st.expander("安全边界", expanded=False):
            st.write("程序不读取你的密码：编程网格的登录在它自己打开的浏览器窗口里由你完成。")
            st.write("学校邮箱只读读取 INBOX；授权码由 Windows 凭据管理器保存，可随时忘记。后台检查不上传 AI。")
            st.write("API Key 可在「AI 设置」页保存到 **Windows 凭据管理器**，"
                     "页面只显示是否已配置，不回显 Key。")
            st.write("不要把密码、Cookie、Token 或私人聊天原文填进任务里。")
            st.write("本程序没有登录认证，只监听本机 127.0.0.1，不要暴露到公网。")

    if page != "Deadline 智能解析" and "parse_session" in st.session_state:
        invalidate_parse()
    if page != "编程网格导入" and "grid_plan" in st.session_state:
        # Leaving the page drops the previewed payload; nothing was written yet.
        for key in ("grid_payload", "grid_plan", "grid_digest"):
            st.session_state.pop(key, None)
        st.session_state["grid_confirm"] = False
    if page != "学校邮箱":
        mail_parse = st.session_state.pop("email_ai_session", None)
        if mail_parse is not None:
            mail_parse.invalidate()
        st.session_state.pop("email_ai_input", None)
    if page == "Deadline 智能解析":
        if "notice_review" in st.session_state:
            st.session_state["notice_review"].preview = None
            st.session_state["notice_confirm"] = False
            st.session_state.pop("notice_receipt", None)
        render_parse_page()
        return
    if page == "通知隐私预览":
        render_notice_page()
        return
    if page == "编程网格导入":
        repository = TaskRepository(database_path())
        try:
            repository.initialize()
        except (sqlite3.Error, OSError, RuntimeError):
            st.error("无法打开数据库：请检查本地文件权限、可用空间和程序版本。")
            st.stop()
        try:
            render_grid_import_page(repository)
        except (sqlite3.Error, OSError, RuntimeError):
            st.error("读取或写入本地数据库失败，请检查权限和可用空间。")
        return
    if page == "学校邮箱":
        repository = TaskRepository(database_path())
        try:
            repository.initialize()
        except (sqlite3.Error, OSError, RuntimeError):
            st.error("无法打开数据库：请检查本地文件权限、可用空间和程序版本。")
            st.stop()
        # Leaving the page drops the preview: nothing was written yet, and a stale
        # plan must not be applied after the mail it came from is out of sight.
        if st.session_state.get("_email_rendered") != page:
            for key in ("email_plan", "email_rendered_plan", "email_generation"):
                st.session_state.pop(key, None)
        st.session_state["_email_rendered"] = page
        try:
            render_email_page(repository)
        except (sqlite3.Error, OSError, RuntimeError):
            st.error("读取或写入本地数据库失败，请检查权限和可用空间。")
        return
    if page == "AI 设置":
        # No repository: this page manages a credential, not task data.
        from pku_deadline_agent.ai_settings_page import render_ai_settings_page

        render_ai_settings_page()
        return
    if "_email_rendered" in st.session_state:
        st.session_state.pop("_email_rendered", None)
        for key in ("email_plan", "email_rendered_plan"):
            st.session_state.pop(key, None)
    # Leaving review mode invalidates its preview/confirmation, retaining manual UI.
    if "notice_review" in st.session_state:
        st.session_state["notice_review"].preview = None
        st.session_state["notice_review"].sent = False
        st.session_state["notice_confirm"] = False
        st.session_state.pop("notice_receipt", None)
    repository = TaskRepository(database_path())
    try:
        repository.initialize()
    except (sqlite3.Error, OSError, RuntimeError):
        st.error("无法打开数据库：请检查本地文件权限、可用空间和程序版本。")
        st.stop()

    if page == "提醒与备份":
        try:
            render_reminder_settings(repository)
        except (sqlite3.Error, OSError, RuntimeError):
            st.error("本地提醒设置或备份不可用，请检查数据库权限和可用空间。")
        return

    # Add-task first, then the board: this keeps the widget order the manual-entry
    # tests rely on, while the board is the part that is actually visible.
    saved = st.session_state.pop("manual_saved", None)
    if saved:
        st.success(f"已保存到本地：「{saved}」。")
    with st.expander("添加任务", expanded=False):
        _render_add_task(repository)

    # The list is the reason to open this page, so it comes before the controls.
    try:
        render_task_board(repository)
    except (sqlite3.Error, OSError):
        st.error("读取失败，请检查本地数据库。")
        st.stop()

    pending = st.session_state.get("manual_pending")
    if pending:
        st.divider()
        st.subheader("核对手动录入的重复或改期任务")
        st.write(f"候选：{pending.title} · {pending.category or '未分类'} · {format_deadline(pending.due_at)}")
        choice = render_import_choice(repository, pending, NoticeOrigin("manual"), f"manual_{st.session_state['manual_generation']}", _invalidate_manual)
        digest = repr(choice)
        if st.session_state.get("manual_choice_digest") != digest:
            st.session_state["manual_choice_digest"] = digest
            st.session_state["manual_import_confirm"] = False
        confirmed = st.checkbox("我已核对手动任务的最终处理方案", key="manual_import_confirm")
        if st.button("确认处理手动任务", disabled=not (confirmed and choice), key="manual_apply"):
            try:
                repository.apply_reviewed_tasks((pending,), (choice,), (NoticeOrigin("manual"),), confirmed=True)
            except (ValueError, sqlite3.Error, OSError) as exc:
                st.error(str(exc) if isinstance(exc, ValueError) else "处理失败，请检查本地数据库。")
            else:
                st.session_state.pop("manual_pending", None)
                st.rerun()
        if st.button("取消本次手动候选", key="manual_cancel"):
            st.session_state.pop("manual_pending", None)
            st.rerun()


def _render_add_task(repository) -> None:
    """The manual entry form, folded away so it does not bury the task list."""
    st.caption("只有日期、没有明确时刻时，先保留为“待确认”，不会自动推断为当天 23:59。"
               "任务只保存到本地，不调用 AI；请不要填写密码、Cookie、Token 或聊天原文。")
    # Kept outside the form so ticking it immediately enables the date fields.
    has_deadline = st.checkbox("截止时间已明确", value=False)
    with st.form("add_task", clear_on_submit=True):
        title = st.text_input("任务标题", max_chars=200, placeholder="例如：完成计算概论第 3 次练习")
        category = st.text_input("所属课程或活动（选填）", max_chars=100)
        task_type = st.selectbox("任务类型", TASK_TYPES)
        date_column, time_column = st.columns(2)
        due_date = date_column.date_input("截止日期（北京时间）", value=beijing_now().date(), disabled=not has_deadline)
        due_time = time_column.time_input("截止时刻（北京时间，需明确填写）", value=None, disabled=not has_deadline)
        submitted = st.form_submit_button("保存到本地", type="primary")
    if submitted:
        if has_deadline and due_time is None:
            st.error("请填写明确的截止时刻，或取消勾选以保存为待确认。")
            st.stop()
        due_at = datetime.combine(due_date, due_time, tzinfo=BEIJING) if has_deadline else None
        try:
            candidate = ExtractedTask(1, title.strip(), category.strip(), task_type, None, due_at, False, ())
            repository._validate_fields(title, category, task_type, due_at)
            if repository.suggestions(candidate, NoticeOrigin("manual")):
                st.session_state["manual_pending"] = candidate
                st.session_state["manual_import_confirm"] = False
                st.session_state["manual_generation"] = st.session_state.get("manual_generation", 0) + 1
            else:
                repository.apply_reviewed_tasks((candidate,), (ImportDecision("new", reason="用户手动添加任务"),), (NoticeOrigin("manual"),), confirmed=True)
                st.session_state.pop("manual_pending", None)
                # The list is rendered above this form, so without a rerun the new
                # task would only appear after the next interaction.
                st.session_state["manual_saved"] = title.strip()
                st.rerun()
        except ValueError as exc:
            st.error(str(exc))
        except (sqlite3.Error, OSError):
            st.error("保存失败，请检查数据库权限或可用空间。")


def _invalidate_manual():
    st.session_state["manual_import_confirm"] = False


if __name__ == "__main__":
    main()
