from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def test_manual_task_flow_is_local_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    app = AppTest.from_file(APP_PATH).run()
    assert not app.exception
    assert "暂无任务" in app.info[-1].value
    app.text_input[0].set_value("虚构通知对应的作业")
    app.text_input[1].set_value("虚构课程")
    app.button[0].click().run()
    assert not app.exception
    assert app.success
    table = app.dataframe[0].value
    assert table.iloc[0]["标题"] == "虚构通知对应的作业"
    assert table.iloc[0]["截止时间（北京）"] == "待确认"
    reopened = AppTest.from_file(APP_PATH).run()
    assert len(reopened.dataframe[0].value) == 1


def test_explicit_deadline_and_validation(tmp_path, monkeypatch):
    from datetime import date, time

    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    app = AppTest.from_file(APP_PATH).run()
    app.button[0].click().run()
    assert any("标题必须" in error.value for error in app.error)
    app.checkbox[0].check().run()
    app.text_input[0].set_value("虚构定时任务")
    app.date_input[0].set_value(date(2026, 10, 22))
    app.time_input[0].set_value(time(21, 30))
    app.button[0].click().run()
    assert not app.exception
    assert app.dataframe[0].value.iloc[0]["截止时间（北京）"] == "2026-10-22 21:30:00"


def test_missing_time_is_not_invented(tmp_path, monkeypatch):
    from pku_deadline_agent.database import TaskRepository

    path = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("PKU_DATABASE_PATH", str(path))
    app = AppTest.from_file(APP_PATH).run()
    app.checkbox[0].check().run()
    app.text_input[0].set_value("只有日期的虚构通知")
    app.button[0].click().run()
    assert not app.exception
    assert any("请填写明确的截止时刻" in error.value for error in app.error)
    assert TaskRepository(path).list_tasks() == []
