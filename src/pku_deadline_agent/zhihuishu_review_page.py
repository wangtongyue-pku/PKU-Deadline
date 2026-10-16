"""Development preview UI. Every button is local/read-only or simulation-only."""

import sqlite3
from dataclasses import asdict

import streamlit as st

from pku_deadline_agent.config import database_path
from pku_deadline_agent.database import ReadOnlyTaskRepository
from pku_deadline_agent.task_management import ImportDecision, identity
from pku_deadline_agent.time_utils import format_deadline
from pku_deadline_agent.zhihuishu_review import CATEGORIES, build_review, fingerprint, simulate_choices
from pku_deadline_agent.zhihuishu_samples import sample_review_data

PREFIX = 'zhs_review_'
ACTIONS = {'暂不处理': 'skip', '拟新增独立任务': 'new', '拟关联已有任务': 'link', '拟修改已有截止': 'update'}
REASONS = {'boundary_unverified': '列表边界尚未验证', 'displayed_total_unknown': '页面总数未知',
           'fields_incomplete': '部分任务字段缺失', 'task_limit': '达到累计任务上限',
           'scroll_limit': '达到滚动上限', 'pagination_not_traversed': '分页尚未读取'}


def invalidate_review():
    for key in list(st.session_state):
        if key.startswith(PREFIX):
            del st.session_state[key]


def render_zhihuishu_preview_page(path=None, *, report=None):
    st.subheader('智慧树任务预览')
    st.info('开发预览：智慧树列表使用虚构数据。所有选择和确认仅模拟，不保存任务，不连接智慧树或 AI。')
    sample, example_tasks = sample_review_data()
    report = report if report is not None else sample
    if not report.evidence.startswith('synthetic'):
        invalidate_review()
        st.error('本轮界面只接受虚构列表，不接收真实浏览器数据。')
        return
    reference = st.radio('对比对象', ('本地任务（只读）', '虚构待办（体验匹配）'), key='zhs_reference', horizontal=True)
    try:
        tasks = (ReadOnlyTaskRepository(path if path is not None else database_path()).list_tasks(include_completed=True)
                 if reference == '本地任务（只读）' else example_tasks)
        snapshot = build_review(report, tasks)
    except (sqlite3.Error, OSError, RuntimeError, ValueError):
        invalidate_review()
        st.error('无法只读读取或核验任务库；未将失败当成空库，也不会创建或迁移数据库。可切换虚构待办体验。')
        return
    digest = fingerprint((snapshot.digest, reference))
    if st.session_state.get(PREFIX+'digest') != digest:
        invalidate_review()
        st.session_state[PREFIX+'digest'] = digest
    st.caption('本地任务包含已完成项，排除回收站；平台状态不会改动本地完成状态。当前对比只使用任务字段，不读取通知正文。')
    coverage = report.coverage
    if coverage.complete:
        st.success('该虚构列表的范围和字段已验证完整；仍然只供模拟。')
    else:
        st.warning('列表读取不完整。未出现的任务仍保持原样，不能推断已完成、已删除或已取消。')
    st.write(f'页面总数：{coverage.displayed_total if coverage.displayed_total is not None else "未知"} · '
             f'已加载峰值 / 最后：{coverage.loaded_peak} / {coverage.loaded_final} · '
             f'累计触及 / 读出：{coverage.observed_count} / {coverage.read_count} · '
             f'最少未读：{coverage.unread_minimum}（0 不代表没有未加载任务）')
    with st.expander('查看读取范围与限制'):
        st.write('范围完整：', coverage.inventory_complete, '字段完整：', coverage.fields_complete)
        for reason in coverage.reasons:
            st.text(REASONS.get(reason, reason))
        st.caption('无稳定来源 ID；临时编号只区分本次候选。同名任务不自动合并。')
    for col, category in zip(st.columns(4), CATEGORIES):
        col.metric(category, sum(i.category == category for i in snapshot.items))
    st.dataframe([{'编号': i.index, '分类': i.category, '课程': i.task.course or '待确认',
                   '任务标题': i.task.title or '缺失', '截止时间（北京时间）': format_deadline(i.task.deadline),
                   '平台状态': i.task.status or '未知', '来源': '智慧树（虚构列表）'}
                  for i in snapshot.items], hide_index=True, use_container_width=True)

    left, right = st.columns(2)
    if left.button('勾选全部候选', key=PREFIX+'all'):
        for item in snapshot.items: st.session_state[f'{PREFIX}pick_{item.index}'] = True
    if right.button('取消全部选择', key=PREFIX+'none'):
        for item in snapshot.items: st.session_state[f'{PREFIX}pick_{item.index}'] = False
    choices = {}
    for item in snapshot.items:
        key = f'{PREFIX}{item.index}_'
        selected = st.checkbox(f'选择候选 {item.index} · {item.category}', key=f'{PREFIX}pick_{item.index}')
        with st.expander(f'候选 {item.index}：详情与人工匹配', expanded=selected):
            st.text(item.task.title or '标题缺失')
            st.text(f'课程：{item.task.course or "待确认"}；截止：{format_deadline(item.task.deadline)}；状态：{item.task.status or "未知"}')
            st.text(f'原始日期文字：{item.task.deadline_date or "未提供"}；来源链接：{item.task.source_url or "未提供"}')
            for warning in item.warnings: st.warning(warning)
            if item.batch_peers:
                st.warning('同批还有同名候选：' + '、'.join(map(str, item.batch_peers)) + '；不得自动合并。')
            if item.matches:
                st.dataframe([{'本地编号': m.task.id, '标题': m.task.title, '课程': m.task.category,
                               '原截止': format_deadline(m.task.due_at), '本地状态': '已完成' if m.task.completed else '未完成',
                               '来源': m.task.source, '建议原因': m.reason} for m in item.matches], hide_index=True)
            elif not item.warnings:
                st.caption('未找到匹配建议，仍需你决定是否为独立新增。')
            action_label = st.selectbox('本次拟处理方式', ('暂不处理',) if item.warnings else tuple(ACTIONS),
                                        key=key+'action', disabled=not selected)
            action, target, reason, distinct = ACTIONS[action_label], None, '', False
            if action == 'new' and (item.matches or item.batch_peers):
                distinct = st.checkbox('已核对相似项，仍拟作为独立任务', key=key+'distinct', disabled=not selected)
            if action in ('link', 'update'):
                eligible = {t.id: t for t in tasks if identity(t.category) == identity(item.task.course or '') and t.task_type == '作业'}
                target_id = st.selectbox('人工选择本地目标（不默认选中）', [None, *eligible],
                    format_func=lambda v: '尚未选择' if v is None else f'#{v} {eligible[v].title} · {format_deadline(eligible[v].due_at)}',
                    key=key+'target', disabled=not selected)
                target = eligible.get(target_id)
                if target:
                    st.text(f'原截止：{format_deadline(target.due_at)} → 列表截止：{format_deadline(item.task.deadline)}')
                    st.text(f'本地状态保持：{"已完成" if target.completed else "未完成"}；版本：{target.revision}')
                    if action == 'link': st.caption('拟关联仅保留现有字段；即使截止不同也不改期。')
                if action == 'update': reason = st.text_input('拟修改原因（必填）', key=key+'reason', disabled=not selected, max_chars=500)
            if selected:
                choices[item.index] = ImportDecision(action, target.id if target else None,
                    target.revision if target else None, reason, distinct)
    st.subheader('拟执行操作（仅预览）')
    try:
        plan = simulate_choices(snapshot, choices)
        error = None
    except ValueError as exc:
        plan, error = None, str(exc)
    decision_digest = fingerprint((digest, {i: asdict(c) for i, c in choices.items()}))
    if st.session_state.get(PREFIX+'confirmed') != decision_digest:
        st.session_state.pop(PREFIX+'confirmed', None)
    if error: st.warning(error)
    if plan and plan.operations:
        st.dataframe([{'候选': op.index, '操作': {'new': '拟新增', 'link': '拟关联（保留原字段）', 'update': '拟改期'}[op.action],
                       '任务': op.incoming.title, '目标编号': str(op.target.id) if op.target else '独立新增',
                       '原截止': format_deadline(op.target.due_at) if op.target else '无',
                       '拟截止': format_deadline(op.target.due_at if op.action == 'link' else op.incoming.deadline),
                       '原因': op.reason or '人工预览选择'} for op in plan.operations], hide_index=True)
    else: st.caption('勾选候选并明确选择处理方式后，这里会列出模拟操作。')
    st.caption('确认只检查当前方案，不代表已入库；修改选择、匹配、原因、数据或离开页面都会撤销模拟确认。')
    if st.button('确认模拟方案（不会保存）', key=PREFIX+'confirm', disabled=not (plan and plan.operations)):
        st.session_state[PREFIX+'confirmed'] = decision_digest
    if st.session_state.get(PREFIX+'confirmed') == decision_digest:
        st.success('模拟方案已核对。没有写入 SQLite、修改任务或调用 AI。')
