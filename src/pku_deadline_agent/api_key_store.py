"""Stage 7F: where an API key comes from, and where it may never be written.

Resolution order, highest priority first:

1. **Environment variable** (``PKU_DEEPSEEK_API_KEY`` / ``PKU_MIMO_API_KEY``).
   Unchanged from stage 3, so an existing setup keeps working exactly as before
   and a key supplied that way always wins.
2. **Windows Credential Manager**, via ``keyring``.

Nothing here ever writes a key to SQLite, a config file, Git, a log or this
project's hand-over documents. A stored key is read on demand and returned; the
public helpers below reveal only *whether* a key exists.

``keyring`` is a **mature, optional** dependency. It is deliberately not required:
without it the module simply reports that secure storage is unavailable and the
page offers the exact install command, so the application keeps working with
environment variables alone.

The keyring backend is injectable so the whole feature can be tested offline
against a fake in-memory credential manager. Tests never touch the real Windows
credential store and never use a real key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# One credential entry per provider. These names are user-facing, so the stored
# entry is identifiable in Windows' own credential list.
SERVICE_NAME = "PKU Deadline Agent"

ENV_NAMES = {
    "DeepSeek": "PKU_DEEPSEEK_API_KEY",
    "MiMo": "PKU_MIMO_API_KEY",
}

SOURCE_ENVIRONMENT = "环境变量"
SOURCE_KEYRING = "Windows 凭据管理器"
SOURCE_NONE = "未配置"

INSTALL_HINT = (
    "安全存储需要可选依赖 keyring（它使用 Windows 凭据管理器，不写明文文件）。"
    '安装命令：.venv\\Scripts\\python.exe -m pip install -e ".[secrets]"'
)


class KeyringUnavailable(RuntimeError):
    """Secure storage cannot be used in this environment."""


class KeyringError(RuntimeError):
    """The credential manager refused the operation."""


class KeyringBackend(Protocol):
    """The slice of the ``keyring`` API this project uses."""

    def get_password(self, service_name: str, username: str) -> str | None: ...
    def set_password(self, service_name: str, username: str, password: str) -> None: ...
    def delete_password(self, service_name: str, username: str) -> None: ...


@dataclass(frozen=True)
class KeyStatus:
    """What the page may show: never the key itself."""

    provider: str
    source: str
    configured: bool

    @property
    def label(self) -> str:
        return f"已配置（{self.source}）" if self.configured else "未配置"

    def __str__(self) -> str:  # pragma: no cover - trivial, but keep it safe
        return f"KeyStatus(provider={self.provider!r}, source={self.source!r}, configured={self.configured})"


def load_keyring() -> KeyringBackend:
    """Import keyring lazily; the page needs a clear message, not an ImportError.

    Tests replace ``sys.modules['keyring']`` with a fake, so this is also the one
    seam through which a fake credential manager is injected.
    """
    try:
        import keyring  # noqa: PLC0415 - optional dependency, imported on demand
    except ImportError:
        raise KeyringUnavailable(INSTALL_HINT) from None
    return keyring


def _usable(backend: KeyringBackend) -> bool:
    """A keyring with no working backend raises on use; treat that as unavailable."""
    try:
        getter = getattr(backend, "get_keyring", None)
        if getter is None:
            return True
        return getter() is not None
    except Exception:  # noqa: BLE001 - any failure means "cannot store securely"
        return False


class ApiKeyStore:
    """Read, store and delete a provider's key without ever logging it.

    ``backend`` and ``environ`` are injectable for tests. ``backend=None`` means
    "import keyring on demand"; passing a backend directly keeps a test away from
    the real credential manager.
    """

    def __init__(self, *, backend: KeyringBackend | None = None, environ=None,
                 service_name: str = SERVICE_NAME) -> None:
        self._backend = backend
        self._resolved = backend is not None
        self._environ = environ
        self.service_name = service_name

    # -- environment ------------------------------------------------------

    def _environment(self):
        if self._environ is not None:
            return self._environ
        import os

        return os.environ

    def environment_key(self, provider: str) -> str:
        return (self._environment().get(ENV_NAMES[provider], "") or "").strip()

    # -- credential manager ----------------------------------------------

    def _require_backend(self) -> KeyringBackend:
        """Resolve the backend once per store, and report unavailability clearly.

        The import is attempted at most once: on a machine without ``keyring``
        this must not re-import on every settings load.
        """
        if not self._resolved:
            self._resolved = True
            try:
                self._backend = load_keyring()
            except KeyringUnavailable:
                self._backend = None
        if self._backend is None or not _usable(self._backend):
            raise KeyringUnavailable(INSTALL_HINT)
        return self._backend

    def storage_available(self) -> bool:
        """True when a stored key could actually be written and read back."""
        try:
            return _usable(self._require_backend())
        except KeyringUnavailable:
            return False

    def stored_key(self, provider: str) -> str:
        """The stored key, or '' — never raises for a simple absence."""
        try:
            backend = self._require_backend()
        except KeyringUnavailable:
            return ""
        try:
            return (backend.get_password(self.service_name, provider) or "").strip()
        except Exception:  # noqa: BLE001 - a broken store must not crash the app
            return ""

    def save(self, provider: str, key: str) -> None:
        """Store a key. The value is validated, never echoed back or logged."""
        cleaned = validate_key_format(key)
        backend = self._require_backend()
        try:
            backend.set_password(self.service_name, provider, cleaned)
        except Exception as error:  # noqa: BLE001 - surface a safe message only
            raise KeyringError(
                f"写入 Windows 凭据管理器失败（{error.__class__.__name__}）；Key 未被保存。"
            ) from None

    def delete(self, provider: str) -> bool:
        """Remove the stored key. Returns True when something was removed."""
        backend = self._require_backend()
        try:
            backend.delete_password(self.service_name, provider)
        except Exception as error:  # noqa: BLE001
            name = error.__class__.__name__
            # keyring raises PasswordDeleteError when there is nothing to delete.
            if "PasswordDeleteError" in name or "not found" in str(error).lower():
                return False
            raise KeyringError(
                f"从 Windows 凭据管理器删除失败（{name}）；原有内容未被改动。"
            ) from None
        return True

    # -- resolution -------------------------------------------------------

    def resolve(self, provider: str) -> tuple[str, str]:
        """Return ``(key, source)``. Environment wins so existing setups keep working."""
        environment = self.environment_key(provider)
        if environment:
            return environment, SOURCE_ENVIRONMENT
        stored = self.stored_key(provider)
        if stored:
            return stored, SOURCE_KEYRING
        return "", SOURCE_NONE

    def status(self, provider: str) -> KeyStatus:
        key, source = self.resolve(provider)
        return KeyStatus(provider, source, bool(key))


def validate_key_format(key: str) -> str:
    """One place where a key is judged; returns it unchanged when acceptable.

    Print-ASCII with no whitespace at all and a modest upper bound — the same rule
    the existing live-mode validation applied, so an environment-supplied key and
    a stored key are held to the same standard. Surrounding whitespace is
    **rejected rather than trimmed**: a pasted key with a trailing newline is a
    mistake worth reporting, and silently trimming it would store a value the user
    did not type. The value is never included in an error.
    """
    text = key if isinstance(key, str) else ""
    if not text:
        raise ValueError("API Key 不能为空。")
    if any(char.isspace() for char in text):
        raise ValueError("API Key 不能包含空格、换行或制表符；请只粘贴 Key 本身。")
    if len(text) > 256:
        raise ValueError("API Key 过长，请核对是否粘贴了多余内容。")
    if any(ord(char) < 33 or ord(char) > 126 for char in text):
        raise ValueError("API Key 只能包含可见的 ASCII 字符。")
    return text
