"""Stage 3 UI: upload approval and reviewed database save are separate actions."""

import os
import json
import sqlite3
from dataclasses import replace
from datetime import datetime

import streamlit as st

from pku_deadline_agent.ai_config import PROVIDERS, load_settings
from pku_deadline_agent.config import database_path
from pku_deadline_agent.database import TaskConflict, TaskRepository
from pku_deadline_agent.models import TASK_TYPES
from pku_deadline_agent.notices import INPUT_MODES, MAX_INPUT_LENGTH
from pku_deadline_agent.parse_service import ParseSession
from pku_deadline_agent.time_utils import BEIJING, beijing_now, format_deadline
from pku_deadline_agent.usage import UsageLedger
from pku_deadline_agent.management_page import render_import_choice
from pku_deadline_agent.task_management import origin_for_notice


def invalidate_parse():
    session = st.session_state.get("parse_session")
    if session is not None:
        session.invalidate()
    for key in ("parse_confirm", "parse_real_confirm", "parse_save_confirm"):
        st.session_state[key] = False
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
    st.caption("默认本地模拟 · 规则优先 · 上传前确认 · 核对重复与改期后保存")
    st.warning("只粘贴必要通知；姓名等本地规则可能遗漏，请检查完整脱敏预览。真实调用会向所选提供方上传预览内容并可能扣费。")
    if not isinstance(st.session_state.get("parse_session"), ParseSession):
        st.session_state["parse_session"] = ParseSession()
    session = st.session_state["parse_session"]
    if st.session_state.pop("parse_reset_approval", False):
        st.session_state["parse_confirm"] = False
        st.session_state["parse_real_confirm"] = False
    if st.session_state.pop("parse_reset_save", False):
        st.session_state["parse_save_confirm"] = False
    execution = st.radio("解析方式", ("本地模拟（不消耗额度）", "真实 API（需单独授权）"), key="parse_execution", on_change=invalidate_parse, horizontal=True)
    live = execution.startswith("真实")
    provider = st.selectbox("模型提供方", tuple(PROVIDERS), key="parse_provider", on_change=invalidate_parse)
    model_options = list(PROVIDERS[provider][1])
    if live:
        prefix = "PKU_DEEPSEEK" if provider == "DeepSeek" else "PKU_MIMO"
        configured = os.environ.get(prefix + "_MODEL")
        if configured:
            model_options = [configured, *[name for name in model_options if name != configured]]
    # Separate keys keep a provider switch from selecting a model from another vendor.
    model = st.selectbox("模型名称", model_options, key=f"parse_model_{provider}_{live}", on_change=invalidate_parse)
    try:
        settings = load_settings(provider, live=live, model=model)
    except ValueError as exc:
        invalidate_parse()
        st.error(str(exc))
        st.info("密钥只通过环境变量配置，不要粘贴到通知或聊天。安全配置和首次测试步骤见 README；开发交付未进行真实调用。")
        return
    if live:
        st.write(f"上传目标：{settings.base_url}/chat/completions · 模型：{settings.model}")
        st.caption(f"保守计费单价（人民币 / 百万 tokens）：输入 {settings.input_price}，输出 {settings.output_price}；每日上限 ¥{settings.daily_limit}；输出最多 {settings.max_output_tokens} tokens；超时 {settings.timeout} 秒。请先与官方当前单价核对。")
    else:
        st.info("仅本地 Python 和模拟响应，不读取真实密钥、不连接外部服务。模拟结果不是模型能力验证；没有识别出的任务或复杂语义需手动补充。")
    mode_label = st.radio("输入模式", tuple(INPUT_MODES.values()), key="parse_mode", on_change=invalidate_parse, horizontal=True)
    mode = next(key for key, label in INPUT_MODES.items() if label == mode_label)
    raw = st.text_area("待解析通知（仅本地）", key="parse_raw", max_chars=MAX_INPUT_LENGTH, height=180, on_change=invalidate_parse, placeholder="《虚构课程》第三次作业延期至下周三晚20:00，请在教学平台提交。")
    st.caption("多条通知用独立一行 --- 分隔。未注明发布时间时默认按解析时的北京时间；未写年份默认参考年份（通常为今年），周五默认本周五。下周从下一个周一算起。")
    with st.expander("历史通知：可选修改时间基准"):
        st.caption("日常通知无需填写。原文可写“发布时间: 2026-10-11 09:00”，每条通知的明确发布时间优先。")
        left, right = st.columns(2)
        date = left.date_input("通知发布日期（北京，选填）", value=None, key="parse_date", on_change=invalidate_parse)
        time = right.time_input("通知发布时刻（北京，选填）", value=None, key="parse_time", on_change=invalidate_parse)
    complete = (date is None) == (time is None)
    if "parse_reference" not in st.session_state:
        st.session_state["parse_reference"] = beijing_now().replace(microsecond=0)
    published = datetime.combine(date, time, tzinfo=BEIJING) if date is not None and time is not None else st.session_state["parse_reference"]
    if not complete:
        st.error("发布日期和时刻必须一起填写或一起留空。")
    if session.observe(raw, published, mode, settings):
        for key in ("parse_confirm", "parse_real_confirm", "parse_save_confirm"):
            st.session_state[key] = False
        st.session_state.pop("parse_saved", None)
    ledger = UsageLedger(database_path().parent / "ai_usage.sqlite3")
    try:
        summary = ledger.summary()
        st.caption(f"北京时间今日真实调用：{summary['calls']} 次；输入 {summary['prompt_tokens']} / 输出 {summary['completion_tokens']} tokens；保守费用与未决预留合计 ¥{summary['charged_micros'] / 1_000_000:.6f}；未决请求 {summary['uncertain'] or 0} 个。模拟免费。")
    except (OSError, sqlite3.Error):
        st.error("无法打开本地用量账本，已阻断解析。手动待办仍可使用。")
        return
    if st.button("本地检查并准备解析", key="parse_scan", disabled=not complete, type="primary"):
        if date is None and time is None:
            # Freeze one reference for preview, execution and save, including midnight.
            published = beijing_now().replace(microsecond=0)
            st.session_state["parse_reference"] = published
        try:
            session.prepare(raw, published, mode, settings)
            st.session_state["parse_generation"] = st.session_state.get("parse_generation", 0) + 1
        except ValueError as exc:
            st.error(str(exc))
        for key in ("parse_confirm", "parse_real_confirm", "parse_save_confirm"):
            st.session_state[key] = False
    st.button("清空解析输入和预览", key="parse_clear", on_click=_clear)
    plan = session.plan
    if plan:
        for error in plan.batch.errors:
            st.error(error)
        for warning in plan.batch.warnings:
            st.warning(warning)
        if plan.batch.findings:
            st.warning("本地检测类别：" + "、".join(sorted({item.kind for item in plan.batch.findings})))
        st.write("完整本地筛选内容（风险草稿不能上传）：")
        for notice in plan.batch.notices:
            st.caption(f"通知 {notice.source_index} · 解析时间基准（北京） {format_deadline(notice.published_at)}")
            st.code(notice.sanitized_text if mode == "natural" else str(notice.to_payload()), language="text")
        if plan.local_tasks:
            st.write(f"{len(plan.local_tasks)} 项可由本地规则处理，不上传这部分内容。")
        if plan.request_json:
            st.write("准备提交的完整 JSON 请求（以下内容与实际请求一致）：")
            st.code(plan.request_json, language="json")
            if plan.route == "live":
                st.warning(f"本次保守预算预留 ¥{plan.reserve_micros / 1_000_000:.6f}；先预留后调用。超时或缺失用量时保留预留，无自动重试。费用为按配置单价计算的估算，以提供方账单为准。")
        elif plan.route == "local":
            st.success("本批任务可由本地规则处理，不明确时间保持待确认；API 调用费用为 0。")
    ready = bool(plan and plan.route != "blocked" and complete and not session.consumed)
    confirmed = st.checkbox("我已检查完整脱敏内容和请求，同意本次解析", key="parse_confirm", disabled=not ready)
    real_confirmed = False
    if plan and plan.route == "live":
        real_confirmed = st.checkbox("我同意本次（含首次）向上述官方提供方上传预览内容，已核对人民币单价，并授权使用不超过本次预留的额度", key="parse_real_confirm", disabled=not ready)
    if st.button("确认后执行解析", key="parse_run", disabled=not (ready and confirmed and (not plan or plan.route != "live" or real_confirmed))):
        try:
            session.run(raw, published, mode, settings, ledger, confirmed=confirmed, real_confirmed=real_confirmed)
        except (ValueError, OSError, sqlite3.Error) as exc:
            st.error(str(exc) if isinstance(exc, ValueError) else "本地账本或存储不可用，未保存任务。")
        st.session_state["parse_reset_approval"] = True
        # Rerun on success; errors remain visible and single-use approval is consumed.
        if session.results is not None:
            st.rerun()
    if session.results is not None:
        st.subheader("解析结果：核对后保存")
        st.caption("日期不完整或存在歧义时截止时间留空，可在这里修订任务。重复或改期须选择处理方式并最终确认，才新增、关联或更新已有任务。")
        st.code(json.dumps({"tasks": [task.to_dict() for task in session.results]}, ensure_ascii=False, indent=2), language="json")
        missing = {notice.source_index for notice in plan.batch.notices} - {task.source_index for task in session.results}
        if missing:
            st.warning("以下通知没有提取结果，请核对是否漏掉任务：" + "、".join(map(str, sorted(missing))))
        selected, decisions = [], []
        invalid_selected = False
        repository = TaskRepository(database_path())
        try:
            repository.initialize()
        except (sqlite3.Error, OSError, RuntimeError):
            st.error("本地任务数据库暂不可用，解析结果保留在当前会话，未保存。请检查权限、空间及数据库版本。")
            return
        sources = {notice.source_index: notice for notice in plan.batch.notices}
        generation = st.session_state.get("parse_generation", 0)
        for index, task in enumerate(session.results):
            prefix = f"candidate_{generation}_{index}"
            with st.expander(f"任务 {index + 1}：{task.title}", expanded=True):
                st.caption(f"通知 {task.source_index} · 截止变更：{'是' if task.deadline_changed else '否'} · 截止依据：{task.deadline_text or '待确认'}")
                for note in task.review_notes:
                    st.warning(note)
                include = st.checkbox("保存此任务", value=True, key=prefix + "_include", on_change=_invalidate_save)
                title = st.text_input("任务名称", value=task.title, key=prefix + "_title", max_chars=200, on_change=_invalidate_save)
                category = st.text_input("课程或活动", value=task.category, key=prefix + "_category", max_chars=100, on_change=_invalidate_save)
                kind = st.selectbox("任务类型", TASK_TYPES, index=TASK_TYPES.index(task.task_type), key=prefix + "_type", on_change=_invalidate_save)
                explicit = st.checkbox("我已核实完整截止时间", value=task.due_at is not None, key=prefix + "_explicit", on_change=_invalidate_save)
                due_date = st.date_input("截止日期（北京）", value=task.due_at.date() if task.due_at else None, key=prefix + "_date", disabled=not explicit, on_change=_invalidate_save)
                due_time = st.time_input("截止时刻（北京）", value=task.due_at.timetz().replace(tzinfo=None) if task.due_at else None, key=prefix + "_time", disabled=not explicit, on_change=_invalidate_save)
                if explicit and (due_date is None or due_time is None):
                    st.error("必须补全日期和时刻，或取消勾选以保存为待确认。")
                    invalid_selected = invalid_selected or include
                    include = False
                due = datetime.combine(due_date, due_time, tzinfo=BEIJING) if explicit and due_date and due_time else None
                if include:
                    candidate = replace(task, title=title, category=category, task_type=kind, due_at=due)
                    decision = render_import_choice(repository, candidate, origin_for_notice(sources[task.source_index]), prefix + "_import", _invalidate_save)
                    if decision is None:
                        invalid_selected = True
                    else:
                        selected.append(candidate)
                        decisions.append(decision)
        decision_digest = repr((selected, decisions, [(item.due_at, item.deadline_text) for item in selected]))
        if st.session_state.get("parse_decision_digest") != decision_digest:
            st.session_state["parse_decision_digest"] = decision_digest
            st.session_state["parse_save_confirm"] = False
        saved = st.checkbox("我已核对以上最终任务、截止时间及变更信息，确认保存所选任务", key="parse_save_confirm", disabled=session.saved)
        if st.button("确认保存到 SQLite", key="parse_save", disabled=not (saved and selected and not invalid_selected and not session.saved)):
            repository = TaskRepository(database_path())
            try:
                repository.initialize()
                added = session.save(repository, raw, published, mode, settings, tuple(selected), confirmed=saved, decisions=tuple(decisions))
            except TaskConflict as exc:
                st.warning(str(exc))
                st.write("请核对已有任务编号：" + "、".join(map(str, exc.task_ids)))
            except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
                st.error(str(exc) if isinstance(exc, ValueError) else "本地数据库保存失败，请检查权限和版本。")
            else:
                st.session_state["parse_saved"] = len(added)
                st.session_state["parse_reset_save"] = True
                st.rerun()
    if "parse_saved" in st.session_state:
        st.success(f"已审核并保存 {st.session_state['parse_saved']} 项处理结果，仅按确认方案新增、关联或更新。可到“手动待办”查看。")


def _invalidate_save():
    st.session_state["parse_save_confirm"] = False
