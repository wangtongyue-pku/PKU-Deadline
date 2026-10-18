"""Stage 6 Streamlit entry: import a programming-grid course JSON.

The page never fetches anything itself. It hands an uploaded payload to the
stage 6 planning functions, shows what would change, and writes only after the
user ticks a confirmation. Everything the planning layer guarantees still holds
here: nothing is deleted, an untrustworthy batch never becomes actionable, and a
set that is absent from this file is simply not mentioned.
"""

import os

import streamlit as st

from pku_deadline_agent import grid_courses, grid_session
from pku_deadline_agent.grid_session import (
    SessionAuthRequired,
    SessionBrowserUnavailable,
    SessionLayoutChanged,
    SessionNetworkError,
)
from pku_deadline_agent.mock_sources import ProgrammingGridJsonAdapter
from pku_deadline_agent.sources import SourceError, SourceRejected
from pku_deadline_agent.sync import (
    DEADLINE_CHANGED,
    NEW,
    POSSIBLE_DUPLICATE,
    UNCHANGED,
    ImportFileError,
    apply_sync_plan,
    decode_grid_bytes,
    describe_plan,
    plan_from_fetch,
    plan_sync,
    parse_grid_payload,
)
from pku_deadline_agent.task_management import ImportDecision
from pku_deadline_agent.time_utils import format_deadline

COURSE_ID_ENV = "PKU_GRID_COURSE_ID"

_KIND_LABEL = {
    NEW: "新题集",
    UNCHANGED: "已有且无变化",
    DEADLINE_CHANGED: "截止时间变化",
    POSSIBLE_DUPLICATE: "可能重复（未关联的平台 ID）",
}


def _uploaded_bytes():
    """Return (filename, bytes) for the chosen file, or None.

    The widget is always created, so the page looks identical under the test
    harness; only the read-back is isolated, because AppTest cannot drive a file
    uploader. Tests replace this one call and exercise everything downstream.
    """
    return _read_upload(st.file_uploader("选择课程页 JSON 文件", type=("json",), key="grid_file"))


def _read_upload(upload):
    if upload is None:
        return None
    return upload.name, upload.getvalue()


def render_grid_import_page(repository) -> None:
    st.subheader("编程网格导入")

    # A failure never changes the plan, so it is reported in the same run that
    # produced it: no rerun, and therefore no chance of the message being
    # discarded together with the run that raised it.
    _render_pending_messages()

    _render_sync_section(repository)
    _render_sync_note()

    st.divider()
    st.markdown("**或者：手动导入 JSON 文件**（同步不可用时的备用方式）")
    st.caption(
        "在编程网格打开课程页，把地址末尾改成 `?type=json`，另存为 UTF-8 的 JSON 文件后在这里上传。"
        "本页不会把内容发送给任何 AI 或外部服务。"
    )
    chosen_file = _uploaded_bytes()
    if chosen_file is not None:
        _plan_from_upload(repository, *chosen_file)

    # Messages raised by the handlers above are shown in this same run.
    _render_pending_messages()

    plan = st.session_state.get("grid_plan")
    if plan is None:
        _render_rules()
        return

    _render_summary(plan)
    if plan.rejections:
        _render_rejections(plan)
        st.error("本次有无法解析的题集，已整批中止；数据库未被修改。请修复文件或平台数据后重试。")
        _render_rules()
        return
    if plan.warnings:
        for text in plan.warnings:
            st.warning(text)
    if not plan.trustworthy:
        st.error("本次结果没有通过可信检查，不能用于更新本地任务；数据库未被修改。")
        _render_rules()
        return
    if not plan.proposals:
        st.info("这个课程页响应里没有题集。数据库未被修改，也不会删除任何已有任务。")
        _render_rules()
        return

    _render_proposals(repository, plan)
    _render_rules()


def _generation() -> int:
    return st.session_state.get("grid_generation", 0)


def _generation_for(plan) -> int:
    """A fresh widget-key generation for a plan, advanced once per new plan.

    Doing this while rendering means the keys always match what is on screen, and
    no extra script run is needed — a forced restart would discard the message
    that reports what just happened.
    """
    if st.session_state.get("grid_rendered_plan") is not plan:
        st.session_state["grid_generation"] = _generation() + 1
        st.session_state["grid_rendered_plan"] = plan
    return _generation()


def _render_sync_section(repository) -> None:
    """One-click sync: opens a browser you log into, then reads your courses."""
    st.markdown("**一键同步**")
    installed = _browser_available()
    if not installed:
        st.warning(
            "浏览器组件尚未安装（可选）。安装后即可一键同步：在项目目录执行 "
            ".venv\\Scripts\\python.exe -m pip install -e .[browser] "
            "安装前请先确认你已经同意添加这个可选依赖。手动导入始终可用。"
        )

    remembered = grid_courses.known()
    default_id = os.environ.get(COURSE_ID_ENV, "").strip()
    if remembered:
        labels = {course.course_id: course for course in remembered}
        # Pre-fill helper, never a replacement: the text field stays available for a
        # course you have not synced yet.
        st.selectbox(
            "从同步过的课程选择（可选）", [""] + list(labels), key="grid_course_pick",
            format_func=lambda value: "不选，手动填写下面的编号" if not value else labels[value].display(),
            on_change=_prefill_course, help="编号只存在本机；选好后会填进下面的输入框。",
        )
    course_id = st.text_input(
        "课程编号（课程页地址中 /course/ 后面的 32 位十六进制）",
        value=default_id, key="grid_sync_course",
        help="只读取这一门课。可先从一个课程开始，确认无误后再逐个添加。",
    )
    st.caption("程序不会读取你的密码，也不会读取其他浏览器的数据；登录在它打开的窗口里由你完成。")

    if st.button("立即同步", type="primary", disabled=not installed, key="grid_sync_run"):
        _run_sync(repository, course_id)


# The failure kinds are distinguishable on purpose: the right reaction differs.
# Auth needs you to log in, network needs a retry, a changed page needs code work.
_SESSION_MESSAGES = (
    (SessionAuthRequired, "需要登录（登录状态已失效）"),
    (SessionNetworkError, "网络或服务端问题"),
    (SessionLayoutChanged, "页面结构可能已改版"),
    (SessionBrowserUnavailable, "浏览器组件不可用"),
)


def _session_error_message(error: Exception) -> str:
    for kind, label in _SESSION_MESSAGES:
        if isinstance(error, kind):
            return f"{label}：{error}"
    return f"同步失败（{error.__class__.__name__}）：{error}"


def _show_sync_error(message: str) -> None:
    """Keep the message for the next run: the rerun clears this run's output."""
    st.session_state["grid_error"] = message


def _run_sync(repository, course_id: str) -> None:
    _begin_attempt()
    if not course_id.strip():
        _show_sync_error("请先填写课程编号。课程页地址里 /course/ 后面那 32 位十六进制就是它。")
        return
    session = grid_session.PlaywrightGridSession(allow_real=True, headless=False)
    try:
        with st.spinner("正在打开浏览器，请在窗口中完成登录（若已登录会自动继续）…"):
            session.open_and_ensure_login()
            outcome = grid_session.fetch_courses(session, [course_id.strip()])
    except Exception as error:
        # Auth, network, changed page and a missing component are reported as
        # themselves, never as "no sets". Nothing was written.
        #
        # No rerun here on purpose: a failure does not change the plan, so the
        # widget keys are still valid and the message can be shown directly. That
        # also keeps the message from being discarded together with the run.
        _show_sync_error(_session_error_message(error))
        return
    finally:
        session.close()

    if outcome.auth_required:
        _forget()
        _show_sync_error("登录状态已失效，请重新登录后再同步。数据库未被修改。")
        return
    if outcome.issues:
        _forget()
        _show_sync_error(
            "有课程没有读到，本次整批中止；数据库未被修改，已有任务不会被删除。原因："
            + " ｜ ".join(outcome.issues)
        )
        return
    try:
        plan = plan_from_fetch(outcome.documents, repository, ProgrammingGridJsonAdapter)
    except (SourceRejected, ValueError) as error:
        _forget()
        _show_sync_error(f"取到的数据无法生成导入方案，数据库未被修改：{error}")
        return
    # Success does change the plan, so the preview must be rendered by a run whose
    # widget keys match the new generation.
    _forget()
    _remember_course(course_id.strip(), outcome.documents)
    st.session_state["grid_plan"] = plan
    st.session_state["grid_source"] = "sync"
    st.success(f"同步完成：{describe_plan(plan)}。请核对后确认写入。")



def _prefill_course() -> None:
    """Copy the picked course id into the text field (runs before it is created)."""
    picked = st.session_state.get("grid_course_pick")
    if picked:
        st.session_state["grid_sync_course"] = picked


def _remember_course(course_id: str, documents) -> None:
    """Best effort: pre-filling a form must never break a successful sync."""
    label = ""
    for document in documents:
        label = (getattr(document, "course", "") or getattr(document, "title", "") or "").strip()
        if label:
            break
    try:
        grid_courses.remember(course_id, label)
    except (OSError, ValueError):
        pass


def _browser_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def _render_sync_note() -> None:
    with st.expander("一键同步会做什么、不会做什么", expanded=False):
        st.write("- **会**打开一个属于本程序的浏览器窗口；登录由你自己在里面完成。")
        st.write("- **不会**读取你的密码，也不会读取 Chrome / Edge 里你平时的浏览数据。")
        st.write("- **只读**你自己有权查看的课程页；不提交作业、不改动平台数据。")
        st.write("- 登录状态由浏览器自己按加密方式保存在本机 `data/grid-session/`（已排除在 Git 之外）。")
        st.write("- **不保存密码**。登录过期后只会提示你重新登录，不会自动重登。")
        st.write("- 取到数据后**先给你预览**，只有你确认才会写入本地数据库；不会因此删除任何已有任务。")


def _plan_from_upload(repository, filename: str, raw: bytes) -> None:
    """Build a fresh plan. Any change to the file invalidates an older preview."""
    digest = f"{filename}:{len(raw)}"
    _begin_attempt()
    if st.session_state.get("grid_digest") == digest:
        # Same file: keep the plan object so the widget keys stay stable across
        # reruns. Rebuilding it would give every rerun a new identity, which would
        # advance the generation and change the keys the user is interacting with.
        return
    if st.session_state.get("grid_failed") == digest:
        # Already refused this exact file. Show the reason again and, above all, do
        # not restart: a rerun here would repeat forever.
        st.error(st.session_state.get("grid_error") or "这个文件无法使用，已中止导入。")
        return
    try:
        payload = decode_grid_bytes(raw)
        result = parse_grid_payload(payload, ProgrammingGridJsonAdapter)
        try:
            plan = plan_sync(result.records, result, repository)
        except SourceError as error:
            # An untrustworthy batch: say plainly that the whole import stopped and
            # why, instead of showing a generic planning failure.
            raise SourceRejected(_untrusted_message(result, error)) from None
    except (ImportFileError, SourceRejected, SourceError, ValueError) as error:
        # Record the refusal before restarting, so the next run reports it once
        # instead of restarting again.
        st.session_state["grid_failed"] = digest
        _forget()
        _show_sync_error(str(error))
        return
    st.session_state.pop("grid_failed", None)
    st.session_state.pop("grid_error", None)
    st.session_state["grid_digest"] = digest
    st.session_state["grid_payload"] = payload
    st.session_state["grid_plan"] = plan
    # A new plan means a new widget-key generation, which _generation_for assigns
    # while rendering it; so an earlier confirmation cannot carry over.


def _forget() -> None:
    """Drop the previewed payload and plan.

    Deliberately leaves the messages alone: failure paths call this *before*
    setting their own message, so clearing messages here would erase the very
    explanation the user needs. Stale messages are cleared when a new attempt
    starts, by ``_begin_attempt``.
    """
    for key in ("grid_payload", "grid_plan", "grid_digest"):
        st.session_state.pop(key, None)


def _begin_attempt() -> None:
    """Clear messages from a previous attempt so only the newest one is shown."""
    for key in ("grid_error", "grid_info", "grid_written"):
        st.session_state.pop(key, None)


def _render_pending_messages() -> None:
    """Show and consume any message set by an earlier handler in this run."""
    error = st.session_state.pop("grid_error", None)
    if error:
        st.error(error)
    info = st.session_state.pop("grid_info", None)
    if info:
        st.info(info)
    written = st.session_state.pop("grid_written", None)
    if written:
        st.success(written)


def _untrusted_message(result, error) -> str:
    total = len(result.rejections) + len(result.warnings)
    parts = [f"本次有 {total} 项无法确认，已整批中止；数据库未被修改。"]
    for item in result.rejections:
        parts.append(f"无法解析：{item.source_id or '（无平台标识）'}：{item.reason}")
    parts.extend(result.warnings)
    return " ｜ ".join(parts)


def _render_summary(plan) -> None:
    st.write(f"课程：**{plan.course or '（响应未提供课程名）'}**")
    if plan.fetched_at is not None:
        st.caption(f"解析时间（北京）：{format_deadline(plan.fetched_at)}")
    st.write(describe_plan(plan))


def _render_rejections(plan) -> None:
    with st.expander(f"无法解析的条目（{len(plan.rejections)}）", expanded=True):
        for item in plan.rejections:
            st.write(f"- {item.source_id or '（无平台标识）'}：{item.reason}")


def _render_proposals(repository, plan) -> None:
    generation = _generation_for(plan)
    selectable = [item for item in plan.proposals if item.kind != UNCHANGED]
    chosen: dict[int, ImportDecision] = {}

    if not selectable:
        st.success("所有题集都已在本地且截止时间一致，无需导入。")
        return

    st.markdown("**逐条核对**（取消勾选可跳过某一条）")
    for index, item in enumerate(plan.proposals):
        key = f"grid_pick_{generation}_{index}"
        if item.kind == UNCHANGED:
            st.checkbox(f"〔{_KIND_LABEL[item.kind]}〕{item.record.task_title} · {format_deadline(item.record.deadline)}",
                        value=False, disabled=True, key=key)
            continue
        checked = st.checkbox(
            f"〔{_KIND_LABEL[item.kind]}〕{item.record.task_title} · {format_deadline(item.record.deadline)}",
            value=item.kind in (NEW, DEADLINE_CHANGED), key=key,
        )
        st.caption(_detail(item))
        if item.kind == POSSIBLE_DUPLICATE:
            st.caption(f"建议：如果要导入，它会成为一个**独立任务**（不覆盖 #{item.task_id}）。")
        if not checked:
            continue
        if item.kind == DEADLINE_CHANGED:
            reason = st.text_input("修改原因（必填，1–500 字）", key=f"grid_reason_{generation}_{index}")
            st.caption(f"将把 #{item.task_id} 从 {format_deadline(item.current_deadline)} 改为 {format_deadline(item.record.deadline)}")
            chosen[index] = ImportDecision("update", target_id=item.task_id,
                                           expected_revision=item.expected_revision, reason=reason or "")
        else:
            chosen[index] = ImportDecision("new", allow_distinct=True,
                                           reason="经页面核对后从编程网格导入")

    st.divider()
    confirmed = st.checkbox("我已核对以上内容，确认按所选写入本地数据库",
                            key=f"grid_confirm_{generation}")
    if st.button("执行导入", type="primary", disabled=not confirmed, key=f"grid_apply_{generation}"):
        _apply(repository, plan, chosen)


def _detail(item) -> str:
    record = item.record
    parts = [f"平台 ID `{record.source_id}`", f"课程 {record.course or '（未提供）'}"]
    if record.published_at is not None:
        parts.append(f"开放时间 {format_deadline(record.published_at)}")
    parts.append(f"平台类型 {record.task_type}")
    parts.append(item.reason)
    return " ｜ ".join(parts)


def _apply(repository, plan, chosen) -> None:
    _begin_attempt()
    if not chosen:
        st.warning("没有选中任何条目，未写入数据库。")
        return
    decisions = []
    for index, item in enumerate(plan.proposals):
        decisions.append(chosen.get(index) or ImportDecision("skip", target_id=item.task_id, reason="本次未选择"))
    for decision in decisions:
        if decision.action == "update" and not decision.reason.strip():
            st.error("截止时间变更必须填写修改原因；本次未写入。")
            return
    try:
        created = apply_sync_plan(repository, plan, decisions, confirmed=True)
    except ValueError as error:
        st.error(str(error))
        return
    except Exception as error:  # sqlite3.Error, OSError from the storage layer
        st.error(f"写入失败（{error.__class__.__name__}），请检查数据库权限或可用空间。")
        return
    _forget()
    st.success(f"已写入 {len(created)} 条；其余未选中的条目未改动，已有任务未被删除。")



def _render_rules() -> None:
    with st.expander("这个页面保证什么", expanded=False):
        st.write("- **不会删除任何任务**：本次文件里没出现的题集不会被删除、取消或标记完成。")
        st.write("- **不会重复**：以平台题集 ID 为准，同一份文件导入两次，第二次全部显示「已有且无变化」。")
        st.write("- **不会误合并**：标题相同但平台 ID 不同的题集各自建任务；标题相似绝不自动合并。")
        st.write("- **数据不可信就不写**：只要有条目解析失败，整批中止并报告，不做部分导入。")
        st.write("- **只读你的文件**：不联网、不登录，也不把内容发送给任何 AI 或外部服务。")
