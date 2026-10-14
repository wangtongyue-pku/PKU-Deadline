"""Real mailbox UI, secure persistence and in-page AI through ParseSession.

Background sync updates only the encrypted mailbox cache. Task writes require a
save click; model requests require their displayed privacy preview approval.
Offline fixtures are injected by tests/preview scripts, never a product option.
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
from pku_deadline_agent.email_credentials import MailCredentials, SessionCredentialStore, WindowsCredentialStore
from pku_deadline_agent.mailbox_cache import MailboxCache, account_key, sync_mailbox
from pku_deadline_agent.email_schedule import cache_path, WindowsMailSchedule
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
    cache: object = None


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
    # Internal injection for offline tests/preview scripts, never a user option.
    override = st.session_state.get(DEMO_FACTORY_KEY)
    if override is not None:
        return EmailPageDependencies(MODE_DEMO, override, st.session_state.get("email_credentials") or SessionCredentialStore(), state_store, "虚构测试邮箱")
    if mode == MODE_REAL:
        credentials = st.session_state.get("email_credentials")
        if credentials is None:
            credentials = WindowsCredentialStore(user_confirmed_persistence=True)
            st.session_state["email_credentials"] = credentials
        return EmailPageDependencies(
            mode=mode,
            adapter_factory=adapter_factory_from(
                credentials, RealImapAdapter, host=IMAP_HOST, port=IMAP_PORT, timeout=30.0
            ),
            credentials=credentials,
            state_store=state_store,
            label=f"{IMAP_HOST}:{IMAP_PORT}（只读）",
            cache=MailboxCache(cache_path()),
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


@st.fragment(run_every="60s")
def render_email_page(repository, *, mode: str = MODE_REAL,
                      dependencies: EmailPageDependencies | None = None) -> None:
    st.subheader("学校邮箱")
    st.caption("只读读取你学校邮箱的 INBOX；不发送、不标记已读、不删除、不移动任何邮件。")

    _render_pending_messages()
    if dependencies is None:
        dependencies = build_dependencies(mode)
    if dependencies.cache is not None:
        credentials = dependencies.credentials.get()
        if credentials:
            account = account_key(credentials.address)
            info = dependencies.cache.info(account)
            cache_version = (account, info["last_success"])
            if st.session_state.get("email_cache_version") != cache_version:
                report, _ = dependencies.cache.load(account)
                st.session_state["email_cache_version"] = cache_version
                st.session_state["email_report"] = report
                st.session_state["email_last_sync"] = datetime.fromisoformat(info["last_success"]) if info["last_success"] else None
                st.session_state.pop("email_plan", None)
                st.session_state.pop("email_digest", None)
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
                "email_generation", "email_plan_signature", "email_rendered_plan",
                "email_ai_session", "email_ai_input", "email_ai_selection_signature",
                "email_ai_selection", "email_cache_version"):
        st.session_state.pop(key, None)
    dependencies.state_store.reset(DEFAULT_FOLDER)
    # Drop the state object itself too: after a disconnect nothing about the
    # mailbox should survive in the session.
    st.session_state.pop("email_state_store", None)


def _render_connection(repository, dependencies: EmailPageDependencies) -> None:
    st.markdown("### 邮箱连接")

    if dependencies.mode == MODE_REAL:
        _render_credentials(dependencies, repository)

    _render_status(dependencies)

    columns = st.columns(4)
    if columns[0].button("检查连接", key="email_check"):
        _check_connection(dependencies)
    if columns[1].button("同步邮件", type="primary", key="email_sync"):
        _run_sync(repository, dependencies)
    if columns[2].button("暂停连接", key="email_disconnect"):
        _disconnect(dependencies)
    columns[3].caption(f"常规范围：INBOX 最近 {INITIAL_DAYS} 天、最多 {INITIAL_MAX} 封"
                       f"（单批硬上限 {DEFAULT_FETCH_LIMIT} 封）")

    if dependencies.cache is not None:
        _render_automation(repository, dependencies)
    _render_bounded_sync(repository, dependencies)

    with st.expander("这个功能会做什么、不会做什么", expanded=False):
        st.write("- **只读**：只列出文件夹、按 UID 读取邮件内容（`BODY.PEEK`），"
                 "不会标记已读、移动、删除或发送。")
        st.write("- **安全保存**：授权码保存在当前 Windows 账户的凭据管理器，不保存网页登录密码。")
        st.write("- **授权失效**：停止自动检查，提示你更新授权码，不反复尝试登录。")
        st.write("- **写库前必经你确认**：同步只生成方案；确认后才会写入本地任务。")
        st.write("- **不会删除任务**：邮件里没出现的本地任务不会被删除、取消或标记完成。")

    report = _current_report()
    if report is None:
        st.info("尚未同步。点击「同步邮件」后，这里会显示分类结果、每日情报与可导入的 Deadline。")
        _render_rules()
        return

    _render_report_summary(report, repository)
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


def _render_credentials(dependencies: EmailPageDependencies, repository) -> None:
    saved = dependencies.credentials.get()
    if st.session_state.pop("email_clear_code", False):
        st.session_state["email_code"] = ""
    with st.expander("邮箱设置" if saved else "首次连接：填写一次即可", expanded=saved is None):
        st.caption("使用客户端授权码，不能填网页登录密码。保存到 Windows 凭据管理器后，重启仍可使用；同一 Windows 账户下的程序可能读取它。")
        address = st.text_input("校园网邮箱地址", value=saved.address if saved else "", key="email_address", placeholder="例如 pkuid@stu.pku.edu.cn")
        code = st.text_input("客户端授权码", key="email_code", type="password", help="已保存的授权码不会回填。更新时再填写。")
        st.caption("保存时同时开启每天自动检查：只读 INBOX，首次最近 7 天最多 30 封；后台不上传 AI。邮件看板以当前 Windows 账户加密缓存，最多 200 封 / 30 天。")
        submitted = st.button("保存邮箱并开启每天检查", key="email_credentials_save")
        forget = st.button("忘记此邮箱", key="email_forget", disabled=saved is None)
        if forget:
            _disconnect(dependencies)
            dependencies.credentials.clear()
            st.session_state["email_clear_code"] = True
            st.rerun()
    if submitted:
        if not address.strip() or not code:
            st.error("邮箱地址和客户端授权码都不能为空。")
            return
        try:
            # Replacing an account must disable its schedule, preserve its cache
            # and revoke every in-memory plan before using the new identity.
            if saved and dependencies.cache:
                dependencies.cache.configure(account_key(saved.address), enabled=False)
            dependencies.credentials.save(MailCredentials(address=address.strip().lower(), authorization_code=code))
        except (ValueError, RuntimeError) as error:
            st.error(str(error))
            return
        _reset_sync_state(dependencies)
        st.session_state["email_clear_code"] = True
        _remember("email_info", "邮箱已保存到 Windows 凭据管理器，重启后无需重填。")
        if dependencies.cache is not None:
            try:
                WindowsMailSchedule(task_path=repository.path).enable()
                dependencies.cache.configure(account_key(address), enabled=True)
            except RuntimeError:
                _remember("email_error", "邮箱凭据已保存，但自动检查未能启用。请在「自动检查邮箱」中重试；仍可手动同步。")
        st.rerun()


def _render_status(dependencies: EmailPageDependencies) -> None:
    report = _current_report()
    connected = bool(st.session_state.get("email_connected"))
    credentials = dependencies.credentials.get()
    if dependencies.mode == MODE_REAL:
        state = "已保存（Windows 凭据管理器）" if credentials is not None else "未提供凭据"
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
                  f"网络或服务端问题：{error} 保留上次结果，请稍后重试。")
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
        _remember("email_error", "请先在上方保存邮箱地址与客户端授权码。")
        return
    service = _service(dependencies)
    scope = (f"最近 {int(window_hours)} 小时、最多 {max_messages} 封"
             if window_hours else "按常规范围")
    try:
        with st.spinner(f"正在只读读取邮件（{scope}）…"):
            credentials = dependencies.credentials.get()
            if dependencies.cache is not None and window_hours is None:
                report = sync_mailbox(dependencies.cache, credentials, dependencies.adapter_factory, force=True)
                if report is None:
                    _remember("email_info", "邮箱正在检查，请稍后刷新。")
                    return
            else:
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
    except RuntimeError as error:
        _remember("email_error", str(error))
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
    credentials = dependencies.credentials.get()
    if credentials and dependencies.cache is not None:
        dependencies.cache.configure(account_key(credentials.address), enabled=False)
    _reset_sync_state(dependencies)
    st.session_state["email_connected"] = False
    st.session_state.pop("email_feedback", None)
    _remember("email_info", "自动检查已暂停，未确认预览已清除；已保存授权码和本地任务保留。要移除授权码请点「忘记此邮箱」。")


def _render_report_summary(report: EmailSyncReport, repository) -> None:
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
    with st.expander("反馈与修正", expanded=False):
        _render_feedback(report)
    st.divider()
    _render_ai_gate(report, repository)


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
                    "修改原因（必填，1–500 字）", value="邮件通知明确修改截止时间", key=f"email_reason_{generation}_{index}")
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
        st.caption("点击保存即确认当前选择；未选中的改期和冲突不会更新。")
        if st.button("保存所选 Deadline", type="primary",
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


def _render_ai_gate(report: EmailSyncReport, repository) -> None:
    from pku_deadline_agent.email_ai import prepare_email_input
    from pku_deadline_agent.parse_controls import render_settings, local_settings, render_preview, render_results
    from pku_deadline_agent.parse_service import ParseSession
    st.markdown("### AI 智能提取邮件")
    enabled = st.toggle("使用 AI 辅助提取", key="email_use_ai")
    if not enabled:
        st.session_state.pop("email_ai_session", None)
        st.session_state.pop("email_ai_input", None)
        return
    st.caption("无需复制到其他页面。先在本地筛选主题和正文中的任务片段，移除联系方式；不上传附件或邮件头。只有点击预览下方的发送按钮才调用模型。")
    settings, provider, model = render_settings("email_ai")
    effective = settings or local_settings(provider, model)
    choices = {f"UID {item.record.uid} · {item.record.subject}": item for item in report.analyses}
    picked = st.multiselect("选择需要智能提取的邮件", tuple(choices), key="email_ai_selection", max_selections=20)
    selected = tuple(choices[label] for label in picked)
    session = st.session_state.get("email_ai_session")
    if not isinstance(session, ParseSession):
        session = st.session_state["email_ai_session"] = ParseSession()
    if not report.trustworthy:
        session.invalidate()
        st.session_state.pop("email_ai_input", None)
        st.warning("邮箱本批读取存在未核对问题，已撤销 AI 预览；先完成只读同步核对。")
        return
    signature = tuple((item.record.stable_key, item.record.subject, item.record.body_text, item.record.sent_at) for item in selected)
    context = st.session_state.get("email_ai_input")
    if st.session_state.get("email_ai_selection_signature") != signature:
        session.invalidate()
        st.session_state.pop("email_ai_input", None)
        st.session_state["email_ai_selection_signature"] = signature
        context = None
    if context:
        session.observe(context.raw, context.reference, "natural", effective)
    if st.button("智能提取所选邮件", key="email_ai_prepare", disabled=not selected or not report.trustworthy):
        try:
            context = prepare_email_input(selected)
            plan = session.prepare(context.raw, context.reference, "natural", effective)
            st.session_state["email_ai_input"] = context
            st.session_state["email_ai_generation"] = st.session_state.get("email_ai_generation", 0) + 1
            if plan.route == "local":
                session.run(context.raw, context.reference, "natural", effective, None, confirmed=True)
        except ValueError as error:
            st.error(str(error))
    if context:
        render_preview(session, effective, context.raw, context.reference, "natural", repository.path, scope="email_ai", live_available=settings is not None)
        def origin(candidate, index):
            ordinal = sum(item.source_index == candidate.source_index for item in session.results[:index + 1])
            return context.origin(candidate, ordinal)
        render_results(session, repository, context.raw, context.reference, "natural", effective, scope="email_ai", origin_builder=origin)


def _render_automation(repository, dependencies):
    credentials = dependencies.credentials.get()
    if credentials is None:
        return
    account = account_key(credentials.address)
    cache = dependencies.cache
    info = cache.info(account)
    with st.expander("自动检查邮箱", expanded=False):
        st.write(f"状态：{'已启用' if info['enabled'] else '已暂停'} · {info['status']}")
        if info["next_due"]:
            st.caption("下次到期（北京）：" + format_deadline(datetime.fromisoformat(info["next_due"])))
        interval = st.selectbox("检查频率", (24, 48), index=0 if info["interval_hours"] == 24 else 1, format_func=lambda hours: "每天一次" if hours == 24 else "每两天一次", key="email_interval")
        st.caption("仅 INBOX：首次最近 7 天最多 30 封，此后按 UID 每次最多 30 封。后台本地分类，不上传 AI、不自动写任务。为重启后显示看板，邮件正文与游标用当前 Windows 账户加密缓存，最多 200 封 / 30 天。")
        st.caption("Windows 每小时检查是否到期，到期才读取；关闭网页仍可运行，关机、休眠或未登录时不能检查，重新登录后下一次检查补一次。授权失效会暂停。")
        if st.button("开启自动检查 / 保存频率", key="email_auto_enable"):
            try:
                WindowsMailSchedule(task_path=repository.path).enable()
                cache.configure(account, enabled=True, interval_hours=interval)
            except RuntimeError as error:
                st.error(str(error))
            else:
                st.success("自动检查已启用。无需保持网页开启，可点「同步邮件」立即更新一次。")
        if st.button("暂停自动检查", key="email_auto_disable"):
            cache.configure(account, enabled=False, interval_hours=interval)
            try:
                WindowsMailSchedule(task_path=repository.path).disable()
            except RuntimeError:
                st.info("本地到期门槛已关闭，任务即使被 Windows 唤起也不会读取邮箱。")
            st.success("已暂停，缓存和任务保留。")


def _render_rules() -> None:
    with st.expander("这个页面保证什么", expanded=False):
        st.write("- **只读**：只列出与读取邮件，不标记已读、不删除、不移动、不发送。")
        st.write("- **不猜日期**：缺年份或只有时刻的时间标为「待确认」，需要你人工填写。")
        st.write("- **活动时间不建任务**：活动举办时间只展示；报名 / 作业 / 材料截止才会成为候选任务。")
        st.write("- **写库前必经确认**：不确认就不会写入；写入走与编程网格完全相同的去重与历史路径。")
        st.write("- **不删除**：邮件里没出现的本地任务不会被删除、取消或标记完成。")
        st.write("- **失败可重试**：解析失败的邮件会记录 UID 并保留，下次同步自动重试。")
        st.write("- **凭据由系统保护**：授权码只保存在 Windows 凭据管理器，不写入日志、配置或数据库；可随时忘记邮箱。")
