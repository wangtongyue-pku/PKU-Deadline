from pku_deadline_agent.config import PROJECT_ROOT, database_path


def test_default_path_is_in_private_data_folder(monkeypatch):
    monkeypatch.delenv("PKU_DATABASE_PATH", raising=False)
    assert database_path() == PROJECT_ROOT / "data" / "deadlines.sqlite3"


def test_custom_database_path(tmp_path, monkeypatch):
    target = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("PKU_DATABASE_PATH", str(target))
    assert database_path() == target
