"""Stage 7F Streamlit tests for the AI settings page.

The page is driven through ``app.py``; only the credential backend is replaced,
with the same in-memory fake used by the storage tests. Every key is fictional.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.api_key_store import ENV_NAMES, ApiKeyStore
from tests.test_stage7f_keys import FAKE_DEEPSEEK, FAKE_MIMO, FakeKeyring

APP = Path(__file__).resolve().parents[1] / "app.py"
PAGE = "AI 设置"
STORE_KEY = "ai_key_store"


@pytest.fixture
def backend(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake)
    return fake


def open_page(backend, monkeypatch, *, store=None):
    app = AppTest.from_file(APP)
    app.session_state["page"] = PAGE
    # Reads the real environment, which is what the app does; tests that need a
    # specific variable set it with monkeypatch.setenv.
    app.session_state[STORE_KEY] = store or ApiKeyStore(backend=backend)
    return app.run()


def visible_text(app) -> str:
    """Everything the page *renders*, so a leak anywhere is caught.

    The password field's own value is deliberately excluded: the user typed it, so
    echoing it back into the widget is not a disclosure. Whether the field is
    cleared after saving is asserted separately.
    """
    parts: list[str] = []
    for name in ("markdown", "caption", "info", "error", "warning", "success",
                 "code", "header", "subheader", "title", "text"):
        try:
            items = getattr(app, name)
        except Exception:  # noqa: BLE001 - a missing element type is fine
            continue
        for item in items:
            parts.append(str(getattr(item, "value", "")))
            parts.append(str(getattr(item, "label", "")))
    for item in app.metric:
        parts.extend([str(item.label), str(item.value)])
    for item in app.text_input:
        parts.append(str(item.label))
    for item in app.selectbox:
        parts.append(str(item.label))
    for item in app.checkbox:
        parts.append(str(item.label))
    for item in app.button:
        parts.append(str(item.label))
    for item in app.expander:
        parts.append(str(item.label))
    return "\n".join(parts)


def save_key(app, key, *, provider=None, acknowledge=True):
    if provider is not None:
        app = app.selectbox(key="ai_key_provider").set_value(provider).run()
    app = app.text_input(key="ai_key_input").set_value(key).run()
    if acknowledge:
        app = app.checkbox(key="ai_key_ack").set_value(True).run()
    return _click(app, "ai_key_save")


def remove_key(app, *, provider=None, confirm=True):
    if provider is not None:
        app = app.selectbox(key="ai_key_provider").set_value(provider).run()
    if confirm:
        app = app.checkbox(key="ai_key_delete_ack").set_value(True).run()
    return _click(app, "ai_key_delete")


def _click(app, key):
    buttons = [item for item in app.button if item.key == key]
    assert buttons, f"button {key!r} not found: {[item.key for item in app.button]}"
    return buttons[0].click().run()


# --------------------------------------------------------------------- shell


def test_sidebar_offers_the_ai_settings_page(backend, monkeypatch):
    app = open_page(backend, monkeypatch)
    # The sidebar labels carry an icon prefix now, so match on the name inside.
    assert any("AI 设置" in str(option) for option in app.sidebar.radio[0].options)
    assert not app.exception, app.exception


def test_page_starts_with_both_providers_unconfigured(backend, monkeypatch):
    app = open_page(backend, monkeypatch)
    metrics = {item.label: item.value for item in app.metric}
    assert set(metrics) == {"DeepSeek", "MiMo"}
    assert all(value == "未配置" for value in metrics.values())


def test_page_shows_configured_state_without_the_key(backend, monkeypatch):
    store = ApiKeyStore(backend=backend)
    store.save("DeepSeek", FAKE_DEEPSEEK)
    app = open_page(backend, monkeypatch, store=store)

    metrics = {item.label: item.value for item in app.metric}
    assert metrics["DeepSeek"] == "已配置（Windows 凭据管理器）"
    assert metrics["MiMo"] == "未配置"
    assert FAKE_DEEPSEEK not in visible_text(app)
    assert FAKE_DEEPSEEK not in repr(app.session_state.get(STORE_KEY).status("DeepSeek"))


# -------------------------------------------------------------------- saving


def test_saving_a_key_stores_it_and_never_renders_it(backend, monkeypatch):
    app = open_page(backend, monkeypatch)
    app = save_key(app, FAKE_DEEPSEEK, provider="DeepSeek")
    assert not app.exception, app.exception
    # It really went to the credential backend...
    assert backend.entries[("PKU Deadline Agent", "DeepSeek")] == FAKE_DEEPSEEK
    # ...and the page neither echoes it nor shows it anywhere.
    assert FAKE_DEEPSEEK not in visible_text(app)
    app = app.run()
    assert any("已保存到 Windows 凭据管理器" in item.value for item in app.success)
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["DeepSeek"] == "已配置（Windows 凭据管理器）"
    assert FAKE_DEEPSEEK not in visible_text(app)
    # The pasted key does not linger in the input after a successful save.
    assert app.text_input(key="ai_key_input").value in ("", None)


def test_the_two_providers_are_saved_separately_from_the_page(backend, monkeypatch):
    app = open_page(backend, monkeypatch)
    app = save_key(app, FAKE_DEEPSEEK, provider="DeepSeek")
    app = save_key(app, FAKE_MIMO, provider="MiMo")
    assert backend.entries[("PKU Deadline Agent", "DeepSeek")] == FAKE_DEEPSEEK
    assert backend.entries[("PKU Deadline Agent", "MiMo")] == FAKE_MIMO
    app = app.run()
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["DeepSeek"].startswith("已配置")
    assert metrics["MiMo"].startswith("已配置")


def test_saving_without_the_acknowledgement_writes_nothing(backend, monkeypatch):
    app = open_page(backend, monkeypatch)
    app = save_key(app, FAKE_DEEPSEEK, provider="DeepSeek", acknowledge=False)
    assert not app.exception, app.exception
    assert backend.entries == {}
    app = app.run()
    assert any("请先确认" in item.value for item in app.warning)
    assert FAKE_DEEPSEEK not in visible_text(app)


def test_an_invalid_key_is_refused_without_echoing_it(backend, monkeypatch):
    app = open_page(backend, monkeypatch)
    app = save_key(app, FAKE_DEEPSEEK + " ", provider="DeepSeek")
    assert not app.exception, app.exception
    assert backend.entries == {}
    app = app.run()
    errors = "\n".join(item.value for item in app.error)
    assert "空格" in errors or "换行" in errors
    assert FAKE_DEEPSEEK not in errors


# ------------------------------------------------------------------ deleting


def test_deleting_a_saved_key_removes_only_that_one(backend, monkeypatch):
    store = ApiKeyStore(backend=backend)
    store.save("DeepSeek", FAKE_DEEPSEEK)
    store.save("MiMo", FAKE_MIMO)
    app = open_page(backend, monkeypatch, store=store)
    app = remove_key(app, provider="DeepSeek")
    assert not app.exception, app.exception
    assert ("PKU Deadline Agent", "DeepSeek") not in backend.entries
    assert backend.entries[("PKU Deadline Agent", "MiMo")] == FAKE_MIMO
    app = app.run()
    assert any("已删除" in item.value for item in app.success)


def test_deleting_needs_an_explicit_confirmation(backend, monkeypatch):
    store = ApiKeyStore(backend=backend)
    store.save("DeepSeek", FAKE_DEEPSEEK)
    app = open_page(backend, monkeypatch, store=store)
    buttons = [item for item in app.button if item.key == "ai_key_delete"]
    assert buttons and buttons[0].disabled is True
    assert backend.entries[("PKU Deadline Agent", "DeepSeek")] == FAKE_DEEPSEEK


def test_deleting_when_nothing_is_stored_says_so(backend, monkeypatch):
    app = open_page(backend, monkeypatch)
    app = remove_key(app)
    assert not app.exception, app.exception
    app = app.run()
    assert any("没有已保存的 Key" in item.value for item in app.warning)


# ------------------------------------------------------- environment priority


def test_an_environment_key_is_shown_as_such_and_is_not_exposed(backend, monkeypatch):
    monkeypatch.setenv(ENV_NAMES["DeepSeek"], "sk-fictional-from-environment")
    app = open_page(backend, monkeypatch)
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["DeepSeek"] == "已配置（环境变量）"
    text = visible_text(app)
    assert "sk-fictional-from-environment" not in text
    assert "来自环境变量" in text
    assert ENV_NAMES["DeepSeek"] in text


def test_deleting_a_stored_key_warns_that_the_environment_still_applies(backend, monkeypatch):
    monkeypatch.setenv(ENV_NAMES["DeepSeek"], "sk-fictional-from-environment")
    store = ApiKeyStore(backend=backend)
    store.save("DeepSeek", FAKE_DEEPSEEK)
    app = open_page(backend, monkeypatch, store=store)
    app = remove_key(app, provider="DeepSeek")
    app = app.run()
    warnings = "\n".join(item.value for item in app.warning)
    assert ENV_NAMES["DeepSeek"] in warnings
    assert "sk-fictional-from-environment" not in visible_text(app)
    # The environment value is still what the app would use.
    assert store.status("DeepSeek").source == "环境变量"


# ------------------------------------------------------- keyring unavailable


def test_without_keyring_the_page_explains_the_install_command(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "keyring", None)
    app = AppTest.from_file(APP)
    app.session_state["page"] = PAGE
    app.session_state[STORE_KEY] = ApiKeyStore(environ={})
    app = app.run()
    assert not app.exception, app.exception
    text = visible_text(app)
    assert "无法保存或删除" in text
    assert "secrets" in text


# ------------------------------------------------------------------ restart


def test_a_saved_key_is_read_back_in_a_new_session(backend, monkeypatch):
    """Requirement 7: safe to read after the application restarts."""
    first = open_page(backend, monkeypatch)
    first = save_key(first, FAKE_DEEPSEEK, provider="DeepSeek")
    assert backend.entries[("PKU Deadline Agent", "DeepSeek")] == FAKE_DEEPSEEK

    # A brand-new session, with no shared session state, as if the app restarted.
    second = AppTest.from_file(APP)
    second.session_state["page"] = PAGE
    second.session_state[STORE_KEY] = ApiKeyStore(backend=backend)
    second = second.run()

    metrics = {item.label: item.value for item in second.metric}
    assert metrics["DeepSeek"] == "已配置（Windows 凭据管理器）"
    assert FAKE_DEEPSEEK not in visible_text(second)
