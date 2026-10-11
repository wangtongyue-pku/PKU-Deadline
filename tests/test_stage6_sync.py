"""Stage 6A/6B tests: local JSON import, change detection, reviewed write.

Offline only. The programming-grid payload keeps the real structure confirmed on
2026-10-11 with synthetic course, teacher, title and id values.
"""

import json

import pytest

from pku_deadline_agent import sync
from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.mock_sources import GRID_COURSE_JSON, ProgrammingGridJsonAdapter
from pku_deadline_agent.sources import (
    FetchResult,
    Rejection,
    SourceRecord,
    SourceRejected,
    notice_key,
)
from pku_deadline_agent.sync import (
    DEADLINE_CHANGED,
    NEW,
    POSSIBLE_DUPLICATE,
    UNCHANGED,
    ImportFileError,
    apply_sync_plan,
    describe_plan,
    load_grid_file,
    parse_grid_payload,
    plan_sync,
)
from pku_deadline_agent.task_management import ImportDecision
from pku_deadline_agent.time_utils import BEIJING
from datetime import datetime


@pytest.fixture
def repo(tmp_path):
    result = TaskRepository(tmp_path / "tasks.sqlite3")
    result.initialize()
    return result


def write_payload(tmp_path, payload, name="grid.json"):
    target = tmp_path / name
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def course_payload(entries, title="虚拟课程 2026 虚拟老师"):
    return {"status": "OK", "type": "course", "course": {"title": title, "problemlists": list(entries)}}


def entry(identifier, title, close, open_="2026-09-30 15:00:00.0", type_="Homework"):
    return {"id": identifier, "title": title, "type": type_,
            "assignment": {"openTime": open_, "closeTime": close}}


def plan_for(repo, payload):
    result = parse_grid_payload(payload, ProgrammingGridJsonAdapter)
    return plan_sync(result.records, result, repo)


# --------------------------------------------------------------- file reading


def test_real_shaped_file_loads_and_parses(tmp_path):
    target = write_payload(tmp_path, GRID_COURSE_JSON)
    payload = load_grid_file(target)
    result = parse_grid_payload(payload, ProgrammingGridJsonAdapter)
    assert len(result.records) == 16
    assert result.trustworthy is True
    assert payload["status"] == "OK"


@pytest.mark.parametrize("content,reason", [
    ("", "空"),
    ("   ", "JSON"),
    ("{not json", "JSON"),
    ("[1, 2]", "对象"),
    ("\"text\"", "对象"),
])
def test_unusable_files_are_refused_with_a_reason(tmp_path, content, reason):
    target = tmp_path / "bad.json"
    target.write_text(content, encoding="utf-8")
    with pytest.raises(ImportFileError) as error:
        load_grid_file(target)
    assert reason in str(error.value)


def test_missing_file_is_refused_not_treated_as_empty(tmp_path):
    with pytest.raises(ImportFileError) as error:
        load_grid_file(tmp_path / "nope.json")
    assert "无法读取" in str(error.value)


def test_oversized_file_is_refused(tmp_path):
    target = tmp_path / "big.json"
    target.write_text("x" * (sync.MAX_IMPORT_BYTES + 1), encoding="utf-8")
    with pytest.raises(ImportFileError) as error:
        load_grid_file(target)
    assert "MB" in str(error.value)


def test_non_utf8_file_is_refused(tmp_path):
    """GBK bytes for Chinese text are not valid UTF-8 and must be refused."""
    target = tmp_path / "gbk.json"
    target.write_bytes(json.dumps({"status": "OK", "course": {"title": "虚拟课程"}}, ensure_ascii=False).encode("gbk"))
    with pytest.raises(ImportFileError) as error:
        load_grid_file(target)
    assert "UTF-8" in str(error.value)


def test_utf8_bom_is_accepted_and_stripped(tmp_path):
    target = tmp_path / "bom.json"
    target.write_bytes(b"\xef\xbb\xbf" + json.dumps(GRID_COURSE_JSON, ensure_ascii=False).encode("utf-8"))
    assert load_grid_file(target)["status"] == "OK"


def test_utf16_file_is_refused_with_a_specific_reason(tmp_path):
    target = tmp_path / "utf16.json"
    target.write_bytes(json.dumps({"status": "OK"}, ensure_ascii=False).encode("utf-16"))
    with pytest.raises(ImportFileError) as error:
        load_grid_file(target)
    assert "UTF-16" in str(error.value)


def test_loading_a_file_never_writes_to_it(tmp_path):
    target = write_payload(tmp_path, GRID_COURSE_JSON)
    before = target.read_bytes()
    load_grid_file(target)
    assert target.read_bytes() == before


# ------------------------------------------------ bytes path used by the uploader


def test_decode_grid_bytes_accepts_the_same_input_as_the_file_path(tmp_path):
    target = write_payload(tmp_path, GRID_COURSE_JSON)
    assert sync.decode_grid_bytes(target.read_bytes()) == load_grid_file(target)


@pytest.mark.parametrize("raw,reason", [
    (b"", "空"),
    (b"   ", "JSON"),
    (b"{not json", "JSON"),
    (b"[1,2]", "对象"),
    (b'{"a": 1}'.replace(b"1", b"\xff"), "UTF-8"),
    ("{\"title\": \"虚拟课程\"}".encode("utf-16"), "UTF-16"),
])
def test_decode_grid_bytes_refuses_unusable_content(raw, reason):
    with pytest.raises(ImportFileError) as error:
        sync.decode_grid_bytes(raw)
    assert reason in str(error.value)


def test_decode_grid_bytes_refuses_non_bytes_and_oversized_input():
    with pytest.raises(ImportFileError):
        sync.decode_grid_bytes("a string")
    with pytest.raises(ImportFileError) as error:
        sync.decode_grid_bytes(b"x" * (sync.MAX_IMPORT_BYTES + 1))
    assert "MB" in str(error.value)


# ------------------------------------------------------------------- planning


def test_first_import_proposes_every_set_as_new(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    assert plan.new_count == 16
    assert plan.change_count == 0 and plan.unchanged_count == 0
    assert plan.trustworthy is True
    assert plan.course == "虚拟课程 2026 虚拟老师"


def test_plan_carries_the_platform_identifier_not_the_title(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    source_keys = {item.source_key for item in plan.proposals}
    assert "programming_grid:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa16" in source_keys
    assert all(item.source_key.startswith("programming_grid:") for item in plan.proposals)
    # The database keeps only the hash, never the readable platform id.
    assert all(len(item.stored_key) == 64 for item in plan.proposals)
    assert all(item.stored_key != item.source_key for item in plan.proposals)


def test_second_identical_import_is_idempotent(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    assert len(repo.list_tasks(include_completed=True)) == 16
    again = plan_for(repo, GRID_COURSE_JSON)
    assert again.new_count == 0
    assert again.unchanged_count == 16
    apply_sync_plan(repo, again, [decision for _, decision in again.actions()], confirmed=True)
    assert len(repo.list_tasks(include_completed=True)) == 16


def test_applying_twice_without_mapping_is_prevented_by_the_plan(repo):
    """Even a repeated apply of the first plan cannot duplicate mapped sets."""
    plan = plan_for(repo, GRID_COURSE_JSON)
    apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    second = plan_for(repo, GRID_COURSE_JSON)
    assert all(decision.action == "skip" for _, decision in second.actions())
    apply_sync_plan(repo, second, [decision for _, decision in second.actions()], confirmed=True)
    assert len(repo.list_tasks(include_completed=True)) == 16


def test_deadline_change_is_detected_without_touching_the_task(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    moved = {"status": "OK", "type": "course", "course": {"title": "虚拟课程 2026 虚拟老师", "problemlists": [
        entry("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa14", "虚拟第一阶段练习（1）", "2026-10-05 14:00:00.0"),
    ]}}
    changed = plan_for(repo, moved)
    proposal = changed.proposals[0]
    assert proposal.kind == DEADLINE_CHANGED
    assert proposal.current_deadline == datetime(2026, 9, 27, 14, 0, tzinfo=BEIJING)
    assert proposal.record.deadline == datetime(2026, 10, 5, 14, 0, tzinfo=BEIJING)
    assert "延后" in proposal.reason
    # Nothing moved yet: planning is read-only.
    assert repo.get_task(proposal.task_id).due_at == datetime(2026, 9, 27, 14, 0, tzinfo=BEIJING)


def test_earlier_deadline_is_reported_as_advance(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    moved = {"status": "OK", "type": "course", "course": {"title": "虚拟课程", "problemlists": [
        entry("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa16", "虚拟试手练习（选做，不计入成绩）", "2026-12-01 15:00:00.0"),
    ]}}
    proposal = plan_for(repo, moved).proposals[0]
    assert proposal.kind == DEADLINE_CHANGED and "提前" in proposal.reason


def test_unlinked_set_with_similar_title_is_only_a_possible_duplicate(repo):
    repo.add_task("虚拟第一阶段练习（1）", "虚拟课程 2026 虚拟老师", "作业")
    plan = plan_for(repo, GRID_COURSE_JSON)
    duplicates = [item for item in plan.proposals if item.kind == POSSIBLE_DUPLICATE]
    assert [item.record.task_title for item in duplicates] == ["虚拟第一阶段练习（1）"]
    assert "可能重复" in duplicates[0].reason
    # A different platform id is never merged just because the title matches.
    assert duplicates[0].source_key == "programming_grid:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa14"


def test_set_missing_from_a_later_response_is_never_deleted(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    partial = {"status": "OK", "type": "course", "course": {"title": "虚拟课程 2026 虚拟老师", "problemlists": [
        entry("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa16", "虚拟试手练习（选做，不计入成绩）", "2027-01-07 15:00:00.0"),
    ]}}
    reduced = plan_for(repo, partial)
    assert reduced.new_count == 0 and reduced.change_count == 0
    assert len(repo.list_tasks(include_completed=True)) == 16
    assert all(item.kind == UNCHANGED for item in reduced.proposals)


def test_completed_task_is_not_reopened_and_keeps_its_state(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    task = repo.list_tasks(include_completed=True)[0]
    repo.set_completed(task.id, task.revision, True, confirmed=True)
    again = plan_for(repo, GRID_COURSE_JSON)
    still = repo.get_task(task.id)
    assert still.completed is True
    assert any(item.task_id == task.id for item in again.proposals)


# ------------------------------------------------- untrustworthy input is inert


@pytest.mark.parametrize("payload", [
    {"status": "OK", "course": {"title": "虚拟课程", "problemlists": []}},
    {"status": "OK", "course": {"title": "虚拟课程", "problemlists": [
        entry("z2", "虚拟坏题集", "下周三")]}},
])
def test_untrustworthy_batches_cannot_produce_a_plan(repo, payload):
    result = parse_grid_payload(payload, ProgrammingGridJsonAdapter)
    assert result.trustworthy is False
    assert result.rejections or result.warnings
    with pytest.raises(Exception):
        plan_sync(result.records, result, repo)


def test_plan_refuses_a_batch_with_rejections(repo):
    payload = course_payload([
        entry("z1", "虚拟正常题集", "2026-11-13 15:00:00.0"),
        entry("z2", "虚拟坏题集", "下周三"),
    ])
    result = parse_grid_payload(payload, ProgrammingGridJsonAdapter)
    assert result.trustworthy is False and len(result.rejections) == 1
    with pytest.raises(Exception) as error:
        plan_sync(result.records, result, repo)
    assert "无法确认" in str(error.value)


def test_a_partial_response_still_reports_the_bad_entry_without_writing(repo):
    payload = course_payload([
        entry("z1", "虚拟正常题集", "2026-11-13 15:00:00.0"),
        entry("z2", "虚拟坏题集", "下周三"),
    ])
    result = parse_grid_payload(payload, ProgrammingGridJsonAdapter)
    # The good record survives, and the failure is named rather than dropped.
    assert [item.source_id for item in result.records] == ["z1"]
    assert result.rejections[0].reason
    assert repo.list_tasks(include_completed=True) == []


def test_plan_cannot_be_built_from_a_mismatched_source(repo):
    result = FetchResult(source="teaching_platform", records=(), fetched_at=datetime.now(BEIJING))
    with pytest.raises(SourceRejected):
        plan_sync((), result, repo, source="programming_grid")


def test_apply_requires_confirmation_and_trust(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    decisions = [decision for _, decision in plan.actions()]
    with pytest.raises(ValueError):
        apply_sync_plan(repo, plan, decisions, confirmed=False)
    with pytest.raises(ValueError):
        apply_sync_plan(repo, plan, decisions[:-1], confirmed=True)
    assert repo.list_tasks(include_completed=True) == []


def test_plan_of_wrong_length_cannot_be_applied(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    with pytest.raises(ValueError) as error:
        apply_sync_plan(repo, plan, [], confirmed=True)
    assert "数量不一致" in str(error.value)


# ------------------------------------------------------------------ writing


def test_confirmed_apply_writes_tasks_and_history(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    created = apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    assert len(created) == 16
    assert all(task.source == "programming_grid" for task in created)
    assert repo.list_tasks()[0].due_at is not None
    history = repo.history(created[0].id)
    assert history and history[0]["source"] == "programming_grid"


def test_confirmed_deadline_update_records_old_and_new(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    moved = {"status": "OK", "type": "course", "course": {"title": "虚拟课程 2026 虚拟老师", "problemlists": [
        entry("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa14", "虚拟第一阶段练习（1）", "2026-10-05 14:00:00.0"),
    ]}}
    changed = plan_for(repo, moved)
    apply_sync_plan(repo, changed, [decision for _, decision in changed.actions()], confirmed=True)
    task = repo.get_task(changed.proposals[0].task_id)
    assert task.due_at == datetime(2026, 10, 5, 14, 0, tzinfo=BEIJING)
    latest = repo.history(task.id)[0]
    assert latest["old_due_at"] == "2026-09-27T14:00:00+08:00"
    assert latest["new_due_at"] == "2026-10-05T14:00:00+08:00"
    assert latest["action"] == "postpone"


def test_skipped_proposals_write_nothing(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    skipped = [ImportDecision("skip", reason="本次忽略") for _ in plan.proposals]
    assert apply_sync_plan(repo, plan, skipped, confirmed=True) == []
    assert repo.list_tasks(include_completed=True) == []
    assert repo.source_mappings("programming_grid") == {}


def test_platform_identifier_is_persisted_for_the_next_sync(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    mappings = repo.source_mappings("programming_grid")
    assert len(mappings) == 16
    # Stored as hashes, and each maps back to the task created for that record.
    assert set(mappings) == {item.stored_key for item in plan.proposals}


def test_two_sets_with_identical_titles_import_as_separate_tasks(repo):
    """Observed in the real course: two "期中试题" with the same deadline."""
    payload = course_payload([
        entry("t1", "虚拟题集：期中试题（新）", "2026-11-13 15:00:00.0"),
        entry("t2", "虚拟题集：期中试题（旧）", "2026-11-13 15:00:00.0"),
    ])
    plan = plan_for(repo, payload)
    assert plan.new_count == 2
    created = apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    assert len(created) == 2
    assert len({task.id for task in created}) == 2


def test_same_platform_id_twice_in_one_response_aborts_the_import(repo):
    payload = course_payload([
        entry("same", "虚拟题集甲", "2026-11-13 15:00:00.0"),
        entry("same", "虚拟题集乙", "2026-11-14 15:00:00.0"),
    ])
    result = parse_grid_payload(payload, ProgrammingGridJsonAdapter)
    with pytest.raises(SourceRejected) as error:
        plan_sync(result.records, result, repo)
    assert "重复的平台标识" in str(error.value)
    assert repo.list_tasks(include_completed=True) == []


@pytest.mark.parametrize("platform_value,expected,unmapped", [
    ("Homework", "作业", None),
    ("Exam", "考试", None),
    ("活动报名", "活动报名", None),
    ("作业", "作业", None),
    ("Practice", "其他", "Practice"),
    ("", "其他", None),
    (None, "其他", None),
])
def test_platform_task_type_maps_into_the_local_enum(platform_value, expected, unmapped):
    local, leftover = sync.map_task_type(platform_value)
    assert local == expected and leftover == unmapped
    assert local in ("作业", "考试", "活动报名", "其他")


def test_unmapped_task_type_is_recorded_in_review_notes(repo):
    payload = course_payload([entry("x1", "虚拟题集", "2026-11-13 15:00:00.0", type_="Practice")])
    plan = plan_for(repo, payload)
    created = apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    assert created[0].task_type == "其他"
    assert any("Practice" in note for note in sync.Candidate(plan.proposals[0].record).review_notes)


def test_grid_tasks_are_saved_with_a_valid_local_type(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    created = apply_sync_plan(repo, plan, [decision for _, decision in plan.actions()], confirmed=True)
    assert {task.task_type for task in created} == {"作业"}


def test_duplicate_platform_keys_are_rejected_by_the_repository(repo):
    """Two tasks must never claim the same platform object."""
    from pku_deadline_agent.task_management import NoticeOrigin

    task = repo.add_task("虚拟题集", "虚拟课程", "作业")
    other = repo.add_task("虚拟题集二", "虚拟课程", "作业")
    key = notice_key("programming_grid:dupe1")
    for target in (task, other):
        repo.apply_reviewed_tasks(
            (_candidate(target.title, "虚拟课程"),),
            (ImportDecision("link", target_id=target.id, expected_revision=target.revision, reason="关联"),),
            (NoticeOrigin("programming_grid", key),),
            confirmed=True,
        )
    with pytest.raises(ValueError) as error:
        repo.source_mappings("programming_grid")
    assert "多个任务" in str(error.value)


class _candidate:
    def __init__(self, title, category):
        self.title = title
        self.category = category
        self.task_type = "作业"
        self.due_at = None
        self.deadline_changed = False
        self.source_index = 1
        self.review_notes = ()
        self.deadline_text = None


def test_source_mappings_rejects_an_unknown_source(repo):
    with pytest.raises(ValueError):
        repo.source_mappings("not_a_source")


def test_describe_plan_summarises_without_leaking_titles(repo):
    plan = plan_for(repo, GRID_COURSE_JSON)
    text = describe_plan(plan)
    assert "新题集 16" in text
    assert "虚拟" not in text
    assert "aaaa" not in text
