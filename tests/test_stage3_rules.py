import json
from datetime import datetime

import pytest

from pku_deadline_agent.deadline_rules import deadline_evidence, resolve_deadline
from pku_deadline_agent.extraction import local_candidates, validate_tasks
from pku_deadline_agent.notices import filter_notices
from pku_deadline_agent.time_utils import BEIJING

PUB = datetime(2026, 10, 11, 9, tzinfo=BEIJING)


@pytest.mark.parametrize("expression, expected", [
    ("2026-10-20 20:00", "2026-10-20T20:00:00+08:00"),
    ("2026年10月20日08:05", "2026-10-20T08:05:00+08:00"),
    ("下周三晚20:00", "2026-10-14T20:00:00+08:00"),
    ("明晚八点", "2026-10-12T20:00:00+08:00"),
    ("明天上午八点半", "2026-10-12T08:30:00+08:00"),
    ("下下周五下午三点", "2026-10-23T15:00:00+08:00"),
    ("10月20日23:59", "2026-10-20T23:59:00+08:00"),
    ("后天中午十二点", "2026-10-13T12:00:00+08:00"),
    ("今天18:00", "2026-10-11T18:00:00+08:00"),
    ("明天上午八点三十分", "2026-10-12T08:30:00+08:00"),
], ids=["absolute", "chinese-date", "next-week", "tomorrow-night", "am-half", "two-weeks", "month-day", "noon", "today", "chinese-minute"])
def test_resolves_only_grounded_complete_dates(expression, expected):
    assert resolve_deadline(expression, PUB).due_at.isoformat() == expected


@pytest.mark.parametrize("expression, publication", [
    ("明晚八点", None), ("下周三20:00", None),
    ("下周三", PUB), ("明晚", PUB),
    ("2026-10-20", PUB), ("2026-02-30 20:00", PUB),
    ("明天八点", PUB), ("明天25:00", PUB), ("明天20:80", PUB),
    ("明天上午20:00", PUB), ("明天20:00或后天20:00", PUB),
    ("另行通知", PUB), ("暂定明晚八点", PUB), ("取消明晚八点", PUB),
    ("明晚十二点", PUB), ("明天晚上0点", PUB), ("明天下午0:00", PUB),
    ("明天上午八点一刻", PUB), ("明天上午八点多", PUB), ("明天23:59:30", PUB),
], ids=["missing-pub", "missing-week-pub", "date-only", "night-only", "absolute-day", "invalid-day", "ambiguous-hour", "invalid-hour", "invalid-minute", "contradictory-period", "alternative", "tbd", "tentative", "cancelled", "midnight-date", "night-zero", "pm-zero", "quarter", "approx-hour", "seconds"])
def test_ambiguous_times_never_guess(expression, publication):
    result = resolve_deadline(expression, publication)
    assert result.due_at is None
    assert result.reason


def test_absolute_timestamp_does_not_require_publication():
    assert resolve_deadline("2026-10-20 08:00", None).due_at.hour == 8


@pytest.mark.parametrize("expression, expected", [
    ("周五20:00", "2026-10-09T20:00:00+08:00"),
    ("星期三晚上八点", "2026-10-07T20:00:00+08:00"),
    ("下星期三20:00", "2026-10-14T20:00:00+08:00"),
    ("这周日20:00", "2026-10-11T20:00:00+08:00"),
    ("10月20日20:00", "2026-10-20T20:00:00+08:00"),
    ("10月20号20:00", "2026-10-20T20:00:00+08:00"),
    ("10/20 20:00", "2026-10-20T20:00:00+08:00"),
    ("10-20 20:00", "2026-10-20T20:00:00+08:00"),
    ("1月1日20:00", "2026-01-01T20:00:00+08:00"),
    ("明年1月1日20:00", "2027-01-01T20:00:00+08:00"),
    ("后年1月1日20:00", "2028-01-01T20:00:00+08:00"),
    ("去年12月31日20:00", "2025-12-31T20:00:00+08:00"),
    ("2027年的活动1月1日20:00截止", "2027-01-01T20:00:00+08:00"),
    ("2025-10-20 20:00", "2025-10-20T20:00:00+08:00"),
], ids=["this-friday", "this-wednesday", "next-week-alias", "this-sunday", "default-year", "day-alias", "slash-month-day", "dash-month-day", "past-no-rollover", "next-year", "two-years", "last-year", "context-year", "explicit-past-year"])
def test_default_week_year_and_explicit_overrides(expression, expected):
    assert resolve_deadline(expression, PUB).due_at.isoformat() == expected


def test_year_without_publication_defaults_to_current_beijing_year(monkeypatch):
    monkeypatch.setattr("pku_deadline_agent.deadline_rules.beijing_now", lambda: PUB)
    assert resolve_deadline("10月20日20:00", None).due_at.year == 2026


def test_conflicting_years_stay_pending():
    result = resolve_deadline("2027年或2028年的活动1月1日20:00截止", PUB)
    assert result.due_at is None


def test_past_default_dates_are_known_and_never_shift_forward():
    result = resolve_deadline("周五20:00", PUB)
    assert result.due_at.day == 9
    assert "未自动顺延" in result.reason
    result = resolve_deadline("1月1日20:00", PUB)
    assert result.due_at.year == 2026
    assert "已过期" in result.reason


def test_default_year_at_year_boundary_and_leap_day():
    reference = datetime(2026, 12, 31, 9, tzinfo=BEIJING)
    assert resolve_deadline("1月2日20:00", reference).due_at.year == 2026
    assert resolve_deadline("明年1月2日20:00", reference).due_at.year == 2027
    assert resolve_deadline("2月29日20:00", reference).due_at is None
    assert resolve_deadline("2028年2月29日20:00", reference).due_at.year == 2028


def test_year_in_notice_context_is_used_for_model_evidence():
    notices = filter_notices("2027年的虚构活动，1月2日20:00截止报名。", PUB, mode="natural").notices
    task = validate_tasks(task_json(deadline_text="1月2日20:00", due_at=None), notices)[0]
    assert task.due_at.isoformat() == "2027-01-02T20:00:00+08:00"


@pytest.mark.parametrize("modifier", ["不是", "并非", "尚未明确", "可能", "预计", "待确认"])
def test_negation_or_uncertainty_never_becomes_a_confirmed_due(modifier):
    assert resolve_deadline(f"{modifier}明晚八点截止", PUB).due_at is None


@pytest.mark.parametrize("expression", ["2026-10-20 20:00 UTC", "2026-10-20 20:00 GMT", "2026-10-20 20:00 美东时间", "2026-10-20 20:00 东京时间", "2026-10-20 周三20:00"])
def test_other_timezones_and_contradictory_weekdays_are_not_ignored(expression):
    assert resolve_deadline(expression, PUB).due_at is None


def test_extension_uses_new_deadline_not_original():
    text = "《虚构课程》作业原定本周五23:59截止，现延期至下周三晚20:00，请在教学平台提交。"
    evidence = deadline_evidence(text)
    assert evidence == "下周三晚20:00"
    tasks, complete = local_candidates(filter_notices(text, PUB, mode="natural").notices)
    assert complete
    assert tasks[0].deadline_changed
    assert tasks[0].due_at.day == 14


def task_json(**changes):
    task = {"source_index": 1, "title": "虚构作业", "category": "虚构课程", "task_type": "作业", "deadline_text": "明晚八点", "due_at": "2026-10-12T20:00:00+08:00", "deadline_changed": False, "review_notes": []}
    task.update(changes)
    return json.dumps({"tasks": [task]}, ensure_ascii=False)


@pytest.mark.parametrize("change", [
    {"source_index": True}, {"source_index": 99}, {"title": ""}, {"title": "x" * 201},
    {"category": []}, {"task_type": "shell"}, {"deadline_changed": "false"},
    {"review_notes": "ok"}, {"deadline_text": "后天18:00"},
    {"due_at": "2026-10-12"}, {"due_at": "2026-10-12T12:00:00Z"},
    {"due_at": "2026-02-30T20:00:00+08:00"}, {"command": "run"},
    {"title": "Cookie: fake-only"}, {"title": "虚构作业 13800138000"},
], ids=["bool-index", "missing-source", "empty-title", "long-title", "wrong-category", "bad-enum", "not-bool", "not-notes", "invented-evidence", "date-only", "wrong-zone", "invalid-date", "extra-command", "returned-secret", "returned-contact"])
def test_untrusted_json_rejected_without_execution(change):
    notices = filter_notices("明晚八点交作业。", PUB, mode="natural").notices
    with pytest.raises(ValueError):
        validate_tasks(task_json(**change), notices)


@pytest.mark.parametrize("text", ["not json", "```json\n{}\n```", '{"tasks":[],"tasks":[]}', '{"tasks":NaN}', '{"tasks":[NaN]}', '{"tasks":{}}', '{"tasks":[],"command":"run"}'])
def test_json_syntax_and_top_level_constraints(text):
    with pytest.raises(ValueError):
        validate_tasks(text, ())


def test_model_cannot_invent_date_when_publication_unknown():
    notices = filter_notices("明晚八点交作业。", mode="natural").notices
    task = validate_tasks(task_json(), notices)[0]
    assert task.due_at is None
    assert any("发布时间" in note for note in task.review_notes)


def test_local_verification_corrects_model_date_with_a_warning():
    notices = filter_notices("明晚八点交作业。", PUB, mode="natural").notices
    task = validate_tasks(task_json(due_at="2026-12-01T20:00:00+08:00"), notices)[0]
    assert task.due_at.day == 12
    assert any("不一致" in note for note in task.review_notes)


def test_model_cannot_present_old_date_as_new_deadline():
    raw = "作业原定本周五23:59截止，延期至下周三晚20:00。"
    notices = filter_notices(raw, PUB, mode="natural").notices
    task = validate_tasks(task_json(deadline_text="本周五23:59", due_at="2026-10-16T23:59:00+08:00", deadline_changed=True), notices)[0]
    assert task.due_at is None
    assert task.deadline_changed


@pytest.mark.parametrize("raw", ["作业不是明晚八点截止。", "作业暂定明晚八点截止。", "作业明晚八点或后天八点截止。", "作业可能延期至明晚八点。"])
def test_model_cannot_remove_uncertain_context_by_quoting_a_substring(raw):
    notices = filter_notices(raw, PUB, mode="natural").notices
    task = validate_tasks(task_json(), notices)[0]
    assert task.due_at is None
    assert task.review_notes


def test_multiple_tasks_can_share_one_notice_with_separate_evidence():
    notices = filter_notices("作业明晚八点交，实验报告后天20:00提交。", PUB, mode="natural").notices
    first = json.loads(task_json())["tasks"][0]
    second = {**first, "title": "虚构实验", "deadline_text": "后天20:00", "due_at": "2026-10-13T20:00:00+08:00"}
    tasks = validate_tasks(json.dumps({"tasks": [first, second]}, ensure_ascii=False), notices)
    assert [task.due_at.day for task in tasks] == [12, 13]
