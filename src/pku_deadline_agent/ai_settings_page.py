"""Stage 7F: the AI settings page — where an API key is entered and removed.

Two rules shape this page:

* **It never shows a key.** The page can say whether one is configured and where
  it came from, never what it is. The same is true of every message it prints and
  of the module's own ``repr``.
* **It never writes a key anywhere but the Windows credential manager.** No
  config file, no SQLite, no log, no hand-over document.

Environment variables keep priority, exactly as in stage 3, so an existing setup
(machine-scoped variables, a shortcut, a wrapper script) continues to work and
cannot be shadowed by something saved here. A key that comes from the environment
is shown as such and cannot be deleted from this page.
"""

from __future__ import annotations

import streamlit as st

from pku_deadline_agent.ai_config import PROVIDERS
from pku_deadline_agent.api_key_store import (
    ENV_NAMES,
    SOURCE_ENVIRONMENT,
    ApiKeyStore,
    KeyringError,
    KeyringUnavailable,
)


def render_ai_settings_page() -> None:
    st.subheader("AI 设置")
    st.caption("在这里保存、更新或删除 DeepSeek / MiMo 的 API Key。")

    _render_pending_messages()
    store = _store()
    _render_status(store)
    st.divider()
    _render_editor(store)
    _render_notes()


def _store() -> ApiKeyStore:
    """The store for this session; injectable so tests never touch the real store."""
    override = st.session_state.get(API_STORE_KEY)
    if override is not None:
        return override
    store = ApiKeyStore()
    st.session_state[API_STORE_KEY] = store
    return store


API_STORE_KEY = "ai_key_store"
# Set after a successful save; consumed at the start of the next run, because
# Streamlit refuses to write a widget's session key once the widget exists.
CLEAR_INPUT_KEY = "ai_key_clear_input"


def _render_pending_messages() -> None:
    for kind, setter in (("ai_key_error", st.error), ("ai_key_info", st.success),
                         ("ai_key_warning", st.warning)):
        message = st.session_state.pop(kind, None)
        if message:
            setter(message)


def _remember(kind: str, message: str) -> None:
    """Keep a message for the next run: this run's output is about to be dropped."""
    st.session_state[kind] = message


def _render_status(store: ApiKeyStore) -> None:
    storage = store.storage_available()
    if not storage:
        st.warning(
            "安全存储当前不可用，因此本页**无法保存或删除** Key。"
            "环境变量配置不受影响，仍然照常生效。"
        )
        st.code('.venv\\Scripts\\python.exe -m pip install -e ".[secrets]"',
                language="powershell")
        st.caption("上面这条命令安装的是可选依赖 keyring，它使用 Windows 凭据管理器，不会写明文文件。"
                   "装好后回到本页刷新即可。")

    columns = st.columns(len(PROVIDERS))
    for column, provider in zip(columns, PROVIDERS):
        status = store.status(provider)
        column.metric(provider, status.label)
        column.caption(f"环境变量：`{ENV_NAMES[provider]}`")
        if status.configured and status.source == SOURCE_ENVIRONMENT:
            column.caption("来自环境变量，优先级高于本页保存的值；本页无法删除它。")


def _render_editor(store: ApiKeyStore) -> None:
    st.markdown("**保存或更新 Key**")
    st.caption("Key 只写入 Windows 凭据管理器；本页只显示是否已配置，永远不显示完整 Key。")
    provider = st.selectbox("提供方", tuple(PROVIDERS), key="ai_key_provider")
    # Clear the field on the run *after* a successful save. Streamlit forbids
    # writing to a widget's session key once the widget exists, so the request is
    # recorded and honoured at the top of the next run — which is also what keeps
    # the pasted key from lingering on screen.
    if st.session_state.pop(CLEAR_INPUT_KEY, False):
        st.session_state["ai_key_input"] = ""
    # Deliberately not a form: a plain keyed button and a keyed password field
    # behave predictably here.
    key = st.text_input("API Key", type="password", key="ai_key_input",
                        placeholder="粘贴后点下面的按钮保存")
    acknowledged = st.checkbox(
        "我了解：Key 会保存在本机 Windows 凭据管理器，任何能以当前 Windows 账户运行的程序都能读取它",
        key="ai_key_ack",
    )
    columns = st.columns(2)
    save = columns[0].button("保存到 Windows 凭据管理器", key="ai_key_save",
                             disabled=not store.storage_available())
    remove_confirmed = columns[1].checkbox("确认删除该提供方已保存的 Key", key="ai_key_delete_ack")
    remove = columns[1].button("删除已保存的 Key", key="ai_key_delete",
                               disabled=not (remove_confirmed and store.storage_available()))

    if save:
        if not acknowledged:
            _remember("ai_key_warning", "请先确认上面的说明，再保存 Key。")
        else:
            try:
                store.save(provider, key)
            except KeyringUnavailable as error:
                _remember("ai_key_error", str(error))
            except ValueError as error:
                _remember("ai_key_error", str(error))
            except KeyringError as error:
                _remember("ai_key_error", str(error))
            else:
                # The value is never echoed, not even partially, and the field is
                # cleared on the next run so the key does not stay on screen.
                st.session_state[CLEAR_INPUT_KEY] = True
                _remember("ai_key_info", f"{provider} 的 API Key 已保存到 Windows 凭据管理器。")
        # No st.rerun() here: it would end this run immediately and discard the
        # message just queued, which is the very feedback the user needs.

    if remove_confirmed and remove:
        try:
            removed = store.delete(provider)
        except KeyringUnavailable as error:
            _remember("ai_key_error", str(error))
        except KeyringError as error:
            _remember("ai_key_error", str(error))
        else:
            if removed:
                _remember("ai_key_info", f"{provider} 在凭据管理器中保存的 Key 已删除。")
            else:
                _remember("ai_key_warning", f"{provider} 在凭据管理器里没有已保存的 Key，无需删除。")
            if store.environment_key(provider):
                _remember(
                    "ai_key_warning",
                    f"注意：{provider} 仍有环境变量 {ENV_NAMES[provider]}，它仍然生效。",
                )


def _render_notes() -> None:
    with st.expander("Key 的读取顺序与边界", expanded=False):
        st.write("1. **环境变量**优先：`PKU_DEEPSEEK_API_KEY` / `PKU_MIMO_API_KEY`。"
                 "原有配置方式不变，也不会被这里保存的值覆盖。")
        st.write("2. 其次读取 **Windows 凭据管理器**（条目服务名 `PKU Deadline Agent`，"
                 "每个提供方一条，用户名即提供方名称）。")
        st.write("3. 两处都没有时视为未配置；真实调用会被拒绝，并提示配置方法。")
        st.write("**不会发生的事**：Key 不写入 SQLite、配置文件、日志或交接文档；"
                 "页面与错误信息都不回显 Key；删除只作用于凭据管理器，不动环境变量。")
        st.write("**限制**：凭据管理器以当前 Windows 账户加密，任何能以该账户运行的程序都能读取；"
                 "SQLite 数据库本身仍未加密。**模拟模式不会读取任何 Key。**")
        st.write("保存 Key 不等于授权调用模型：真实调用仍需 `PKU_REAL_API_ENABLED=1`，"
                 "并在「Deadline 智能解析」页对每次请求单独确认。")
