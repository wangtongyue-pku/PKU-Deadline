"""Stage 7D: the school-mailbox page (connection, intelligence board, import).

Design rules this page follows, all of them already enforced by the layers below:

* the page itself never talks IMAP. It delegates to ``EmailSyncService`` (which
  owns the sync state) and only ever displays what came back;
* nothing is written to the task database until the user confirms a reviewed
  plan; a batch that is not trustworthy cannot even become a plan;
* an activity time (活动举办时间) is shown, never auto-imported, and a time without
  a date (待确认时间) is shown for manual entry;
* real credentials are optional and in-memory by default. The page ships an
  offline demo mailbox so the whole flow is usable and reviewable without any
  account, and it never writes a password or a code into logs.

The AI section is a **gate, not a sender**: it prepares the minimal payload,
shows it, and refuses to run unless the user confirms that exact batch. Dispatch
stays in the existing parse service, which is where the real per-call
authorisation and the usage budget live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import streamlit as st

from pku_deadline_agent.email_adapter import (
    DEFAULT_FOLDER,
    DEFAULT_FETCH_LIMIT,
    IMAP_HOST,
    IMAP_PORT,
    EmailAdapterError,
    EmailAuthRequired,
    EmailLayoutChanged,
    EmailNetworkError,
)
from pku_deadline_agent.email_credentials import MailCredentials, SessionCredentialStore
from pku_deadline_agent.email_demo import demo_mailbox
from pku_deadline_agent.email_import import apply_email_plan, plan_email_import
from pku_deadline_agent.email_intel import (
    ALL_CATEGORIES,
    ALL_IMPORTANCE,
    ALL_TIME,
    BoardFilters,
    ClassificationFeedback,
    EmailFeedbackStore,
    TIME_WINDOWS,
    build_ai_payload,
    build_daily_digest,
    filter_digest,
)
from pku_deadline_agent.email_models import (
    EMAIL_CATEGORIES,
    IMPORTANCE_HIGH,
    IMPORTANCE_LEVELS,
)
from pku_deadline_agent.email_sync import (
    INITIAL_DAYS,
    INITIAL_MAX,
    EmailSyncReport,
    EmailSyncService,
    MailboxStateStore,
    adapter_factory_from,
)
from pku_deadline_agent.email_imap import RealImapAdapter
from pku_deadline_agent.sync import (
    DEADLINE_CHANGED,
    NEW,
    POSSIBLE_DUPLICATE,
    UNCHANGED,
)
from pku_deadline_agent.task_management import ImportDecision
from pku_deadline_agent.time_utils import beijing_now, format_deadline

MODE_DEMO = "演示模式（离线虚构邮件，不连接真实邮箱）"
MODE_REAL = "真实邮箱（只读 IMAP）"

# Session key that may hold an offline mailbox factory for the demo mode.
DEMO_FACTORY_KEY = "email_demo_factory"

# Defaults for the one-off bounded read: small enough to state in one sentence.
BOUNDED_HOURS_DEFAULT = 48
BOUNDED_MAX_DEFAULT = 5

WINDOW_CHOICES = TIME_WINDOWS


@dataclass(frozen=True)
class EmailPageDependencies:
    """What the page needs from the outside, so tests can substitute it."""

    mode: str
    adapter_factory: object          # () -> EmailAdapter
    credentials: object              # CredentialStore
    state_store: object              # MailboxStateStore
    label: str


# ------------------------------------------------------------------ demo mail
# The offline fixture lives in email_demo so tests can replace the factory without
# patching the module under test (a factory defined here and patched there is an
# easy way to build an accidental recursion).


def build_dependencies(mode: str, *, state_store=None) -> EmailPageDependencies:
    """Real or demo wiring. The demo path never reaches the network."""
    # The sync state (UID cursor, failed UIDs) must outlive a single script run,
    # or every pass would look like a first pass and "incremental" would never
    # happen. It lives in the session, next to the credentials.
    state_store = state_store or st.session_state.get("email_state_store")
    if state_store is None:
        state_store = MailboxStateStore()
        st.session_state["email_state_store"] = state_store
    if mode == MODE_REAL:
        credentials = st.session_state.get("email_credentials")
        if credentials is None:
            credentials = SessionCredentialStore()
            st.session_state["email_credentials"] = credentials
        return EmailPageDependencies(
            mode=mode,
            adapter_factory=adapter_factory_from(
                credentials, RealImapAdapter, host=IMAP_HOST, port=IMAP_PORT, timeout=30.0
            ),
            credentials=credentials,
            state_store=state_store,
            label=f"{IMAP_HOST}:{IMAP_PORT}（只读）",
        )
    # Demo branch only: an override lets tests (and future previews) supply a
    # different offline mailbox without reaching into module globals, which
    # Streamlit's script re-imports can silently reset. The real branch above
    # never consults it, so a planted value cannot redirect a real connection.
    override = st.session_state.get(DEMO_FACTORY_KEY)
    return EmailPageDependencies(
        mode=mode,
        adapter_factory=override or demo_mailbox,
        credentials=st.session_state.get("email_credentials") or SessionCredentialStore(),
        state_store=state_store,
        label="演示收件箱（虚构邮件）",
    )

# ------------------------------------------------------------------- rendering


def render_email_page(repository, *, mode: str = MODE_DEMO,
                      dependencies: EmailPageDependencies | None = None) -> None:
    st.subheader("学校邮箱")
    st.caption("只读读取你学校邮箱的 INBOX；不发送、不标记已读、不删除、不移动任何邮件。")

    _render_pending_messages()
    mode = st.radio("读取方式", (MODE_DEMO, MODE_REAL), index=0,
                    key="email_mode", horizontal=True,
                    help="演示模式使用离线虚构邮件；真实模式需要用你的校园网账号与客户端授权码。")
    if dependencies is None:
        dependencies = build_dependencies(mode)
    _render_connection(repository, dependencies)
    return dependencies


def _render_pending_messages() -> None:
    for kind, setter in (("email_error", st.error), ("email_info", st.info),
                         ("email_written", st.success)):
        message = st.session_state.pop(kind, None)
        if message:
            setter(message)


def _remember(kind: str, message: str) -> None:
    """Keep a message for the next run: this run's output is about to be dropped."""
    st.session_state[kind] = message


def _service(dependencies: EmailPageDependencies) -> EmailSyncService:
    return EmailSyncService(dependencies.adapter_factory,
                            state_store=dependencies.state_store)


def _last_sync_at() -> datetime | None:
    return st.session_state.get("email_last_sync")


def _current_report() -> EmailSyncReport | None:
    return st.session_state.get("email_report")


def _reset_sync_state(dependencies: EmailPageDependencies) -> None:
    for key in ("email_report", "email_plan", "email_last_sync", "email_error",
                "email_info", "email_written", "email_digest", "email_digest_key",
                "email_generation", "email_plan_signature", "email_rendered_plan"):
        st.session_state.pop(key, None)
    dependencies.state_store.reset(DEFAULT_FOLDER)
    # Drop the state object itself too: after a disconnect nothing about the
    # mailbox should survive in the session.
    st.session_state.pop("email_state_store", None)


def _render_connection(repository, dependencies: EmailPageDependencies) -> None:
    st.markdown("### 邮箱连接")

    if dependencies.mode == MODE_REAL:
        _render_credentials(dependencies)
    else:
        st.info(f"当前为{dependencies.mode}：读取内置的虚构邮件，不会访问任何网络或真实邮箱。")

    _render_status(dependencies)

    columns = st.columns(4)
    if columns[0].button("检查连接", key="email_check"):
        _check_connection(dependencies)
    if columns[1].button("同步邮件", type="primary", key="email_sync"):
        _run_sync(repository, dependencies)
    if columns[2].button("断开连接", key="email_disconnect"):
        _disconnect(dependencies)
    columns[3].caption(f"常规范围：INBOX 最近 {INITIAL_DAYS} 天、最多 {INITIAL_MAX} 封"
                       f"（单批硬上限 {DEFAULT_FETCH_LIMIT} 封）")

    _render_bounded_sync(repository, dependencies)

    with st.expander("这个功能会做什么、不会做什么", expanded=False):
        st.write("- **只读**：只列出文件夹、按 UID 读取邮件内容（`BODY.PEEK`），"
                 "不会标记已读、移动、删除或发送。")
        st.write("- **不保存密码**：默认只在当前程序运行期间保留授权码，退出即失效。")
        st.write("- **不自动重登**：登录过期只会提示你重新输入，不会自动尝试。")
        st.write("- **写库前必经你确认**：同步只生成方案；确认后才会写入本地任务。")
        st.write("- **不会删除任务**：邮件里没出现的本地任务不会被删除、取消或标记完成。")

    report = _current_report()
    if report is None:
        st.info("尚未同步。点击「同步邮件」后，这里会显示分类结果、每日情报与可导入的 Deadline。")
        _render_rules()
        return

    _render_report_summary(report)
    plan = st.session_state.get("email_plan")
    if plan is None:
        try:
            plan = plan_email_import(report, repository)
        except Exception as error:
            st.error(f"本次结果无法生成导入方案：{error}")
            _render_rules()
            return
        st.session_state["email_plan"] = plan
    _render_plan(repository, report, plan)
    _render_rules()


def _render_bounded_sync(repository, dependencies: EmailPageDependencies) -> None:
    """A one-off pass with a scope the user states: hours back, message cap.

    Kept separate from 「同步邮件」 on purpose. The two differ in what they read,
    so they must be separate decisions: this one is for a first look at real
    mail with a scope small enough to state in one sentence. It ignores the UID
    cursor and leaves it untouched, so it cannot change what a later normal sync
    reads, and it still writes nothing to the task database.
    """
    with st.expander("受限试读（指定时间范围与封数，适合首次真实读取）", expanded=False):
        st.caption("这一项**会读取邮件正文**（只读、不标记已读、不发送）。"
                   "它不改动增量游标，也不会创建或修改任何任务。")
        columns = st.columns(3)
        hours = columns[0].number_input("只看最近多少小时", min_value=1, max_value=24 * 30,
                                        value=BOUNDED_HOURS_DEFAULT, step=1,
                                        key="email_bounded_hours")
        cap = columns[1].number_input("最多读取几封", min_value=1,
                                      max_value=DEFAULT_FETCH_LIMIT,
                                      value=BOUNDED_MAX_DEFAULT, step=1,
                                      key="email_bounded_max")
        columns[2].caption(f"文件夹固定为 {DEFAULT_FOLDER}；"
                           f"单批硬上限 {DEFAULT_FETCH_LIMIT} 封。")
        acknowledged = st.checkbox(
            f"我同意本次只读读取 {DEFAULT_FOLDER} 最近 {int(hours)} 小时、最多 {int(cap)} 封邮件的正文",
            key="email_bounded_ack",
        )
        st.button("开始受限试读", disabled=not acknowledged, key="email_bounded_run",
                  on_click=_run_sync, args=(repository, dependencies),
                  kwargs={"window_hours": float(hours), "max_messages": int(cap)})


def _render_credentials(dependencies: EmailPageDependencies) -> None:
    with st.form("email_credentials_form", clear_on_submit=True):
        st.caption("用于只读 IMAP 的是**客户端授权码**，不是网页登录密码；"
                   "授权码只在内存中，不会写入日志、数据库或交接文档。")
        address = st.text_input("校园网邮箱地址", key="email_address",
                                placeholder="例如 pkuid@stu.pku.edu.cn")
        code = st.text_input("客户端授权码", key="email_code", type="password")
        acknowledged = st.checkbox("我了解：本程序只会只读读取 INBOX，不会发送、删除或标记邮件",
                                   key="email_ack")
        submitted = st.form_submit_button("保存到本次运行（仅内存）")
    if submitted:
        if not acknowledged:
            st.warning("请先确认上面的说明，再保存凭据。")
            return
        if not address.strip() or not code:
            st.error("邮箱地址和客户端授权码都不能为空。")
            return
        dependencies.credentials.save(
            MailCredentials(address=address.strip(), authorization_code=code))
        _remember("email_info", "凭据已保存到本次运行的内存中；程序退出后即失效。")


def _render_status(dependencies: EmailPageDependencies) -> None:
    report = _current_report()
    connected = bool(st.session_state.get("email_connected"))
    credentials = dependencies.credentials.get()
    if dependencies.mode == MODE_REAL:
        state = "已保存凭据（内存）" if credentials is not None else "未提供凭据"
    else:
        state = "演示收件箱"
    columns = st.columns(4)
    columns[0].metric("连接状态", "已连接" if connected else "未连接")
    columns[1].metric("凭据状态", state)
    last = _last_sync_at()
    columns[2].metric("上次同步", format_deadline(last).replace("北京时间 ", "") if last else "尚未同步")
    columns[3].metric("本次邮件数", str(len(report.analyses)) if report else "—")
    if report is not None:
        detail = [f"模式 {report.mode}", f"UIDVALIDITY {report.uidvalidity}",
                  f"最高 UID {report.highest_uid}"]
        if report.has_more:
            detail.append("服务器还有更多邮件（本次未全部读取，可再次同步继续拉取）")
        if report.retried_uids:
            detail.append(f"本次重试成功 {len(report.retried_uids)} 封")
        if report.still_failed_uids:
            detail.append(f"仍解析失败 {len(report.still_failed_uids)} 封：{list(report.still_failed_uids)}")
        st.caption(" ｜ ".join(detail))
        for warning in report.warnings:
            st.warning(warning)
        for rejection in report.rejections:
            st.error(f"无法解析：{rejection}")
        if not report.trustworthy:
            st.error("本次结果未通过完整检查：不会生成导入方案，也不会改动任何本地任务。")


def _check_connection(dependencies: EmailPageDependencies) -> None:
    adapter = None
    try:
        adapter = dependencies.adapter_factory()
        folders = adapter.list_folders()
    except EmailAuthRequired as error:
        st.session_state["email_connected"] = False
        _remember("email_error", f"登录被拒绝：{error} 请核对授权码是否有效、IMAP 是否已开启。")
        return
    except EmailNetworkError as error:
        st.session_state["email_connected"] = False
        _remember("email_error",
                  f"网络或服务端问题：{error} 演示模式下可以改用「演示模式」先在本地试用。")
        return
    except EmailLayoutChanged as error:
        st.session_state["email_connected"] = False
        _remember("email_error", f"服务器返回了无法理解的内容：{error}")
        return
    except EmailAdapterError as error:
        st.session_state["email_connected"] = False
        _remember("email_error", f"无法连接：{error}")
        return
    finally:
        if adapter is not None:
            adapter.close()
    st.session_state["email_connected"] = True
    _remember("email_info", f"连接正常，可读取的文件夹：{'、'.join(folders) or '（无）'}。")


def _run_sync(repository, dependencies: EmailPageDependencies, *,
              window_hours: float | None = None,
              max_messages: int | None = None) -> None:
    st.session_state.pop("email_error", None)
    if dependencies.mode == MODE_REAL and dependencies.credentials.get() is None:
        _remember("email_error", "请先在上方保存邮箱地址与客户端授权码（仅保存在本次运行内存中）。")
        return
    service = _service(dependencies)
    scope = (f"最近 {int(window_hours)} 小时、最多 {max_messages} 封"
             if window_hours else "按常规范围")
    try:
        with st.spinner(f"正在只读读取邮件（{scope}）…"):
            report = service.sync(window_hours=window_hours, max_messages=max_messages)
    except EmailAuthRequired as error:
        _remember("email_error", f"登录被拒绝：{error} 请核对授权码，或确认邮箱已开启 IMAP。")
        return
    except EmailNetworkError as error:
        _remember("email_error", f"网络或服务端问题：{error} 未改动任何本地任务。")
        return
    except EmailLayoutChanged as error:
        _remember("email_error", f"服务器返回了无法理解的内容：{error} 未改动任何本地任务。")
        return
    except EmailAdapterError as error:
        _remember("email_error", f"同步失败：{error} 未改动任何本地任务。")
        return
    except OSError as error:
        _remember("email_error", f"读取邮件时出错（{error.__class__.__name__}），未改动任何本地任务。")
        return

    st.session_state["email_connected"] = True
    st.session_state["email_report"] = report
    st.session_state["email_last_sync"] = beijing_now()
    st.session_state.pop("email_plan", None)
    st.session_state.pop("email_digest", None)
    _remember("email_info",
              f"同步完成：{report.describe_scope}。分类与每日情报见下方。"
              "数据库尚未写入，也没有创建或修改任何任务；请核对后自行决定是否导入。")


def _disconnect(dependencies: EmailPageDependencies) -> None:
    dependencies.credentials.clear()
    _reset_sync_state(dependencies)
    st.session_state["email_connected"] = False
    st.session_state.pop("email_feedback", None)
    _remember("email_info", "已断开：凭据、同步结果与预览都已从本次运行中清除；本地任务未受影响。")


def _render_report_summary(report: EmailSyncReport) -> None:
    st.divider()
    digest = st.session_state.get("email_digest")
    if digest is None or st.session_state.get("email_digest_key") != id(report):
        digest = build_daily_digest(report.analyses)
        st.session_state["email_digest"] = digest
        st.session_state["email_digest_key"] = id(report)
    st.markdown("### 校园情报")
    st.write(digest.summary_line())
    _render_board(report, digest)
    st.divider()
    _render_feedback(report)
    st.divider()
    _render_ai_gate(report)


def _render_board(report: EmailSyncReport, digest) -> None:
    st.caption("每封邮件都带分类理由与相关日期；低重要程度只是**排在后面**，不会消失。")
    columns = st.columns(4)
    category = columns[0].selectbox("分类", (ALL_CATEGORIES, *EMAIL_CATEGORIES),
                                    key="email_filter_category")
    importance = columns[1].selectbox("重要程度", (ALL_IMPORTANCE, *IMPORTANCE_LEVELS),
                                      key="email_filter_importance")
    window = columns[2].selectbox("时间范围", WINDOW_CHOICES,
                                  index=WINDOW_CHOICES.index(ALL_TIME),
                                  key="email_filter_window")
    search = columns[3].text_input("搜索主题或发件人", key="email_filter_search")
    filters = BoardFilters(category=category, importance=importance,
                           window=window, search=search)
    view = filter_digest(digest, filters)

    if view.hidden:
        st.caption(f"当前筛选隐藏了 {view.hidden} / {view.total_before} 封；把筛选放宽即可看到。")
    if view.all_hidden:
        st.info("当前筛选下没有邮件。这不代表邮箱里没有邮件：请放宽筛选条件。")
        return

    overrides = _feedback_overrides()
    ordered = sorted(view.entries, key=_entry_sort_key)
    st.markdown(f"**邮件（{len(ordered)}）**")
    for entry in ordered:
        override = overrides.get(entry.stable_key, {})
        shown_category = override.get("category", entry.category)
        shown_importance = override.get("importance", entry.importance)
        with st.expander(f"【{shown_category}·{shown_importance}】{entry.subject}",
                         expanded=shown_importance == IMPORTANCE_HIGH):
            st.write(f"发件人：{entry.sender}")
            st.write(f"时间：{format_deadline(entry.sent_at) if entry.sent_at else '（邮件未提供日期）'}")
            if override:
                st.caption(f"你已修正为：分类 {shown_category}、重要程度 {shown_importance}")
            st.write(f"分类理由：{entry.reasons[0] if entry.reasons else entry.note or '（无）'}")
            for reason in entry.reasons[1:]:
                st.caption(f"· {reason}")
            if entry.note:
                st.warning(entry.note)
            related = [item for item in view.deadlines if item.stable_key == entry.stable_key]
            if related:
                st.markdown("**相关时间**")
                for item in related:
                    when = format_deadline(item.due_at) if item.due_at else "待人工确认"
                    flag = "（需确认）" if item.needs_confirmation else ""
                    st.write(f"- {item.kind}：{when}{flag}")

    if view.deadlines:
        st.markdown("**全部时间点（按时间排序）**")
        for item in view.deadlines:
            when = format_deadline(item.due_at) if item.due_at else "待人工确认"
            flag = "（需确认）" if item.needs_confirmation else ""
            st.write(f"- {item.kind}：{when}{flag} ｜ 来源：{item.subject}")


def _entry_sort_key(entry):
    from pku_deadline_agent.time_utils import beijing_now

    return (entry.sent_at is None, entry.sent_at or beijing_now(), entry.subject)


def _render_plan(repository, report: EmailSyncReport, plan) -> None:
    st.divider()
    st.markdown("### Deadline 导入")
    st.write(plan.describe())
    if plan.warnings:
        for warning in plan.warnings:
            st.warning(warning)
    if plan.skipped:
        st.error("以下邮件无法完整读取，已记录并跳过（可稍后重试），其余邮件不受影响：")
        for item in plan.skipped:
            st.write(f"- UID {item.uid}：{item.subject} ｜ {'；'.join(item.reasons)}")

    context = _context_table(plan)
    if not plan.proposals:
        st.info("本次没有可导入的 Deadline。活动举办时间与待确认时间只展示、不建任务。")
    else:
        st.markdown("**逐条核对**（取消勾选可跳过某一条）")
        generation = _generation_for(plan)
        decisions = []
        for index, proposal in enumerate(plan.proposals):
            info = context.get(proposal.source_key)
            label = _proposal_label(proposal, info)
            if proposal.kind == UNCHANGED:
                st.checkbox(f"〔无需改动〕{label}", value=False, disabled=True,
                            key=f"email_pick_{generation}_{index}")
                decisions.append(ImportDecision("skip", target_id=proposal.task_id,
                                                reason="已是最新，无需改动"))
                continue
            checked = st.checkbox(f"〔{KIND_LABEL[proposal.kind]}〕{label}",
                                  value=proposal.kind == NEW,
                                  key=f"email_pick_{generation}_{index}")
            if info is not None:
                st.caption(f"分类 {info.category}（{info.reason}）；日期依据「{info.raw_text}」"
                           f"；发件人 {info.sender}")
                if info.snippet:
                    st.caption(f"原文片段：{info.snippet}")
                if info.needs_confirmation:
                    st.warning("该日期的语义不明确，导入后会保留为「待确认」，请人工填写时刻。")
            if not checked:
                decisions.append(ImportDecision("skip", target_id=proposal.task_id,
                                                reason="本次未选择"))
                continue
            if proposal.kind == DEADLINE_CHANGED:
                reason = st.text_input(
                    "修改原因（必填，1–500 字）", key=f"email_reason_{generation}_{index}")
                st.caption(f"将把 #{proposal.task_id} 从 {format_deadline(proposal.current_deadline)}"
                           f" 改为 {format_deadline(proposal.record.deadline)}")
                decisions.append(ImportDecision("update", target_id=proposal.task_id,
                                                expected_revision=proposal.expected_revision,
                                                reason=reason or ""))
            elif proposal.kind == POSSIBLE_DUPLICATE:
                st.caption("邮件与本地任务可能重复；导入会**新建独立任务**，不会覆盖已有任务。")
                decisions.append(ImportDecision("new", allow_distinct=True,
                                                reason="邮件与本地任务可能重复，经核对后作为独立任务新增"))
            else:
                decisions.append(ImportDecision("new", allow_distinct=True,
                                                reason="经页面核对后从学校邮箱导入"))
        confirmed = st.checkbox("我已核对以上内容，确认按所选写入本地任务库",
                                key=f"email_confirm_{generation}")
        if st.button("执行导入", type="primary", disabled=not confirmed,
                     key=f"email_apply_{generation}"):
            _apply_plan(repository, plan, decisions)

    if plan.event_only:
        st.markdown("**活动举办时间（仅展示，不自动创建 Deadline）**")
        for item in plan.event_only:
            st.write(f"- {item.kind}：{format_deadline(item.source.deadline)} ｜ {item.subject}"
                     f" ｜ {item.raw_text}")
        st.caption("如需把活动时间变成任务，请用「手动待办」页自行添加，或在上方勾选同类报名截止。")

    if plan.undated:
        st.markdown("**待确认时间（缺少可用日期，需人工填写）**")
        for item in plan.undated:
            st.write(f"- {item.kind}：{item.raw_text or '（未给出明确表达）'} ｜ 来源：{item.subject}"
                     f" ｜ UID {item.uid}")
        st.caption("这些时间没有可用的完整日期（例如缺少年份或只有小时刻），"
                   "程序不会猜测年份，请到「手动待办」页按原文填写。")


KIND_LABEL = {
    NEW: "新 Deadline",
    UNCHANGED: "已有且无变化",
    DEADLINE_CHANGED: "截止时间变化",
    POSSIBLE_DUPLICATE: "可能重复（未关联的来源标识）",
}


def _proposal_label(proposal, info) -> str:
    when = format_deadline(proposal.record.deadline)
    title = proposal.record.task_title
    course = f" · {proposal.record.course}" if proposal.record.course else ""
    return f"{title}{course} · {when}"


def _context_table(plan) -> dict:
    return {item.source.source_id: item for item in plan.records}


def _generation_for(plan) -> int:
    """A fresh widget-key generation per distinct plan, assigned while rendering.

    Keyed on the plan's *content*, not its identity: a re-run rebuilds the plan
    object (and leaving the page drops it), so keying on identity would bump the
    generation on every interaction and change the widget keys under the user's
    hands. The signature still changes whenever the proposals do, which is exactly
    when an earlier confirmation must stop applying.
    """
    signature = tuple(
        (proposal.kind, proposal.source_key, proposal.task_id, proposal.record.deadline)
        for proposal in plan.proposals
    )
    if st.session_state.get("email_plan_signature") != signature:
        st.session_state["email_generation"] = st.session_state.get("email_generation", 0) + 1
        st.session_state["email_plan_signature"] = signature
    return st.session_state.get("email_generation", 0)


def _apply_plan(repository, plan, decisions) -> None:
    if not any(decision.action != "skip" for decision in decisions):
        st.warning("没有选中任何条目，未写入数据库。")
        return
    try:
        created = apply_email_plan(repository, plan, tuple(decisions), confirmed=True)
    except ValueError as error:
        st.error(str(error))
        return
    except Exception as error:  # sqlite3.Error / OSError from the storage layer
        st.error(f"写入失败（{error.__class__.__name__}），请检查数据库权限或可用空间。")
        return
    st.session_state.pop("email_plan", None)
    st.session_state.pop("email_plan_signature", None)
    _remember("email_written",
              f"已写入 {len(created)} 条；未选中的条目未改动，已有任务未被删除。")


# ------------------------------------------------------------------- feedback


def _feedback_store() -> EmailFeedbackStore:
    store = st.session_state.get("email_feedback")
    if store is None:
        store = EmailFeedbackStore()
        st.session_state["email_feedback"] = store
    return store


def _feedback_overrides() -> dict[str, dict]:
    """User corrections of category/importance, by message key."""
    store = st.session_state.get("email_feedback")
    if store is None:
        return {}
    overrides: dict[str, dict] = {}
    for item in store.all():
        entry = overrides.setdefault(item.stable_key, {})
        if item.corrected_category:
            entry["category"] = item.corrected_category
        if item.corrected_importance:
            entry["importance"] = item.corrected_importance
    return overrides


def _render_feedback(report: EmailSyncReport) -> None:
    st.markdown("### 反馈与修正")
    st.caption("你的修正只保存在本次运行中，并立即用于重新显示看板；不会改动邮件本身。")
    options = {f"{a.record.subject}（当前 {a.category}·{a.importance}）": a
               for a in report.analyses}
    if not options:
        st.info("本次没有可反馈的邮件。")
        return
    chosen = st.selectbox("选择要修正的邮件", tuple(options), key="email_feedback_target")
    analysis = options[chosen]
    columns = st.columns(2)
    category = columns[0].selectbox("分类修正为", (analysis.category, *EMAIL_CATEGORIES),
                                    key="email_feedback_category")
    importance = columns[1].selectbox("重要程度修正为", (analysis.importance, *IMPORTANCE_LEVELS),
                                      key="email_feedback_importance")
    note = st.text_input("补充说明（选填）", key="email_feedback_note")
    if st.button("保存本次修正", key="email_feedback_save"):
        if category == analysis.category and importance == analysis.importance:
            st.warning("分类与重要程度都没有变化，无需记录。")
            return
        store = _feedback_store()
        store.record(ClassificationFeedback(
            stable_key=analysis.record.stable_key,
            subject=analysis.record.subject,
            predicted_category=analysis.category,
            corrected_category=category,
            corrected_importance=importance,
            note=note,
        ))
        st.success(f"已记录修正：{analysis.record.subject} → {category}·{importance}")
        st.rerun()
    entries = _feedback_store().all()
    if entries:
        st.caption(f"本次运行已记录 {len(entries)} 条修正。")


# --------------------------------------------------------------------- AI gate


def _render_ai_gate(report: EmailSyncReport) -> None:
    st.markdown("### AI 辅助（可选，默认不发送）")
    st.caption("本地规则先给出结论；只有在你看过完整预览并逐次确认后，才会考虑让模型参与。"
               "本页目前**只准备载荷、不发送**：真正的调用要走「Deadline 智能解析」页的"
               "同一个网关（含费用与逐次授权）。")
    try:
        payload = build_ai_payload(report.analyses, confirmed=True)
    except Exception as error:
        st.error(f"无法准备 AI 载荷：{error}")
        return
    st.write(f"可参与的最小载荷：{len(payload.items)} / {len(report.analyses)} 封"
             f"（其余因含敏感内容被整体剔除）。")
    st.caption("载荷只包含：主题、发件域名、本地规则结论、已提取的时间片段。"
               "**正文与附件不在其中**，也不会上传邮件全文或认证凭据。")
    if payload.items:
        with st.expander("查看将发送的最小载荷（逐字段）", expanded=False):
            for item in payload.items:
                st.write(f"- 主题：{item.subject}")
                st.write(f"  发件域名：{item.sender_domain}")
                st.write(f"  本地结论：{item.category}")
                st.write(f"  时间片段：{'；'.join(item.deadlines) or '（无）'}")
                st.write(f"  片段：{item.snippet or '（无）'}")
                if item.privacy_kinds:
                    st.caption(f"  已脱敏类型：{'、'.join(item.privacy_kinds)}")
    confirmed = st.checkbox(
        "我确认上述内容可以交给模型辅助（仍需在解析页对具体请求再次确认）",
        key="email_ai_confirm",
    )
    st.button("准备好载荷（本页不发送）", disabled=not confirmed, key="email_ai_prepare")
    if not confirmed:
        st.info("未确认前，程序不会构建任何对外请求；邮件全文与附件永远不进入载荷。")


def _render_rules() -> None:
    with st.expander("这个页面保证什么", expanded=False):
        st.write("- **只读**：只列出与读取邮件，不标记已读、不删除、不移动、不发送。")
        st.write("- **不猜日期**：缺年份或只有时刻的时间标为「待确认」，需要你人工填写。")
        st.write("- **活动时间不建任务**：活动举办时间只展示；报名 / 作业 / 材料截止才会成为候选任务。")
        st.write("- **写库前必经确认**：不确认就不会写入；写入走与编程网格完全相同的去重与历史路径。")
        st.write("- **不删除**：邮件里没出现的本地任务不会被删除、取消或标记完成。")
        st.write("- **失败可重试**：解析失败的邮件会记录 UID 并保留，下次同步自动重试。")
        st.write("- **凭据只在内存**：授权码默认不落盘，退出即失效；本页也不会把它写进日志。")
