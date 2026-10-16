"""Local task board, explicit import decisions, history, reminders and backups."""

from datetime import datetime

import streamlit as st

from pku_deadline_agent.database import TaskChanged
from pku_deadline_agent.models import TASK_TYPES
from pku_deadline_agent.reminders import parse_hours, preview_due
from pku_deadline_agent.task_management import ImportDecision, filter_tasks, urgency
from pku_deadline_agent.time_utils import BEIJING, beijing_now, format_deadline, normalize_datetime

ACTION_LABELS = {"create": "新增", "edit": "编辑", "postpone": "延期", "advance": "提前", "deadline_correction": "截止修正",
                 "complete": "标记完成", "reopen": "恢复待办", "delete": "移入回收站", "restore": "恢复任务", "link_notice": "关联重复通知"}
SOURCE_LABELS = {"manual": "手动录入", "wechat": "微信通知（手动粘贴）", "teaching_platform": "教学网", "programming_grid": "编程网格", "zhihuishu": "智慧树（人工确认）"}


def render_import_choice(repository, candidate, origin, key, on_change=None):
    matches = repository.suggestions(candidate, origin)
    if not matches:
        return ImportDecision("new")
    st.warning("发现重复或截止时间冲突；请选择处理方式，确认前不会覆盖。")
    for match in matches:
        task = match.task
        st.write(f"#{task.id} {task.title} · {task.category or '未分类'} · {task.task_type} · {'已完成' if task.completed else '未完成'}")
        st.caption(f"{match.reason}。原截止：{format_deadline(task.due_at)} → 候选截止：{format_deadline(candidate.due_at)}")
    action = st.selectbox("处理方式", ("请选择处理方式", "关联重复通知，保留原任务", "更新已有任务", "作为独立任务新增", "忽略此任务"), key=key + "_action", on_change=on_change)
    if action == "请选择处理方式":
        return None
    if action == "忽略此任务":
        return ImportDecision("skip")
    if action == "作为独立任务新增":
        return ImportDecision("new", allow_distinct=True, reason="用户核对相似任务后确认独立新增")
    target = st.selectbox("核对目标任务", [match.task.id for match in matches], format_func=lambda value: f"#{value}", key=key + "_target", on_change=on_change)
    task = next(match.task for match in matches if match.task.id == target)
    if action == "关联重复通知，保留原任务":
        st.info("只记录通知指纹关联，任务名称、截止时间和完成状态均保留。")
        return ImportDecision("link", target, task.revision, "用户确认重复通知，关联已有任务")
    if task.completed:
        st.warning("目标任务已完成；本次更新保留完成状态，需要重新提醒时请在看板恢复待办。")
    if candidate.due_at is None and task.due_at is not None:
        clear = st.checkbox("我确认将已有截止时间清空为待确认", key=key + "_clear_due", on_change=on_change)
        if not clear:
            return None
    reason = st.text_input("更新原因", value="通知明确改期" if candidate.deadline_changed else "用户确认截止时间修正", max_chars=500, key=key + "_reason", on_change=on_change)
    if not reason.strip():
        st.error("请填写更新原因。")
        return None
    return ImportDecision("update", target, task.revision, reason)


def _due_phrase(task, now: datetime) -> str:
    """How far away the deadline is, in words. Never guesses an unknown due date."""
    if task.due_at is None:
        return "截止时间待确认"
    seconds = (task.due_at - normalize_datetime(now)).total_seconds()
    if seconds <= 0:
        overdue = -seconds
        days = int(overdue // 86400)
        return f"已过期 {days} 天" if days >= 1 else f"已过期 {int(overdue // 3600)} 小时"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟后"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时后"
    days = int(seconds // 86400)
    if days == 1:
        return "明天"
    if days <= 7:
        return f"{days} 天后"
    return f"{days} 天后（{task.due_at.month} 月 {task.due_at.day} 日）"


def _group_label(task, now: datetime) -> str:
    """Which urgency group a task belongs to, for the collapsed sections."""
    if task.due_at is None:
        return "截止时间待确认"
    seconds = (task.due_at - normalize_datetime(now)).total_seconds()
    if seconds <= 0:
        return "已过期"
    if seconds <= 86400:
        return "今天到期"
    if seconds <= 3 * 86400:
        return "3 天内"
    if seconds <= 7 * 86400:
        return "本周内"
    return "更晚"


GROUP_ORDER = ("已过期", "今天到期", "3 天内", "本周内", "更晚", "截止时间待确认")
GROUP_HINT = {
    "已过期": "已经超过截止时间；如果需要，可以改期或标记完成。",
    "今天到期": "24 小时内到期。",
    "3 天内": "三天内到期。",
    "本周内": "一周内到期。",
    "更晚": "一周以后。",
    "截止时间待确认": "通知里没有明确时刻，需要你自己补上；程序不会替你猜。",
}


def render_task_board(repository):
    st.subheader("待办列表")
    all_tasks = repository.list_tasks(include_completed=True, include_deleted=True)
    if not all_tasks:
        st.info("暂无任务。展开下面的「添加任务」，或用「编程网格导入」同步一次。")
        return
    recycle = st.checkbox("查看回收站", key="board_recycle")
    tasks = [task for task in all_tasks if bool(task.deleted_at) == recycle]
    left, middle, right = st.columns(3)
    category = left.selectbox("课程筛选", ["全部", *sorted({task.category for task in tasks})], format_func=lambda value: value or "未分类", key="board_category")
    kind = middle.selectbox("类型筛选", ("全部", *TASK_TYPES), key="board_type")
    status = right.selectbox("状态筛选", ("未完成", "全部", "已完成", "已过期") if not recycle else ("全部", "未完成", "已完成"), key="board_status")
    now = beijing_now()
    selected = filter_tasks(tasks, category=category, task_type=kind, status=status, now=now)
    order = st.selectbox("截止时间排序", ("由近到远", "由远到近"), key="board_order")
    if order == "由远到近":
        selected = list(reversed([task for task in selected if task.due_at])) + [task for task in selected if not task.due_at]
    _render_overview(selected, now, recycle)
    if not selected:
        st.info("筛选结果为空。可切换状态筛选，或退出回收站。")
        return
    st.dataframe([{"编号": task.id, "标题": task.title, "课程 / 活动": task.category or "—", "类型": task.task_type,
                   "截止时间（北京）": format_deadline(task.due_at), "状态": urgency(task, now)} for task in selected],
                 hide_index=True, use_container_width=True,
                 column_config={
                     "编号": st.column_config.NumberColumn(width="small"),
                     "标题": st.column_config.TextColumn(width="large"),
                     "课程 / 活动": st.column_config.TextColumn(width="medium"),
                     "类型": st.column_config.TextColumn(width="small"),
                     "截止时间（北京）": st.column_config.TextColumn(width="medium"),
                     "状态": st.column_config.TextColumn(width="small"),
                 })
    task_id = st.selectbox("选择要管理的任务", [task.id for task in selected], format_func=lambda value: f"#{value} " + next(task.title for task in selected if task.id == value), key="board_task")
    task = next(task for task in selected if task.id == task_id)
    _render_task_actions(repository, task, selected, now, recycle)


def _render_overview(selected, now: datetime, recycle: bool) -> None:
    """The at-a-glance answer to "what do I have to do"."""
    if recycle:
        st.caption(f"回收站共 {len(selected)} 条。恢复后才会重新出现在待办里。")
        return
    live = [task for task in selected if not task.completed]
    groups: dict[str, list] = {label: [] for label in GROUP_ORDER}
    for task in live:
        groups[_group_label(task, now)].append(task)
    overdue = len(groups["已过期"])
    today = len(groups["今天到期"])
    soon = len(groups["3 天内"])
    unknown = len(groups["截止时间待确认"])
    done = len(selected) - len(live)
    columns = st.columns(4)
    columns[0].metric("已过期", overdue, delta=None if not overdue else "需要处理", delta_color="inverse")
    columns[1].metric("24 小时内", today)
    columns[2].metric("3 天内", soon)
    columns[3].metric("待确认截止", unknown)
    if done:
        st.caption(f"另有 {done} 条已完成（当前筛选内）。标记完成或移入回收站后停止新提醒。")
    lines = []
    for label in GROUP_ORDER:
        if groups[label]:
            lines.append(f"**{label}** {len(groups[label])}")
    if lines:
        st.caption("未完成：" + " · ".join(lines) + "。分组按截止时间，待确认的永远排在最后。")


def _render_task_actions(repository, task, selected, now, recycle) -> None:
    """Complete / delete / edit / history for the chosen task, one tab each.

    The controls used to sit open under the table, which pushed the list itself
    into the background. Keeping them in tabs means the list stays the thing you
    look at, and the controls are one click away when you need them.
    """
    st.markdown(f"**正在管理：** #{task.id} {task.title}")
    bits = []
    if task.category:
        bits.append(f"课程 / 活动：{task.category}")
    bits.append(f"截止：{format_deadline(task.due_at)}")
    bits.append(_due_phrase(task, now))
    st.caption(" · ".join(bits))

    prefix = f"manage_{task.id}_{task.revision}"
    if recycle:
        try:
            if st.button("从回收站恢复", key=prefix + "_restore"):
                repository.set_deleted(task.id, task.revision, False, confirmed=True)
                st.rerun()
        except (ValueError, TaskChanged) as exc:
            st.error(str(exc))
        return

    try:
        left, right = st.columns(2)
        if left.button("恢复为待办" if task.completed else "标记完成", key=prefix + "_complete"):
            repository.set_completed(task.id, task.revision, not task.completed, confirmed=True)
            st.rerun()
        if right.button("移入回收站", key=prefix + "_delete"):
            repository.set_deleted(task.id, task.revision, True, confirmed=True)
            st.rerun()
    except (ValueError, TaskChanged) as exc:
        st.error(str(exc))

    edit_tab, history_tab = st.tabs(["编辑任务与截止时间", "变更历史"])
    with edit_tab:
        _render_edit_form(repository, task, now, prefix)
    with history_tab:
        _render_history(repository, task)


def _render_edit_form(repository, task, now, prefix) -> None:
    try:
        explicit = st.checkbox("截止时间已明确", value=task.due_at is not None, key=prefix + "_explicit")
        with st.form(prefix + "_edit"):
            title = st.text_input("修改任务名称", value=task.title, max_chars=200, key=prefix + "_title")
            course = st.text_input("修改课程或活动", value=task.category, max_chars=100, key=prefix + "_category")
            task_type = st.selectbox("修改任务类型", TASK_TYPES, index=TASK_TYPES.index(task.task_type), key=prefix + "_type")
            date = st.date_input("修改截止日期（北京）", value=task.due_at.date() if task.due_at else now.date(), disabled=not explicit, key=prefix + "_date")
            time = st.time_input("修改截止时刻（北京）", value=task.due_at.time() if task.due_at else None, disabled=not explicit, key=prefix + "_time")
            reason = st.text_input("修改原因", value="用户手动修正", max_chars=500, key=prefix + "_reason")
            confirmed = st.checkbox("确认保存上述修改", key=prefix + "_confirmed")
            submitted = st.form_submit_button("保存修改")
        if submitted:
            if explicit and (date is None or time is None):
                st.error("请补全日期和时刻，或取消截止时间已明确。")
            else:
                due = datetime.combine(date, time, tzinfo=BEIJING) if explicit else None
                repository.edit_task(task.id, task.revision, title=title, category=course, task_type=task_type, due_at=due, reason=reason, confirmed=confirmed)
                st.rerun()
    except (ValueError, TaskChanged) as exc:
        st.error(str(exc))


def _render_history(repository, task) -> None:
    records = repository.history(task.id)
    if not records:
        st.info("该任务在升级前创建，旧版本没有变更历史；之后的修改会自动记录。")
        return
    st.dataframe([{"修改时间（北京）": format_deadline(datetime.fromisoformat(row["changed_at"])), "操作": ACTION_LABELS.get(row["action"], row["action"]),
                   "原截止": format_deadline(datetime.fromisoformat(row["old_due_at"])) if row["old_due_at"] else "待确认",
                   "新截止": format_deadline(datetime.fromisoformat(row["new_due_at"])) if row["new_due_at"] else "待确认",
                   "原因": row["reason"], "通知来源": SOURCE_LABELS.get(row["source"], row["source"]), "通知指纹": row["notice_hash"][:12] or "手动 / 旧版本"} for row in records], hide_index=True, use_container_width=True)


def render_reminder_settings(repository):
    st.subheader("提醒与备份")
    st.info("提醒由独立 Python 进程检查，不会因关闭网页而停止。仅打开此页面不会启动提醒。")
    st.warning("关机、休眠、未登录桌面或提醒进程停止时不能弹窗。Windows 勿扰 / 通知设置也可能隐藏弹窗；记录为已发送仅代表提交给系统，不保证你已看到。")
    config = repository.reminder_config()
    enabled = st.checkbox("启用提醒规则", value=config["enabled"], key="reminder_enabled")
    hours = st.text_input("提前多少小时提醒（逗号分隔，可用小数）", value=",".join(f"{value / 3600:g}" for value in config["offsets"]), key="reminder_hours")
    st.caption("默认 72、24、3 小时，即提前 3 天、1 天、3 小时。修改规则后点保存；不会重发已记录的同一档位。")
    if st.button("保存提醒配置", key="reminder_save"):
        try:
            repository.configure_reminders(parse_hours(hours), enabled=enabled, confirmed=True)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.success("提醒配置已保存。")
    st.caption("提醒进程最近检查（北京）：" + (format_deadline(datetime.fromisoformat(config["last_check_at"])) if config["last_check_at"] else "尚无记录"))
    st.write("在另一个 PowerShell 窗口、项目目录中运行（无需配置 API Key）：")
    path = str(repository.path.resolve()).replace("'", "''")
    command = f".\\.venv\\Scripts\\python.exe -m pku_deadline_agent.reminders --database '{path}'"
    st.code(command + " --dry-run --once\n# 正式运行，保持窗口开启；Ctrl+C 停止\n" + command + " --notify", language="powershell")
    st.caption("默认每 60 秒检查；停止后重启只补当前最紧急的一档，不连续推送所有错过档位。已过期、已完成、回收站任务不再提醒。异常 / 崩溃的保留记录不自动重试，避免重复推送。")
    due = preview_due(repository)
    st.write(f"当前待提醒：{len(due)} 项（预览不会发送或消耗提醒次数）")
    if due:
        st.dataframe([{"编号": task.id, "标题": task.title, "截止（北京）": format_deadline(task.due_at), "提前小时档位": offset / 3600} for task, offset in due], hide_index=True)
    with st.expander("最近提醒记录"):
        events = repository.reminder_events()
        if events:
            states = {"sent": "已提交给 Windows", "failed": "失败（不自动重发）", "reserved": "未决（不自动重发）", "superseded": "已取消 / 略过"}
            st.dataframe([{"任务编号": row["task_id"], "截止（北京）": format_deadline(datetime.fromisoformat(row["due_at"])),
                           "提前小时": row["lead_seconds"] / 3600, "状态": states.get(row["state"], "未知"),
                           "记录时间（北京）": format_deadline(datetime.fromisoformat(row["updated_at"]))} for row in events], hide_index=True)
        else:
            st.info("暂无提醒记录。")
    st.subheader("本地数据库备份")
    st.caption("旧数据库升级前自动生成一致性备份；也可手动备份。备份包含个人任务，应留在本机，不能提交 Git 或上传 AI。回收站不会永久删除数据。")
    if st.button("立即生成数据库备份", key="database_backup"):
        target = repository.backup()
        st.success("备份完成：" + str(target))
