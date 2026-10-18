"""The remembered grid course list: local, lossy-safe, and used by the sync form."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent import grid_courses

APP = Path(__file__).resolve().parents[1] / "app.py"
COURSE_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"
COURSE_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2"


def test_remember_and_known_round_trip(tmp_path):
    store = tmp_path / "courses.json"
    assert grid_courses.known(store) == ()
    grid_courses.remember(COURSE_A, "虚构数据结构", store)
    grid_courses.remember(COURSE_B, "虚构算法", store)
    courses = grid_courses.known(store)
    assert [course.course_id for course in courses] == [COURSE_B, COURSE_A]  # newest first
    assert courses[0].label == "虚构算法"
    assert courses[0].display().endswith("…") and "虚构算法" in courses[0].display()
    # Remembering the same course again moves it to the top instead of duplicating.
    grid_courses.remember(COURSE_A, "虚构数据结构（改名）", store)
    courses = grid_courses.known(store)
    assert [course.course_id for course in courses] == [COURSE_A, COURSE_B]
    assert courses[0].label == "虚构数据结构（改名）"
    assert grid_courses.last_seen(store)


def test_course_store_is_bounded_and_rejects_junk(tmp_path):
    store = tmp_path / "courses.json"
    for index in range(grid_courses.MAX_COURSES + 5):
        grid_courses.remember(f"{index:032x}", f"虚构课程 {index}", store)
    assert len(grid_courses.known(store)) == grid_courses.MAX_COURSES
    with pytest.raises(ValueError):
        grid_courses.remember("not-a-course-id", "虚构", store)
    # A damaged or missing file means "nothing remembered", never a crash.
    store.write_text("{not json", encoding="utf-8")
    assert grid_courses.known(store) == ()
    assert grid_courses.known(tmp_path / "absent.json") == ()
    assert grid_courses.last_seen(tmp_path / "absent.json") == ""


def test_sync_form_offers_remembered_courses(tmp_path, monkeypatch):
    store = tmp_path / "courses.json"
    grid_courses.remember(COURSE_A, "虚构数据结构", store)
    monkeypatch.setenv("PKU_GRID_COURSES", str(store))
    monkeypatch.setenv("PKU_DATABASE_PATH", str(tmp_path / "ui.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=15)
    app.session_state["page"] = "编程网格导入"
    app.run()
    assert not app.exception, [error.value for error in app.exception]
    picker = app.selectbox(key="grid_course_pick")
    # AppTest exposes the formatted option labels, which is what the user sees.
    assert any("虚构数据结构" in str(text) for text in picker.options)
    # The id field stays available for a course that was never synced before.
    assert any(item.key == "grid_sync_course" for item in app.text_input)
    # Picking a remembered course fills the id field instead of retyping 32 characters.
    app.selectbox(key="grid_course_pick").set_value(COURSE_A).run()
    assert app.text_input(key="grid_sync_course").value == COURSE_A
