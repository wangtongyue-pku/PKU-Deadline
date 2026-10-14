"""Compact preview/results shared by mail and notices; approvals are button clicks."""

import json
import os
from dataclasses import replace
from datetime import datetime

import streamlit as st

from pku_deadline_agent.ai_config import PROVIDERS, load_settings
from pku_deadline_agent.management_page import render_import_choice
from pku_deadline_agent.models import TASK_TYPES
from pku_deadline_agent.task_management import ImportDecision, origin_for_notice
from pku_deadline_agent.time_utils import BEIJING, format_deadline
from pku_deadline_agent.usage import UsageLedger


def render_settings(scope="parse"):
    with st.expander("模型与费用设置", expanded=False):
        provider = st.selectbox("模型提供方", tuple(PROVIDERS), key=scope + "_provider")
        prefix = "PKU_DEEPSEEK" if provider == "DeepSeek" else "PKU_MIMO"
        options = list(PROVIDERS[provider][1])
        configured = os.environ.get(prefix + "_MODEL")
        if configured:
            options = [configured, *[name for name in options if name != configured]]
        model = st.selectbox("模型名称", options, key=f"{scope}_model_{provider}_True")
        try:
            settings = load_settings(provider, live=True, model=model)
        except ValueError as error:
            st.info(str(error))
            st.caption("请到「AI 设置」配置 Key。仍可本地解析明确通知；需要模型时才要求 Key。")
            return None, provider, model
        st.caption(f"{settings.base_url} · 每日上限 ¥{settings.daily_limit} · 超时 {settings.timeout} 秒")
        st.caption(f"人民币 / 百万 tokens：输入 {settings.input_price}，输出 {settings.output_price}；估算不等于账单。")
        return settings, provider, model


def local_settings(provider, model):
    # Only route=local may run without real configuration; no fake fallback.
    from pku_deadline_agent.ai_config import AISettings
    return AISettings(provider, model)


def render_preview(session, settings, raw, published, mode, task_path, *, scope="parse", live_available=True):
    error = st.session_state.pop(scope + "_call_error", None)
    if error:
        st.error(error)
    plan = session.plan
    if plan is None:
        return
    for error in plan.batch.errors:
        st.error(error)
    for warning in plan.batch.warnings:
        st.warning(warning)
    if plan.batch.findings:
        st.caption("已在本地检查：" + "、".join(sorted({item.kind for item in plan.batch.findings})))
    if plan.route == "blocked":
        st.error("存在未消除的隐私风险，请修改内容；不能强制发送。")
    elif plan.route == "local":
        st.caption("明确内容已用本地规则提取，无需上传、费用为 0。")
    else:
        st.markdown("**将发送的脱敏内容**")
        for notice in plan.ai_notices:
            st.caption(f"通知 {notice.source_index} · 北京时间基准 {format_deadline(notice.published_at)}")
            st.code(notice.sanitized_text if mode == "natural" else json.dumps(notice.to_payload(), ensure_ascii=False), language="text")
        with st.expander("完整请求 JSON", expanded=False):
            st.code(plan.request_json, language="json")
        st.caption(f"发送给 {settings.provider} / {settings.model}。本次最多预留 ¥{plan.reserve_micros / 1_000_000:.6f}；超时不自动重试。")
        if not live_available:
            st.info("请先配置真实 API Key 并启用真实调用，然后重新准备；不会使用模拟响应代替。")
    ready = plan.route != "blocked" and not session.consumed and (live_available or plan.route == "local")
    label = "同意发送上述内容并解析" if plan.route != "local" else "提取本地结果"
    if st.button(label, key=scope + "_run", disabled=not ready, type="primary"):
        try:
            session.run(raw, published, mode, settings, UsageLedger(task_path.parent / "ai_usage.sqlite3"), confirmed=True, real_confirmed=True)
        except (ValueError, OSError) as error:
            st.session_state[scope + "_call_error"] = str(error) if isinstance(error, ValueError) else "本地账本不可用，未发送或保存任务。"
        st.rerun()


def render_results(session, repository, raw, published, mode, settings, *, scope="parse", origin_builder=None):
    if session.results is None:
        return
    st.markdown("### 提取结果")
    if session.saved:
        st.success("已审核并保存，可到「手动待办」查看。")
        return
    sources = {notice.source_index: notice for notice in session.plan.batch.notices}
    missing = set(sources) - {item.source_index for item in session.results}
    if missing:
        st.warning("以下通知没有任务结果，请核对是否漏项：" + "、".join(map(str, sorted(missing))))
    if not session.results:
        st.info("未提取到任务，可修改原文后重新解析。")
        return
    summary = st.empty()
    review_rows = []
    selected, decisions, origins = [], [], []
    invalid = False
    generation = st.session_state.get(scope + "_generation", 0)
    for index, base in enumerate(session.results):
        prefix = ("candidate" if scope == "parse" else scope + "_candidate") + f"_{generation}_{index}"
        with st.expander(f"调整或跳过：{base.title}", expanded=False):
            include = st.checkbox("保存此任务", value=True, key=prefix + "_include")
            title = st.text_input("任务名称", value=base.title, max_chars=200, key=prefix + "_title")
            course = st.text_input("课程或活动", value=base.category, max_chars=100, key=prefix + "_category")
            kind = st.selectbox("任务类型", TASK_TYPES, index=TASK_TYPES.index(base.task_type), key=prefix + "_type")
            explicit = st.checkbox("设置明确截止时间", value=base.due_at is not None, key=prefix + "_explicit")
            date = st.date_input("截止日期（北京）", value=base.due_at.date() if base.due_at else None, disabled=not explicit, key=prefix + "_date")
            time = st.time_input("截止时刻（北京）", value=base.due_at.time() if base.due_at else None, disabled=not explicit, key=prefix + "_time")
        if explicit and (date is None or time is None):
            st.error("请补全日期和时刻，或取消设置以保留待确认。")
            invalid = invalid or include
            continue
        candidate = replace(base, title=title, category=course, task_type=kind, due_at=datetime.combine(date, time, tzinfo=BEIJING) if explicit else None)
        review_rows.append({"保存": include, "任务": candidate.title, "课程 / 活动": candidate.category, "类型": candidate.task_type,
                            "截止（北京）": format_deadline(candidate.due_at), "需留意": "；".join(candidate.review_notes)})
        if not include:
            continue
        origin = origin_builder(candidate, index) if origin_builder else origin_for_notice(sources[base.source_index])
        matches = repository.suggestions(candidate, origin)
        if origin.source == "email" and origin.notice_hash not in repository.source_mappings("email"):
            # Same subject from distinct messages is distinct identity. The
            # batch save click approves new objects, never an automatic merge.
            decision = (render_import_choice(repository, candidate, origin, prefix + "_import") if matches else
                        ImportDecision("new", allow_distinct=True, reason="用户确认 AI 提取的邮件任务，以邮件来源标识独立新增"))
        elif len(matches) == 1 and matches[0].kind == "duplicate":
            task = matches[0].task
            st.caption(f"「{candidate.title}」与 #{task.id} 相同，保存时仅关联来源，不新增或覆盖。")
            decision = ImportDecision("link", task.id, task.revision, "用户批量保存时确认关联重复来源")
        else:
            decision = render_import_choice(repository, candidate, origin, prefix + "_import")
        if decision is None:
            invalid = True
        else:
            selected.append(candidate)
            decisions.append(decision)
            origins.append(origin)
    summary.dataframe(review_rows, hide_index=True, use_container_width=True)
    previous_targets = st.session_state.get(scope + "_shown_targets", {})
    targets = {decision.target_id: decision.expected_revision for decision in decisions if decision.target_id is not None}
    stale = any(target in previous_targets and previous_targets[target] != revision for target, revision in targets.items())
    st.session_state[scope + "_shown_targets"] = targets
    if st.button("保存所选任务", key=scope + "_save", disabled=not selected or invalid, type="primary"):
        if stale:
            st.error("目标任务在上次展示后已改变，请核对刷新后的内容，再点击保存；本次未写入。")
            return
        try:
            written = session.save(repository, raw, published, mode, settings, tuple(selected), confirmed=True, decisions=tuple(decisions), origins=tuple(origins))
        except (ValueError, OSError) as error:
            st.error(str(error) if isinstance(error, ValueError) else "本地保存失败，请检查数据库。")
        else:
            st.success(f"已审核并保存 {len(written)} 项处理结果，可到「手动待办」查看。")
