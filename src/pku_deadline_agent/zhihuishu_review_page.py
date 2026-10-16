"""Development preview UI. Every button is local/read-only or simulation-only."""

import sqlite3
from dataclasses import asdict
from pathlib import Path

import streamlit as st

from pku_deadline_agent.config import database_path
from pku_deadline_agent.database import ReadOnlyTaskRepository
from pku_deadline_agent.task_management import ImportDecision, identity
from pku_deadline_agent.time_utils import format_deadline
from pku_deadline_agent.zhihuishu_review import CATEGORIES, UNKNOWN_DEADLINE, build_review, fingerprint, simulate_choices
from pku_deadline_agent.zhihuishu_samples import sample_review_data
from pku_deadline_agent.zhihuishu_import import prepare_import, apply_import, source_associations

PREFIX = 'zhs_review_'
ACTIONS = {'暂不处理': 'skip', '拟新增独立任务': 'new', '拟关联已有任务': 'link', '拟修改已有截止': 'update'}
REASONS = {'boundary_unverified': '列表边界尚未验证', 'displayed_total_unknown': '页面总数未知',
           'fields_incomplete': '部分任务字段缺失', 'task_limit': '达到累计任务上限',
           'scroll_limit': '达到滚动上限', 'pagination_not_traversed': '分页尚未读取'}


def invalidate_review(*, keep_receipt=False):
    for key in list(st.session_state):
        if key.startswith(PREFIX):
            del st.session_state[key]
    if not keep_receipt:
        st.session_state.pop('zhs_import_result', None)


def render_zhihuishu_preview_page(path=None, *, report=None, import_target=None):
    st.subheader('智慧树任务预览')
    st.info('开发预览：智慧树列表使用虚构数据。所有选择和确认仅模拟，不保存任务，不连接智慧树或 AI。'
            if import_target is None else
            '列表与专用库均为虚构数据。默认预览不写库；另选“模拟数据库入库”并确认后才写入虚构库。正式写入未开放，不连接智慧树或 AI。')
    sample, example_tasks = sample_review_data()
    report = report if report is not None else sample
    if not report.evidence.startswith('synthetic'):
        invalidate_review()
        st.error('本轮界面只接受虚构列表，不接收真实浏览器数据。')
        return
    write_mode = False
    associations_available = ()
    if import_target is not None:
        try:
            import_target.check()
            if path is None or import_target.path.resolve() != Path(path).resolve():
                raise PermissionError('模拟目标与预览库不一致。')
            associations_available = source_associations(import_target)
        except (sqlite3.Error, OSError, ValueError, PermissionError):
            invalidate_review()
            st.error('无法验证专用模拟库，入库入口关闭。')
            return
        mode = st.radio('运行方式', ('模拟预览（不写库）', '模拟数据库入库'), key='zhs_mode', horizontal=True)
        write_mode = mode == '模拟数据库入库'
        if write_mode:
            st.warning('本入口会实际写入专用模拟数据库；虚构数据不会写入正式库。')
    reference = st.radio('对比对象', ('本地任务（只读）', '虚构待办（体验匹配）'), key='zhs_reference', horizontal=True)
    try:
        tasks = (ReadOnlyTaskRepository(path if path is not None else database_path()).list_tasks(include_completed=True)
                 if reference == '本地任务（只读）' else example_tasks)
        snapshot = build_review(report, tasks)
    except (sqlite3.Error, OSError, RuntimeError, ValueError):
        invalidate_review()
        st.error('无法只读读取或核验任务库；未将失败当成空库，也不会创建或迁移数据库。可切换虚构待办体验。')
        return
    can_write = write_mode and reference == '本地任务（只读）'
    digest = fingerprint((snapshot.digest, reference, write_mode))
    if st.session_state.get(PREFIX+'digest') != digest:
        previous_pending = st.session_state.get(PREFIX+'pending')
        previous_result = st.session_state.get('zhs_import_result')
        own_commit = bool(previous_pending and previous_result and previous_result[3].batch_id == previous_pending.batch_id)
        if previous_pending is not None and not own_commit:
            st.warning('本地任务或预览依据已变化，旧入库确认已撤销；未执行旧方案，请重新核对。')
        invalidate_review(keep_receipt=True)
        st.session_state[PREFIX+'digest'] = digest
        # Explicit widget values also reset the actual browser after a successful
        # commit; deleting server state alone can leave old client checkmarks.
        for item in snapshot.items:
            st.session_state[f'{PREFIX}pick_{item.index}'] = False
            st.session_state[f'{PREFIX}{item.index}_action'] = '暂不处理'
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
    choices, unknown_deadlines, chosen_links = {}, set(), {}
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
            only_unknown_due = item.warnings == (UNKNOWN_DEADLINE,)
            actions = (('暂不处理','拟新增独立任务') if can_write and only_unknown_due else
                       ('暂不处理',) if item.warnings else tuple(ACTIONS))
            action_label = st.selectbox('本次拟处理方式', actions,
                                        key=key+'action', disabled=not selected)
            action, target, reason, distinct = ACTIONS[action_label], None, '', False
            if can_write and only_unknown_due and action == 'new':
                if st.checkbox('按截止待确认保存，不补造日期或时刻',key=key+'unknown',disabled=not selected):
                    unknown_deadlines.add(item.index)
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
                if can_write and target:
                    links = {l['local_id']:l for l in associations_available if l['task_id']==target.id
                             and l['course']==item.task.course and l['kind']==item.task.kind}
                    if links:
                        local_link = st.selectbox('人工选择本地来源关联（不代表平台 ID）',[None,*links],
                            format_func=lambda v:'新建本地关联' if v is None else f'本地 {v[:8]} · {links[v]["title"]}',
                            key=key+'local_link',disabled=not selected)
                        if selected and local_link: chosen_links[item.index] = local_link
            if selected:
                choices[item.index] = ImportDecision(action, target.id if target else None,
                    target.revision if target else None, reason, distinct)
    st.subheader('拟执行操作（仅预览）')
    try:
        plan = simulate_choices(snapshot, choices,unknown_deadlines=frozenset(unknown_deadlines))
        error = None
    except ValueError as exc:
        plan, error = None, str(exc)
    decision_digest = fingerprint((digest, {i: asdict(c) for i, c in choices.items()},sorted(unknown_deadlines),chosen_links))
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
    if can_write:
        st.divider()
        st.subheader('人工确认入库（仅专用模拟库）')
        pending = st.session_state.get(PREFIX+'pending')
        if st.session_state.get(PREFIX+'pending_digest') != decision_digest:
            if pending is not None:
                st.warning('处理选择已变化，旧入库确认已撤销，请重新准备方案。')
            pending = None
            for name in ('pending','pending_digest','save_confirm','partial_confirm'):
                st.session_state.pop(PREFIX+name,None)
        if st.button('准备模拟库入库方案',key=PREFIX+'prepare',disabled=not(plan and plan.operations)):
            try:
                pending = prepare_import(import_target,snapshot,choices,
                    unknown_deadlines=frozenset(unknown_deadlines),associations=chosen_links)
            except (ValueError,sqlite3.Error,OSError,PermissionError):
                st.error('准备方案失败，请重新核对模拟库与处理选择。')
            else:
                st.session_state[PREFIX+'pending']=pending
                st.session_state[PREFIX+'pending_digest']=decision_digest
        if not coverage.complete:
            st.warning('本次仅导入部分任务，并非完整同步。未出现的已有任务不会改变。')
        # A fresh batch gets fresh widget identities. Disabled client widgets can
        # otherwise retain old checkmarks even after server-side state is cleared.
        confirmation_suffix = pending.batch_id if pending else 'none'
        reviewed = st.checkbox('我已核验选中任务及新增、关联、改期方案，确认写入模拟库',
                               key=PREFIX+'save_confirm_'+confirmation_suffix,disabled=pending is None)
        partial_ok = (st.checkbox('我理解本次仅导入部分任务，并非完整同步',key=PREFIX+'partial_confirm_'+confirmation_suffix,disabled=pending is None)
                      if not coverage.complete else True)
        if st.button('确认写入模拟数据库',key=PREFIX+'save',disabled=not(pending and reviewed and partial_ok)):
            try:
                receipt = apply_import(import_target,pending,confirmed=reviewed,partial_confirmed=partial_ok)
                after = ReadOnlyTaskRepository(import_target.path).list_tasks(include_completed=True)
            except (ValueError,sqlite3.Error,OSError,RuntimeError,PermissionError) as exc:
                st.error(str(exc) if isinstance(exc,ValueError) else '模拟库写入失败；请核对备份、权限和数据库。')
                for name in ('pending','pending_digest','save_confirm','partial_confirm'):
                    st.session_state.pop(PREFIX+name,None)
            else:
                st.session_state['zhs_import_result']=(import_target.database_id,tuple(snapshot.local_tasks),tuple(after),receipt)
                st.rerun()
    else:
        st.button('确认入库（正式写入未开放）',disabled=True,key=PREFIX+'production_closed')
        st.caption('正式库写入默认关闭。只有独立验收实例可选择“模拟数据库入库”；预览确认不会转换成真实保存。')
    last = st.session_state.get('zhs_import_result')
    if import_target and last and last[0]==import_target.database_id:
        _, before, after, receipt = last
        st.success(f'已写入模拟库：处理 {len(receipt.task_ids)} 项，任务 {len(before)} → {len(after)}。正式库未写入。')
        st.caption('写入前已做一致性备份；事务包含迁移、任务、来源关联和回执。未知截止仍显示待确认。')
        for label, rows in (('入库前（模拟库）',before),('入库后 Deadline 看板（模拟库）',after)):
            st.subheader(label)
            st.dataframe([{'编号':t.id,'标题':t.title,'课程':t.category,'截止（北京时间）':format_deadline(t.due_at),
                           '完成状态':'已完成' if t.completed else '未完成','来源':t.source,'版本':t.revision} for t in rows],hide_index=True)
