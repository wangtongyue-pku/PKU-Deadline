"""Never let any UI regression read a real Windows secret or ambient AI key."""

import pytest


@pytest.fixture(autouse=True)
def isolate_real_credentials(monkeypatch, tmp_path):
    for name in ("PKU_DEEPSEEK_API_KEY", "PKU_MIMO_API_KEY", "PKU_REAL_API_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    # The daily real-read payload is local user data: no test may pick it up by
    # accident. Tests that exercise it point the variable at their own copy.
    monkeypatch.setenv("PKU_ZHIHUISHU_PAYLOAD", str(tmp_path / "no-real-payload.json"))
    monkeypatch.setenv("PKU_YUETANG_PAYLOAD", str(tmp_path / "no-yuetang-payload.json"))
    # Remembered grid courses are local user data too: never read or overwrite the
    # real list from a test.
    monkeypatch.setenv("PKU_GRID_COURSES", str(tmp_path / "no-grid-courses.json"))
    try:
        from keyring.backends.Windows import WinVaultKeyring
    except ImportError:
        return
    monkeypatch.setattr(WinVaultKeyring, "get_password", lambda *args, **kw: None)
    def refuse(*args, **kw):
        raise AssertionError("tests must inject a fake credential backend")
    monkeypatch.setattr(WinVaultKeyring, "set_password", refuse)
    monkeypatch.setattr(WinVaultKeyring, "delete_password", refuse)
