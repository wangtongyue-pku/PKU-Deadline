"""Stage 7F offline tests for API-key storage.

Every key here is fictional. The credential manager is a fake in-memory backend
injected through ``sys.modules['keyring']``, so nothing in this file can reach the
real Windows credential store, and no test asserts on a real key.
"""

import sys
from decimal import Decimal

import pytest

from pku_deadline_agent.ai_config import AISettings, load_settings
from pku_deadline_agent.api_key_store import (
    ENV_NAMES,
    SERVICE_NAME,
    SOURCE_ENVIRONMENT,
    SOURCE_KEYRING,
    ApiKeyStore,
    KeyringError,
    KeyringUnavailable,
    validate_key_format,
)

FAKE_DEEPSEEK = "sk-fictional-deepseek-0000"
FAKE_MIMO = "sk-fictional-mimo-1111"


class FakeKeyring:
    """An in-memory credential manager with the keyring surface this app uses."""

    def __init__(self, entries=None):
        self.entries: dict[tuple[str, str], str] = dict(entries or {})
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()

    def get_keyring(self):
        return self

    def get_password(self, service_name, username):
        self.calls.append(("get", service_name, username))
        if "get" in self.fail_on:
            raise RuntimeError("fictional backend failure")
        return self.entries.get((service_name, username))

    def set_password(self, service_name, username, password):
        self.calls.append(("set", service_name, username))
        if "set" in self.fail_on:
            raise RuntimeError("fictional backend failure")
        self.entries[(service_name, username)] = password

    def delete_password(self, service_name, username):
        self.calls.append(("delete", service_name, username))
        if "delete" in self.fail_on:
            raise RuntimeError("fictional backend failure")
        if (service_name, username) not in self.entries:
            raise PasswordDeleteError("no such credential")
        del self.entries[(service_name, username)]


class PasswordDeleteError(Exception):
    """Mirrors keyring.errors.PasswordDeleteError by name, which is what we match."""


@pytest.fixture
def fake_keyring(monkeypatch):
    backend = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", backend)
    return backend


@pytest.fixture
def empty_env():
    return {}


def store(fake_keyring, env=None):
    return ApiKeyStore(backend=fake_keyring, environ=env if env is not None else {})


# ------------------------------------------------------------------- storage


def test_save_then_read_back_from_the_credential_manager(fake_keyring):
    keys = store(fake_keyring)
    assert keys.status("DeepSeek").configured is False

    keys.save("DeepSeek", FAKE_DEEPSEEK)

    assert keys.stored_key("DeepSeek") == FAKE_DEEPSEEK
    status = keys.status("DeepSeek")
    assert status.configured and status.source == SOURCE_KEYRING
    # Stored under a named service and the provider, so it is identifiable in
    # Windows' own credential list.
    assert (SERVICE_NAME, "DeepSeek") in fake_keyring.entries


def test_the_two_providers_are_managed_separately(fake_keyring):
    keys = store(fake_keyring)
    keys.save("DeepSeek", FAKE_DEEPSEEK)
    assert keys.stored_key("MiMo") == ""
    keys.save("MiMo", FAKE_MIMO)
    assert keys.stored_key("DeepSeek") == FAKE_DEEPSEEK
    assert keys.stored_key("MiMo") == FAKE_MIMO
    assert len(fake_keyring.entries) == 2


def test_environment_variable_takes_priority_and_is_not_overwritten(fake_keyring):
    """Requirement 6: an existing environment setup keeps working unchanged."""
    keys = store(fake_keyring, {ENV_NAMES["DeepSeek"]: "sk-fictional-from-env"})
    keys.save("DeepSeek", FAKE_DEEPSEEK)          # the same provider also has a stored key

    key, source = keys.resolve("DeepSeek")
    assert (key, source) == ("sk-fictional-from-env", SOURCE_ENVIRONMENT)
    # Saving did not destroy what is in the credential manager.
    assert keys.stored_key("DeepSeek") == FAKE_DEEPSEEK


def test_delete_removes_only_the_stored_key(fake_keyring):
    keys = store(fake_keyring, {ENV_NAMES["DeepSeek"]: "sk-fictional-from-env"})
    keys.save("DeepSeek", FAKE_DEEPSEEK)

    assert keys.delete("DeepSeek") is True
    assert keys.stored_key("DeepSeek") == ""
    # The environment variable is untouched, and still wins.
    assert keys.resolve("DeepSeek") == ("sk-fictional-from-env", SOURCE_ENVIRONMENT)


def test_deleting_nothing_reports_false_instead_of_raising(fake_keyring):
    keys = store(fake_keyring)
    assert keys.delete("MiMo") is False


def test_save_rejects_an_empty_or_malformed_key_without_storing_it(fake_keyring):
    keys = store(fake_keyring)
    for bad in ("", "   ", "sk with space", "sk\nnewline", "sk-\u4e2d\u6587", "x" * 257):
        with pytest.raises(ValueError):
            keys.save("DeepSeek", bad)
    assert fake_keyring.entries == {}


def test_the_error_message_never_contains_the_key_it_rejected(fake_keyring):
    keys = store(fake_keyring)
    secret = "sk-fictional-secret-should-not-appear"
    for bad in (secret + " ", secret + "\n"):
        with pytest.raises(ValueError) as error:
            keys.save("DeepSeek", bad)
        assert secret not in str(error.value)


def test_a_broken_credential_manager_is_reported_not_crashed(fake_keyring):
    keys = store(fake_keyring)
    fake_keyring.fail_on.add("set")
    with pytest.raises(KeyringError) as error:
        keys.save("DeepSeek", FAKE_DEEPSEEK)
    assert FAKE_DEEPSEEK not in str(error.value)
    # A read failure degrades to "not configured" rather than raising at the page.
    fake_keyring.fail_on.add("get")
    assert keys.stored_key("DeepSeek") == ""


def test_missing_keyring_is_reported_as_unavailable_with_the_install_command(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)  # forces the lazy import to fail
    keys = ApiKeyStore(environ={})
    assert keys.storage_available() is False
    assert keys.stored_key("DeepSeek") == ""
    with pytest.raises(KeyringUnavailable) as error:
        keys.save("DeepSeek", FAKE_DEEPSEEK)
    assert "secrets" in str(error.value)


def test_validate_key_format_is_the_single_place_a_key_is_judged():
    assert validate_key_format("sk-fictional") == "sk-fictional"
    with pytest.raises(ValueError):
        validate_key_format("")
    # Whitespace is reported, not silently trimmed: a pasted newline is a mistake
    # worth telling the user about rather than storing something they did not type.
    for rejected in ("sk-two words", "sk-fictional\n", " sk-fictional", "sk-fictional\t"):
        with pytest.raises(ValueError):
            validate_key_format(rejected)
    assert validate_key_format("sk-" + "a" * 253)          # 256 total is allowed
    with pytest.raises(ValueError):
        validate_key_format("sk-" + "a" * 254)             # 257 is not


# ------------------------------------------------------------------ settings


def test_live_settings_read_a_stored_key_when_the_environment_has_none(fake_keyring, monkeypatch):
    monkeypatch.delenv(ENV_NAMES["DeepSeek"], raising=False)
    keys = store(fake_keyring)
    keys.save("DeepSeek", FAKE_DEEPSEEK)
    monkeypatch.setenv("PKU_REAL_API_ENABLED", "1")

    settings = load_settings("DeepSeek", live=True, key_store=keys)
    assert settings.api_key == FAKE_DEEPSEEK
    assert settings.live and settings.enabled


def test_live_settings_still_prefer_the_environment(fake_keyring, monkeypatch):
    monkeypatch.setenv(ENV_NAMES["DeepSeek"], "sk-fictional-from-env")
    # A store that reads the real environment, which is what the app builds.
    keys = ApiKeyStore(backend=fake_keyring)
    keys.save("DeepSeek", FAKE_DEEPSEEK)
    monkeypatch.setenv("PKU_REAL_API_ENABLED", "1")

    assert load_settings("DeepSeek", live=True, key_store=keys).api_key == "sk-fictional-from-env"


def test_live_settings_reject_a_missing_key_and_say_how_to_configure_it(fake_keyring, monkeypatch):
    monkeypatch.delenv(ENV_NAMES["DeepSeek"], raising=False)
    monkeypatch.setenv("PKU_REAL_API_ENABLED", "1")
    with pytest.raises(ValueError) as error:
        load_settings("DeepSeek", live=True, key_store=store(fake_keyring))
    message = str(error.value)
    assert "AI 设置" in message and ENV_NAMES["DeepSeek"] in message


def test_simulated_mode_never_reads_a_stored_key(fake_keyring, monkeypatch):
    """A simulated run must not touch real configuration, stored or not."""
    monkeypatch.setenv(ENV_NAMES["DeepSeek"], "sk-fictional-from-env")
    keys = store(fake_keyring)
    keys.save("DeepSeek", FAKE_DEEPSEEK)
    fake_keyring.calls.clear()

    settings = load_settings("DeepSeek", live=False, key_store=keys)
    assert settings.api_key == "" and not settings.live
    assert fake_keyring.calls == []          # the credential manager was not queried


def test_disabled_real_mode_never_queries_stored_keys(monkeypatch):
    class NoRead:
        def resolve(self, provider): raise AssertionError("disabled mode read a secret")
    monkeypatch.delenv("PKU_REAL_API_ENABLED", raising=False)
    with pytest.raises(ValueError, match="尚未启用"):
        load_settings("DeepSeek", live=True, key_store=NoRead())


def test_settings_repr_and_fingerprint_never_expose_the_key(fake_keyring):
    settings = AISettings("DeepSeek", "deepseek-flash", True, "https://api.deepseek.com",
                          FAKE_DEEPSEEK, Decimal("2"), Decimal("8"), Decimal("1"))
    assert FAKE_DEEPSEEK not in repr(settings)
    assert FAKE_DEEPSEEK not in str(settings)
    # The fingerprint is a digest: it changes when the key rotates, without
    # containing the key.
    rotated = AISettings("DeepSeek", "deepseek-flash", True, "https://api.deepseek.com",
                         "sk-fictional-rotated", Decimal("2"), Decimal("8"), Decimal("1"))
    assert settings.fingerprint() != rotated.fingerprint()
    assert FAKE_DEEPSEEK not in settings.fingerprint()


def test_key_status_repr_never_contains_a_key(fake_keyring):
    keys = store(fake_keyring)
    keys.save("DeepSeek", FAKE_DEEPSEEK)
    assert FAKE_DEEPSEEK not in repr(keys.status("DeepSeek"))
    assert FAKE_DEEPSEEK not in keys.status("DeepSeek").label


def test_nothing_is_written_to_a_file_or_the_database(fake_keyring, tmp_path, monkeypatch):
    """Requirement 5/2: no plaintext config file, nothing in the task database."""
    import sqlite3

    from pku_deadline_agent.database import TaskRepository

    monkeypatch.chdir(tmp_path)
    keys = store(fake_keyring)
    keys.save("DeepSeek", FAKE_DEEPSEEK)

    repository = TaskRepository(tmp_path / "tasks.sqlite3")
    repository.initialize()
    repository.add_task("虚构任务")

    found = []
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.suffix in {".sqlite3", ".json", ".toml", ".ini", ".txt", ".env"}:
            try:
                if FAKE_DEEPSEEK.encode() in path.read_bytes():
                    found.append(path.name)
            except OSError:
                continue
    assert found == []
    with sqlite3.connect(repository.path) as connection:
        for (table,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"):
            assert FAKE_DEEPSEEK not in str(
                connection.execute(f"SELECT * FROM {table}").fetchall())
