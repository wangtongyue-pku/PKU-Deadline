"""Week view: the same tasks, laid out Monday to Sunday.

A calendar is only useful if it never lies about a date: tasks without a deadline
are listed separately rather than dropped into a day, and unfinished tasks from
earlier weeks get their own block instead of disappearing off the left edge.
"""

from datetime import date, datetime, timedelta
from html import escape

import streamlit as st

# Same package: the edit / complete / delete / history controls are shared on
# purpose, so the week view cannot drift from the board.
from pku_deadline_agent.management_page import _render_task_actions
from pku_deadline_agent.time_utils import BEIJING, beijing_now, format_deadline

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def monday_of(moment: datetime) -> date:
    """Beijing Monday of the week containing `moment`."""
    day = moment.astimezone(BEIJING).date()
    return day - timedelta(days=day.weekday())


def week_buckets(tasks, monday: date):
    """Split tasks into seven days plus the two leftovers.

    Returns ``(days, undated, overdue)``:

    - ``days``: ``{date: [task, ...]}`` for that Monday..Sunday, ordered by time;
    - ``undated``: tasks with no deadline - never guessed into a day;
    - ``overdue``: unfinished tasks dated before this Monday.

    Tasks dated in a later week are simply not part of this week and are omitted.
    """
    days = {monday + timedelta(days=index): [] for index in range(7)}
    undated: list = []
    overdue: list = []
    for task in tasks:
        if getattr(task, "deleted_at", None):
            continue
        if task.due_at is None:
            undated.append(task)
            continue
        day = task.due_at.astimezone(BEIJING).date()
        if day in days:
            days[day].append(task)
        elif day < monday and not task.completed:
            overdue.append(task)
    for bucket in days.values():
        bucket.sort(key=lambda task: task.due_at.astimezone(BEIJING))
    overdue.sort(key=lambda task: task.due_at.astimezone(BEIJING))
    undated.sort(key=lambda task: (task.category or "", task.title))
    return days, undated, overdue


def card_label(task) -> str:
    """One line per task: state mark, time, title. The course goes to the tooltip."""
    if task.completed:
        mark = "✅ "
    else:
        mark = ""
    clock = task.due_at.astimezone(BEIJING).strftime("%H:%M") if task.due_at else "--:--"
    title = task.title if len(task.title) <= 18 else task.title[:17] + "…"
    return f"{mark}{clock} {title}"


def short_title(title: str, limit: int = 24) -> str:
    """Keep a card to a couple of lines; the full title stays in the tooltip."""
    text = title.strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def card_html(task) -> str:
    """A small card that can wrap: seven columns are too narrow for a button label.

    Everything user-provided is HTML-escaped; the card only carries text, and the
    full title lives in a native title attribute so long names stay readable.
    """
    done = task.completed
    strike = "text-decoration:line-through;opacity:.55;" if done else ""
    clock = task.due_at.astimezone(BEIJING).strftime("%H:%M") if task.due_at else "--:--"
    course = (f"<div style='font-size:.7rem;opacity:.6'>{escape(task.category)}</div>"
              if task.category else "")
    return (
        "<div style='border:1px solid rgba(128,128,128,.28);border-radius:.4rem;"
        "padding:.3rem .45rem;margin:.2rem 0;line-height:1.25'>"
        f"<div style='font-size:.66rem;opacity:.6'>{clock}</div>"
        f"<div style='font-size:.78rem;font-weight:600;{strike}' title='{escape(task.title)}'>"
        f"{escape(short_title(task.title))}</div>"
        f"{course}</div>"
    )


def render_week_view(repository) -> None:
    st.subheader("周视图")
    now = beijing_now()
    offset = int(st.session_state.get("week_offset", 0) or 0)
    monday = monday_of(now) + timedelta(weeks=offset)
    tasks = repository.list_tasks(include_completed=True)
    days, undated, overdue = week_buckets(tasks, monday)

    back, here, forward, spacer = st.columns([1, 1, 1, 4])
    if back.button("← 上一周", key="week_prev"):
        st.session_state["week_offset"] = offset - 1
        st.rerun()
    if here.button("本周", key="week_today", disabled=offset == 0):
        st.session_state["week_offset"] = 0
        st.rerun()
    if forward.button("下一周 →", key="week_next"):
        st.session_state["week_offset"] = offset + 1
        st.rerun()

    sunday = monday + timedelta(days=6)
    in_week = [task for bucket in days.values() for task in bucket]
    done = sum(1 for task in in_week if task.completed)
    spacer.markdown(
        f"**{monday.strftime('%Y-%m-%d')} — {sunday.strftime('%m-%d')}**　"
        f"本周 {len(in_week)} 项 · 已完成 {done} 项 · 逾期未完成 {len(overdue)} 项"
    )
    if offset == 0:
        st.caption("所有时间均为北京时间；「待确认」任务没有明确日期，因此不会出现在任何一天里。")

    if overdue:
        st.markdown(f"**⏰ 逾期未完成（{len(overdue)}）**")
        for task in overdue:
            st.markdown(
                f"<div style='border-left:3px solid #9B1C31;padding:.15rem .5rem;margin:.15rem 0'>"
                f"<span style='font-size:.7rem;opacity:.7'>{format_deadline(task.due_at)}</span>　"
                f"<span style='font-size:.82rem;font-weight:600'>{escape(short_title(task.title, 40))}</span>"
                f"<span style='font-size:.7rem;opacity:.6'>　{escape(task.category or '未分类')}</span></div>",
                unsafe_allow_html=True)

    columns = st.columns(7)
    today = now.astimezone(BEIJING).date()
    for index, (day, bucket) in enumerate(sorted(days.items())):
        column = columns[index]
        count = f" · {len(bucket)}" if bucket else ""
        today_mark = " · 今天" if day == today else ""
        column.markdown(
            f"**{WEEKDAYS[index]}** <span style='opacity:.6;font-size:.8rem'>"
            f"{day.strftime('%m-%d')}{today_mark}{count}</span>", unsafe_allow_html=True)
        if not bucket:
            column.caption("—")
            continue
        for task in bucket:
            column.markdown(card_html(task), unsafe_allow_html=True)

    if undated:
        st.divider()
        st.markdown(f"**待确认截止（{len(undated)}）**")
        st.caption("这些任务没有明确日期时刻，程序不会替你猜一个日子；补上后才会出现在上面的某一周。")
        columns = st.columns(min(3, len(undated)))
        for index, task in enumerate(undated):
            columns[index % len(columns)].markdown(card_html(task), unsafe_allow_html=True)

    # One picker for the whole page, same pattern as the board.
    visible = [*in_week, *overdue, *undated]
    by_id = {task.id: task for task in visible}
    if st.session_state.get("week_task") not in by_id:
        st.session_state.pop("week_task", None)
    if visible:
        st.divider()
        # Nothing is preselected: opening the page must not imply you chose a task.
        picked = st.selectbox(
            "选择要管理的任务", [None, *by_id], key="week_task",
            format_func=lambda value: "（从上表选一条）" if value is None else
            f"#{value} {by_id[value].title} · {format_deadline(by_id[value].due_at)}",
        )
        if picked is not None:
            with st.expander(f"管理「{by_id[picked].title}」", expanded=True):
                _render_task_actions(repository, by_id[picked], [by_id[picked]], now, False)
    else:
        st.caption("本周没有任务，也没有逾期或待确认的项目。")
