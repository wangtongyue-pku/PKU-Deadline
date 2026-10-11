"""Stage 5 adapter contract tests. Offline only: fictional payloads, no platform.

These tests call no platform, read no credential and import nothing that can open
a socket, so a passing run proves the adapter layer itself, not real connectivity.
"""

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pku_deadline_agent import sources
from pku_deadline_agent.mock_sources import (
    FICTIONAL_GRID_PAYLOAD,
    FICTIONAL_TEACHING_PAYLOAD,
    GRID_COURSE_JSON,
    MockProgrammingGridAdapter,
    MockTeachingPlatformAdapter,
    MockTransport,
    ProgrammingGridJsonAdapter,
    _failure_auth,
    _failure_rate,
    _failure_unavailable,
    parse_grid_time,
)
from pku_deadline_agent.sources import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_PENDING,
    FetchResult,
    Rejection,
    SourceAdapter,
    SourceAuthRequired,
    SourceCapabilities,
    SourceRateLimited,
    SourceRecord,
    SourceRejected,
    SourceUnavailable,
    SourceUnsupported,
    SourceUnverified,
    adapter_sources,
    create_adapter,
    describe_adapters,
    external_key,
    normalize_status,
    normalize_text,
    sort_records,
    validate_record,
)
from pku_deadline_agent.time_utils import BEIJING

NEW_YORK = timezone(timedelta(hours=-4))
NOW = datetime(2026, 10, 11, 23, 30, tzinfo=BEIJING)


def record(**overrides) -> SourceRecord:
    base = dict(source="teaching_platform", source_id="FA-1", task_title="虚构第一次作业", course="虚构课程甲")
    base.update(overrides)
    return SourceRecord(**base)


# ---------------------------------------------------------------- text handling


@pytest.mark.parametrize("value", ["", "   ", "\t\n", None])
def test_blank_text_normalizes_to_empty(value):
    assert normalize_text(value, limit=10, label="任务标题") == ""


def test_whitespace_is_collapsed_but_content_is_kept():
    assert normalize_text("  虚构   第一次\n作业  ", limit=40, label="任务标题") == "虚构 第一次 作业"


def test_over_long_text_is_rejected_not_truncated():
    with pytest.raises(SourceRejected) as error:
        normalize_text("虚" * 11, limit=10, label="任务标题")
    assert "10" in str(error.value)


def test_non_text_value_is_rejected():
    with pytest.raises(SourceRejected):
        normalize_text(123, limit=10, label="任务标题")


# ------------------------------------------------------------- datetime handling


@pytest.mark.parametrize("value,expected", [
    ("2026-10-20T23:59:00+08:00", datetime(2026, 10, 20, 23, 59, tzinfo=BEIJING)),
    ("2026-10-20 23:59:00+08:00", datetime(2026, 10, 20, 23, 59, tzinfo=BEIJING)),
    ("2026-10-20T23:59+08:00", datetime(2026, 10, 20, 23, 59, tzinfo=BEIJING)),
    ("2026-10-20T23:59:00Z", datetime(2026, 10, 21, 7, 59, tzinfo=BEIJING)),
    ("2026-10-20T11:59:00-04:00", datetime(2026, 10, 20, 23, 59, tzinfo=BEIJING)),
])
def test_timezone_aware_iso_8601_is_normalized_to_beijing(value, expected):
    result = validate_record(record(deadline=value), source="teaching_platform")
    assert result.deadline == expected
    assert result.deadline.utcoffset() == timedelta(hours=8)


@pytest.mark.parametrize("value", ["2026-10-20T23:59:00", "2026-10-20", "2026-10-20 23:59"])
def test_naive_time_is_rejected_instead_of_using_host_timezone(value):
    with pytest.raises(SourceRejected) as error:
        validate_record(record(deadline=value), source="teaching_platform")
    assert "时区" in str(error.value)


def test_unparseable_time_is_rejected_and_stays_empty():
    with pytest.raises(SourceRejected):
        validate_record(record(deadline="下周三晚上八点"), source="teaching_platform")


@pytest.mark.parametrize("value", [None, ""])
def test_missing_time_is_allowed_and_stays_empty(value):
    result = validate_record(record(deadline=value, published_at=value), source="teaching_platform")
    assert result.deadline is None and result.published_at is None


def test_non_datetime_type_is_rejected():
    with pytest.raises(SourceRejected):
        validate_record(record(deadline=1750000000), source="teaching_platform")


# --------------------------------------------------------------- status mapping


@pytest.mark.parametrize("value,expected", [
    (STATUS_PENDING, STATUS_PENDING),
    (STATUS_DONE, STATUS_DONE),
    (STATUS_CANCELLED, STATUS_CANCELLED),
    ("已完成（推测）", None),
    ("whatever the platform says", None),
    (None, None),
    ("", None),
])
def test_unknown_status_becomes_none_and_never_a_guess(value, expected):
    assert normalize_status(value) == expected


def test_status_is_optional_and_absence_is_preserved():
    assert validate_record(record(), source="teaching_platform").status is None


def test_non_text_status_is_rejected():
    with pytest.raises(SourceRejected):
        normalize_status(1)


# ------------------------------------------------------------- record validation


def test_record_from_another_source_is_rejected():
    with pytest.raises(SourceRejected) as error:
        validate_record(record(source="programming_grid"), source="teaching_platform")
    assert "来源" in str(error.value)


def test_missing_source_id_is_rejected_because_dedup_needs_it():
    with pytest.raises(SourceRejected) as error:
        validate_record(record(source_id="  "), source="teaching_platform")
    assert "标识" in str(error.value)


def test_empty_title_is_rejected_rather_than_invented():
    with pytest.raises(SourceRejected):
        validate_record(record(task_title=""), source="teaching_platform")


def test_missing_course_is_allowed_and_stays_empty():
    assert validate_record(record(course=None), source="teaching_platform").course == ""


def test_missing_task_type_defaults_to_other():
    assert validate_record(record(task_type=""), source="teaching_platform").task_type == "其他"


@pytest.mark.parametrize("url,valid", [
    ("https://example.invalid/fictional/a", True),
    ("http://example.invalid/fictional/a", True),
    ("javascript:alert(1)", False),
    ("file:///C:/secret.txt", False),
    ("example.invalid/a", False),
    ("", True),
    (None, True),
])
def test_source_url_must_be_http_or_https(url, valid):
    if valid:
        assert validate_record(record(source_url=url), source="teaching_platform").source_url == (url or None)
    else:
        with pytest.raises(SourceRejected):
            validate_record(record(source_url=url), source="teaching_platform")


def test_non_record_input_is_rejected():
    with pytest.raises(SourceRejected):
        validate_record({"source_id": "FA-1"}, source="teaching_platform")


# ------------------------------------------------------------------- dedup key


def test_external_key_is_stable_and_platform_scoped():
    assert external_key(record()) == "teaching_platform:FA-1"
    assert external_key(record()) == external_key(record(task_title="改名后的标题"))
    assert external_key(record()) != external_key(record(source="programming_grid"))


def test_external_key_requires_a_platform_identifier():
    with pytest.raises(SourceRejected):
        external_key(record(source_id=""))


def test_sorting_is_deterministic_with_unknown_deadlines_last():
    early = record(source_id="A", deadline=NOW)
    late = record(source_id="B", deadline=NOW + timedelta(days=3))
    unknown = record(source_id="C", deadline=None)
    assert [item.source_id for item in sort_records([unknown, late, early])] == ["A", "B", "C"]


# ------------------------------------------------------------------ mock adapters


def test_teaching_mock_maps_fictional_payload_to_beijing_records():
    result = MockTeachingPlatformAdapter().fetch()
    assert result.source == "teaching_platform"
    assert result.complete and result.rejections == ()
    first, second, third = result.records
    assert (first.source_id, first.task_title, first.course, first.task_type) == ("FA-1", "虚构第一次作业", "虚构课程甲", "作业")
    assert first.deadline == datetime(2026, 10, 20, 23, 59, tzinfo=BEIJING)
    assert first.published_at == datetime(2026, 10, 8, 9, 0, tzinfo=BEIJING)
    assert first.status == STATUS_PENDING
    assert first.source_url == "https://example.invalid/fictional/course-001/assignments/FA-1"
    # Deferred deadline and an activity with no deadline are both preserved as given.
    assert second.deadline == datetime(2026, 11, 2, 20, 0, tzinfo=BEIJING)
    assert third.source_id == "FB-1" and third.deadline is None and third.course == "虚构校园活动乙"


def test_grid_mock_keeps_missing_fields_empty_and_never_guesses_status():
    teaching_ids = {item.source_id for item in MockTeachingPlatformAdapter().fetch().records}
    result = MockProgrammingGridAdapter().fetch()
    assert result.source == "programming_grid"
    assert [item.source_id for item in result.records] == ["FP-100", "FP-101"]
    assert all(item.source_url is None for item in result.records)
    assert all(item.status is None for item in result.records)
    assert result.records[0].task_title == "虚构题集：循环与数组（共 6 题）"
    assert "FP-100" not in teaching_ids


def test_fetch_result_is_stable_across_runs():
    first = MockTeachingPlatformAdapter().fetch()
    second = MockTeachingPlatformAdapter().fetch()
    assert [item.source_id for item in first.records] == [item.source_id for item in second.records]
    assert [external_key(item) for item in first.records] == [external_key(item) for item in second.records]


def test_records_are_normalized_by_the_adapter_not_trusted():
    payload = [{"id": "FC-9", "name": "  虚构课程丙  ", "assignments": [
        {"assignmentId": " FC-9-A ", "title": "  虚构  作业  ", "type": "", "dueAt": "2026-10-20T11:59:00-04:00", "state": "submitted"}]}]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    item = result.records[0]
    assert (item.source_id, item.course, item.task_title) == ("FC-9-A", "虚构课程丙", "虚构 作业")
    assert item.task_type == "其他"
    assert item.deadline == datetime(2026, 10, 20, 23, 59, tzinfo=BEIJING)
    assert item.status == STATUS_DONE


def test_unsorted_platform_order_is_normalized():
    payload = [{"id": "FX", "name": "虚构课程丁", "assignments": [
        {"assignmentId": "FX-late", "title": "晚的", "dueAt": "2026-12-01T20:00:00+08:00"},
        {"assignmentId": "FX-none", "title": "无截止"},
        {"assignmentId": "FX-early", "title": "早的", "dueAt": "2026-10-01T20:00:00+08:00"}]}]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    assert [item.source_id for item in result.records] == ["FX-early", "FX-late", "FX-none"]


# ------------------------------------------------------- incomplete platform data


def test_bad_record_is_reported_and_good_records_survive():
    payload = [{"id": "FC-1", "name": "虚构课程戊", "assignments": [
        {"assignmentId": "OK-1", "title": "虚构正常作业", "dueAt": "2026-10-20T23:59:00+08:00"},
        {"assignmentId": "BAD-1", "title": "虚构时间异常作业", "dueAt": "下周三"},
        {"assignmentId": "BAD-2", "title": "", "dueAt": "2026-10-21T23:59:00+08:00"},
        "not-an-object"]}]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    assert [item.source_id for item in result.records] == ["OK-1"]
    assert result.complete is False
    reasons = {item.source_id: item.reason for item in result.rejections}
    assert set(reasons) == {"BAD-1", "BAD-2", None}
    assert "猜测" in reasons["BAD-1"] and "标题" in reasons["BAD-2"]


def test_rejections_never_contain_accepted_records():
    payload = [{"id": "FC-2", "name": "虚构课程己", "assignments": [
        {"assignmentId": "OK-2", "title": "虚构正常作业"}, {"assignmentId": "", "title": "虚构缺标识作业"}]}]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    accepted = {item.source_id for item in result.records}
    rejected = {item.source_id for item in result.rejections}
    assert accepted == {"OK-2"} and rejected == {None}


def test_missing_course_name_does_not_invent_one():
    payload = [{"assignments": [{"assignmentId": "FC-3-A", "title": "虚构无课程作业"}]}]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    assert result.records[0].course == ""


# --------------------------------------- damaged shape must not look like "no work"
#
# An empty record list and "the platform has no tasks" look identical downstream.
# Reported bug: a malformed course entry produced zero records, zero rejections and
# complete=True, so a parse failure would read as "nothing due".


def test_non_object_course_entry_is_reported_not_silently_dropped():
    payload = ["not-an-object", 42, None]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    assert result.records == ()
    assert len(result.rejections) == 3
    assert result.complete is False
    assert all("课程条目" in item.reason for item in result.rejections)


def test_course_without_assignments_list_is_surfaced_as_a_warning():
    payload = [{"id": "FC-5", "name": "虚构课程辛"}, {"id": "FC-6", "name": "虚构课程壬", "announcements": []}]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    assert result.records == () and result.rejections == ()
    assert result.complete is True
    assert len(result.warnings) == 2
    assert all("assignments" in text for text in result.warnings)
    assert "虚构课程辛" in result.warnings[0]


# -------------------------- complete is NOT the same as "safe to trust"
#
# Reported boundary: complete only means "no entry was rejected". An unconfirmed
# field still yields complete=True, so stage 6 must never read complete=True plus
# zero records as "this source has no tasks".


@pytest.mark.parametrize("payload,expect_complete,expect_trustworthy,expect_empty", [
    # A genuinely empty course: the only case that means "no tasks here".
    ([{"id": "c", "name": "虚构课程空", "assignments": []}], True, True, True),
    # Field missing: cannot tell an empty course from a renamed field.
    ([{"id": "c", "name": "虚构课程辛"}], True, False, False),
    # Field explicitly null: same uncertainty.
    ([{"id": "c", "name": "虚构课程辛", "assignments": None}], True, False, False),
    # Field of the wrong type: rejected outright.
    ([{"id": "c", "name": "虚构课程癸", "assignments": {"a": 1}}], False, False, False),
    # Unreadable entry: rejected outright.
    (["broken"], False, False, False),
    # Healthy data is both complete and trustworthy.
    ([{"id": "c", "name": "虚构课程子", "assignments": [
        {"assignmentId": "OK-1", "title": "虚构作业", "dueAt": "2026-11-05T23:59:00+08:00"}]}], True, True, False),
])
def test_complete_does_not_mean_trustworthy(payload, expect_complete, expect_trustworthy, expect_empty):
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    assert result.complete is expect_complete
    assert result.trustworthy is expect_trustworthy
    assert result.confirmed_empty is expect_empty


def test_complete_true_with_zero_records_still_blocks_destructive_action():
    """The exact reported trap: complete=True, no records, but a missing field."""
    result = MockTeachingPlatformAdapter(MockTransport([{"id": "c", "name": "虚构课程辛"}])).fetch()
    assert result.complete is True and result.records == ()
    assert result.confirmed_empty is False
    with pytest.raises(SourceUnverified) as error:
        result.require_trustworthy()
    assert "删除" in str(error.value)


def test_require_trustworthy_passes_for_healthy_and_for_confirmed_empty():
    healthy = MockTeachingPlatformAdapter().fetch()
    healthy.require_trustworthy()
    empty = MockTeachingPlatformAdapter(MockTransport([{"id": "c", "name": "虚构课程空", "assignments": []}])).fetch()
    empty.require_trustworthy()
    assert empty.confirmed_empty is True


def test_unverified_batch_reports_how_many_items_could_not_be_confirmed():
    payload = ["broken", {"id": "c", "name": "虚构课程丑"}]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    with pytest.raises(SourceUnverified) as error:
        result.require_trustworthy()
    assert "2 项" in str(error.value)


def test_assignments_of_wrong_type_is_a_rejection_not_an_empty_result():
    payload = [{"id": "FC-7", "name": "虚构课程癸", "assignments": {"FA-9": {"title": "虚构作业"}}}]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    assert result.records == () and result.complete is False
    assert "不是列表" in result.rejections[0].reason


def test_damaged_course_does_not_hide_healthy_courses():
    payload = [
        "broken",
        {"id": "FC-8", "name": "虚构课程子", "assignments": [{"assignmentId": "OK-8", "title": "虚构正常作业", "dueAt": "2026-11-05T23:59:00+08:00"}]},
        {"id": "FC-9", "name": "虚构课程丑"},
    ]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    assert [item.source_id for item in result.records] == ["OK-8"]
    assert len(result.rejections) == 1 and len(result.warnings) == 1


def test_warnings_and_rejections_survive_the_fetch_pipeline():
    """The base class must not discard warnings collected by an adapter."""
    payload = [{"id": "FC-10", "name": "虚构课程寅"}]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    assert result.warnings and "assignments" in result.warnings[0]


def test_grid_payload_that_is_not_an_array_is_rejected_not_treated_as_empty():
    adapter = MockProgrammingGridAdapter(MockTransport({"problemSets": []}))
    with pytest.raises(SourceRejected) as error:
        adapter.fetch()
    assert "没有作业" in str(error.value)


def test_teaching_payload_that_is_not_an_array_is_rejected_not_iterated_as_keys():
    """A dict payload must not be silently iterated into its key names."""
    adapter = MockTeachingPlatformAdapter(MockTransport({"courses": []}))
    with pytest.raises(SourceRejected) as error:
        adapter.fetch()
    assert "没有作业" in str(error.value)


def test_empty_platform_payload_is_an_empty_result_not_an_error():
    result = MockTeachingPlatformAdapter(MockTransport([])).fetch()
    assert result.records == () and result.rejections == () and result.complete


def test_error_details_never_leak_the_payload():
    payload = [{"id": "FC-4", "name": "虚构课程庚", "assignments": [{"assignmentId": "BAD-4", "title": "虚构坏数据作业", "dueAt": "不是时间"}]}]
    result = MockTeachingPlatformAdapter(MockTransport(payload)).fetch()
    text = repr(result)
    assert "BAD-4" in text and "不是时间" not in text


# ------------------------------------------------------------------ failure paths


class FailingGridAdapter(MockProgrammingGridAdapter):
    """Fails during ``_load`` while the payload itself is perfectly good."""

    def __init__(self, failure=_failure_unavailable):
        super().__init__(MockTransport(FICTIONAL_GRID_PAYLOAD, failure=failure))


@pytest.mark.parametrize("failure,expected", [
    (_failure_unavailable, SourceUnavailable),
    (_failure_auth, SourceAuthRequired),
    (_failure_rate, SourceRateLimited),
])
def test_platform_failures_propagate_instead_of_looking_empty(failure, expected):
    adapter = MockTeachingPlatformAdapter(MockTransport([], failure=failure))
    with pytest.raises(expected) as error:
        adapter.fetch()
    assert "模拟" in str(error.value)


def test_failure_is_not_swallowed_into_a_partial_result():
    with pytest.raises(SourceUnavailable):
        FailingGridAdapter().fetch()


# ------------------------------------------------------------- incremental sync


def test_incremental_request_is_refused_when_capability_is_absent():
    with pytest.raises(SourceUnsupported) as error:
        MockTeachingPlatformAdapter().fetch(since=NOW)
    assert "增量" in str(error.value)


def test_incremental_request_is_refused_by_the_transport_not_silently_widened():
    adapter = MockTeachingPlatformAdapter(MockTransport(FICTIONAL_TEACHING_PAYLOAD))
    with pytest.raises(SourceUnsupported):
        adapter._load(NOW)


def test_naive_since_is_rejected():
    with pytest.raises(SourceRejected):
        MockTeachingPlatformAdapter().fetch(since=datetime(2026, 10, 11, 9, 0))


def test_incremental_adapter_still_needs_the_declared_capability():
    class IncrementalMock(MockTeachingPlatformAdapter):
        capabilities = SourceCapabilities(readable=("task_title",), incremental=True, needs_login=True)

    transport = MockTransport(FICTIONAL_TEACHING_PAYLOAD, incremental_supported=True)
    result = IncrementalMock(transport).fetch(since=NOW)
    assert transport.calls == [NOW]
    assert result.since == NOW
    assert [item.source_id for item in result.records] == ["FA-1", "FA-2", "FB-1"]


def test_adapter_capabilities_are_declared_not_assumed():
    teaching = MockTeachingPlatformAdapter.capabilities
    grid = MockProgrammingGridAdapter.capabilities
    assert teaching.incremental is False and grid.incremental is False
    assert grid.reports_status is False
    assert teaching.needs_login and grid.needs_login


# --------------------------------------------------------- base class behaviour


def test_base_class_requires_a_source_and_load_implementation():
    class NoSource(SourceAdapter):
        def _load(self, since):
            return FetchResult(source="")

    with pytest.raises(ValueError):
        NoSource()
    with pytest.raises(TypeError):
        SourceAdapter()


def test_broken_adapter_output_is_reported_not_trusted():
    class WrongType(MockTeachingPlatformAdapter):
        def _load(self, since):
            return [{"id": "x"}]

    with pytest.raises(SourceRejected) as error:
        WrongType(MockTransport()).fetch()
    assert "FetchResult" in str(error.value)


def test_adapter_raising_a_non_source_error_still_fails_loudly():
    class Boom(MockTeachingPlatformAdapter):
        def _load(self, since):
            raise KeyError("platform shape changed")

    with pytest.raises(KeyError):
        Boom(MockTransport()).fetch()


# --------------------------------------------------------------------- registry


def test_registered_sources_are_listed_without_running_platform_code():
    assert set(adapter_sources()) >= {"teaching_platform", "programming_grid"}
    described = {item.source: item for item in describe_adapters()}
    assert described["teaching_platform"].display_name == "教学网（模拟适配器）"
    # The grid registers the adapter whose shape was confirmed on real data.
    assert described["programming_grid"].factory is ProgrammingGridJsonAdapter
    assert described["programming_grid"].capabilities.needs_login is True
    assert described["programming_grid"].capabilities.reports_status is False
    assert "真实" in described["programming_grid"].capabilities.notes


def test_grid_adapter_reports_it_cannot_read_completion_state():
    """Verified on real data: this endpoint carries no per-problem status."""
    assert ProgrammingGridJsonAdapter.capabilities.reports_status is False
    assert "status" not in ProgrammingGridJsonAdapter.capabilities.readable


@pytest.mark.parametrize("source,expected", [
    ("teaching_platform", MockTeachingPlatformAdapter),
    ("programming_grid", ProgrammingGridJsonAdapter),
])
def test_create_adapter_returns_the_registered_class(source, expected):
    assert isinstance(create_adapter(source), expected)


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError):
        create_adapter("not_a_real_platform")


def test_adapter_sources_and_task_types_stay_compatible_with_the_task_model():
    """Stage 6 must map into existing enums; drift here would break saving later."""
    from pku_deadline_agent.models import SOURCES, TASK_TYPES

    assert set(adapter_sources()) <= set(SOURCES)
    assert sources.TASK_TYPE_OTHER in TASK_TYPES
    assert set(sources.STATUSES) == {"未完成", "已完成", "已取消"}


def test_registry_note_points_at_the_research_document():
    for info in describe_adapters():
        assert "docs/stage5_feasibility.md" in info.capabilities.notes


# ------------------------------------------------- real grid shape (2026-10-11)
#
# Structure verified on a real course page: field names, nesting and the
# "2026-11-13 15:00:00.0" time format are real; teacher, course, titles and ids
# are synthetic.


def test_grid_time_parses_the_platform_format():
    assert parse_grid_time("2026-11-13 15:00:00.0", "截止时间") == datetime(2026, 11, 13, 15, 0, tzinfo=BEIJING)
    assert parse_grid_time("2026-11-13T15:00:00", "截止时间") == datetime(2026, 11, 13, 15, 0, tzinfo=BEIJING)
    assert parse_grid_time("2026-11-13 15:00:00", "截止时间") == datetime(2026, 11, 13, 15, 0, tzinfo=BEIJING)


@pytest.mark.parametrize("value", ["", "下周三", "2026/11/13 15:00", "13-11-2026 15:00:00", None, 1788969599000])
def test_grid_time_rejects_anything_it_cannot_read_exactly(value):
    with pytest.raises(SourceRejected):
        parse_grid_time(value, "截止时间")


def test_nonzero_fractional_seconds_are_refused_not_rounded():
    """Rounding would silently move a deadline by up to a second."""
    assert parse_grid_time("2026-11-13 15:00:00.0", "截止时间").second == 0
    assert parse_grid_time("2026-11-13 15:00:00.000", "截止时间").second == 0
    with pytest.raises(SourceRejected) as error:
        parse_grid_time("2026-11-13 15:00:00.5", "截止时间")
    assert "小数秒" in str(error.value)


def test_grid_course_response_maps_every_real_field():
    result = ProgrammingGridJsonAdapter().fetch()
    assert len(result.records) == len(GRID_COURSE_JSON["course"]["problemlists"]) == 16
    assert result.rejections == () and result.warnings == ()
    assert result.trustworthy is True
    first = result.records[0]
    assert first.source == "programming_grid"
    assert first.task_title == "虚拟抄写练习（选做，不计入成绩）"
    assert first.course == "虚拟课程 2026 虚拟老师"
    assert first.task_type == "Homework"
    assert first.deadline == datetime(2026, 9, 16, 15, 0, tzinfo=BEIJING)
    assert first.published_at == datetime(2026, 9, 9, 15, 0, tzinfo=BEIJING)
    assert first.status is None
    assert first.source_url is None


def test_grid_source_id_is_the_platform_object_id():
    """A 32-hex server id, unlike pku3b's local hash: usable as a stable key."""
    result = ProgrammingGridJsonAdapter().fetch()
    ids = [item.source_id for item in result.records]
    assert len(set(ids)) == len(ids)
    assert all(len(value) == 32 for value in ids)


def test_grid_records_are_sorted_by_deadline_not_platform_order():
    """The real payload arrives newest-first; output must be deadline-ordered."""
    platform_order = [item["title"] for item in GRID_COURSE_JSON["course"]["problemlists"]]
    result = ProgrammingGridJsonAdapter().fetch()
    deadlines = [item.deadline for item in result.records]
    assert deadlines == sorted(deadlines)
    assert result.records[0].task_title != platform_order[0]


def test_grid_course_level_millisecond_window_is_not_used_as_a_deadline():
    """course.closeTime is epoch ms for the whole course, not a task deadline."""
    result = ProgrammingGridJsonAdapter().fetch()
    assert all(item.deadline.year <= 2027 for item in result.records)
    assert datetime(2026, 9, 10, tzinfo=BEIJING) not in {item.deadline for item in result.records}


def test_grid_deadline_and_open_time_are_distinct_fields():
    """Swapping them would move every deadline; check one known pair."""
    result = ProgrammingGridJsonAdapter().fetch()
    target = next(item for item in result.records if item.task_title == "虚拟第二阶段练习（1）")
    assert target.published_at == datetime(2026, 9, 30, 12, 0, tzinfo=BEIJING)
    assert target.deadline == datetime(2026, 10, 23, 12, 0, tzinfo=BEIJING)


@pytest.mark.parametrize("status,expected", [("OK", None), ("FAIL", SourceAuthRequired), (None, SourceAuthRequired)])
def test_grid_envelope_status_is_checked(status, expected):
    payload = {"course": GRID_COURSE_JSON["course"], "type": "course", "status": status}
    adapter = ProgrammingGridJsonAdapter(MockTransport(payload))
    if expected is None:
        assert adapter.fetch().trustworthy is True
    else:
        with pytest.raises(expected):
            adapter.fetch()


def test_non_ok_grid_status_is_never_treated_as_an_empty_course():
    adapter = ProgrammingGridJsonAdapter(MockTransport({"status": "FAIL", "course": {"problemlists": []}}))
    with pytest.raises(SourceAuthRequired) as error:
        adapter.fetch()
    assert "空课程" in str(error.value)


@pytest.mark.parametrize("payload", [[], "text", 5, None])
def test_grid_response_of_the_wrong_type_is_rejected(payload):
    with pytest.raises(SourceRejected):
        ProgrammingGridJsonAdapter(MockTransport(payload)).fetch()


@pytest.mark.parametrize("broken,reason", [
    ({"status": "OK"}, "course"),
    ({"status": "OK", "course": {"title": "虚拟课程"}}, "problemlists"),
    ({"status": "OK", "course": {"title": "虚拟课程", "problemlists": {}}}, "不是数组"),
])
def test_grid_missing_structure_is_rejected_not_read_as_empty(broken, reason):
    with pytest.raises(SourceRejected) as error:
        ProgrammingGridJsonAdapter(MockTransport(broken)).fetch()
    assert reason in str(error.value)


def test_grid_set_without_assignment_is_a_rejection_with_full_trust_lost():
    payload = {"status": "OK", "course": {"title": "虚拟课程", "problemlists": [
        {"id": "x1", "title": "虚拟无截止题集", "type": "Homework"},
        {"id": "x2", "title": "虚拟正常题集", "type": "Homework",
         "assignment": {"openTime": "2026-09-30 15:00:00.0", "closeTime": "2026-11-13 15:00:00.0"}},
    ]}}
    result = ProgrammingGridJsonAdapter(MockTransport(payload)).fetch()
    assert [item.source_id for item in result.records] == ["x2"]
    assert len(result.rejections) == 1 and "assignment" in result.rejections[0].reason
    assert result.complete is False and result.trustworthy is False


def test_grid_broken_deadline_does_not_become_a_missing_deadline():
    """An unreadable closeTime must not look like 'this set has no deadline'."""
    payload = {"status": "OK", "course": {"title": "虚拟课程", "problemlists": [
        {"id": "y1", "title": "虚拟题集", "type": "Homework",
         "assignment": {"openTime": "2026-09-30 15:00:00.0", "closeTime": "下周三"}},
    ]}}
    result = ProgrammingGridJsonAdapter(MockTransport(payload)).fetch()
    assert result.records == ()
    assert len(result.rejections) == 1
    assert result.trustworthy is False


def test_grid_empty_problem_list_is_warned_and_never_confirmed_empty():
    payload = {"status": "OK", "course": {"title": "虚拟空课程", "problemlists": []}}
    result = ProgrammingGridJsonAdapter(MockTransport(payload)).fetch()
    assert result.records == () and result.rejections == ()
    assert len(result.warnings) == 1 and "空题集列表" in result.warnings[0]
    assert result.trustworthy is False
    assert result.confirmed_empty is False


def test_grid_failures_propagate_like_the_other_adapters():
    adapter = ProgrammingGridJsonAdapter(MockTransport(GRID_COURSE_JSON, failure=_failure_auth))
    with pytest.raises(SourceAuthRequired):
        adapter.fetch()


def test_duplicate_registration_is_refused():
    with pytest.raises(ValueError):
        sources.register_adapter(MockTeachingPlatformAdapter)


# --------------------------------------------------------------- offline guarantee


def test_adapter_layer_imports_nothing_that_can_reach_the_network():
    banned = {"socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "ftplib", "smtplib"}
    assert banned.isdisjoint(set(sources.__dict__))
    assert banned.isdisjoint(set(sys.modules["pku_deadline_agent.mock_sources"].__dict__))


def test_contract_never_defines_a_field_for_a_credential():
    """Not "no string mentions cookies" but "there is nowhere to put one"."""
    from dataclasses import fields

    credential_like = ("password", "cookie", "token", "secret", "credential", "key")
    record_fields = {item.name for item in fields(SourceRecord)} | {item.name for item in fields(FetchResult)} | {item.name for item in fields(SourceCapabilities)}
    assert not any(word in name for name in record_fields for word in credential_like)


@pytest.mark.parametrize("option", ["password", "cookie", "token", "session", "api_key"])
def test_adapter_construction_refuses_credential_options(option):
    with pytest.raises(TypeError):
        create_adapter("teaching_platform", **{option: "would-be-secret"})


def test_adapters_hold_no_payload_or_credential_attribute():
    adapter = MockTeachingPlatformAdapter()
    attributes = set(vars(adapter))
    assert attributes == {"transport"}
    for banned in ("password", "cookie", "token", "credential", "session"):
        assert banned not in attributes
