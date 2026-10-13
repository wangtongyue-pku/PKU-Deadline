import json
import socket
import sqlite3
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from pku_deadline_agent.ai_config import AISettings, load_settings
from pku_deadline_agent.database import TaskConflict, TaskRepository
from pku_deadline_agent.parse_service import ParseSession
from pku_deadline_agent.time_utils import BEIJING
from pku_deadline_agent.usage import UsageLedger

PUB = datetime(2026, 10, 11, 9, tzinfo=BEIJING)
SIMPLE = "《虚构课程》第三次作业延期至下周三晚20:00，请在教学平台提交。"
COMPLEX = "虚构校园探索活动明晚八点截止报名。"


@pytest.fixture
def setup(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite3")
    repository.initialize()
    return ParseSession(), UsageLedger(tmp_path / "usage.sqlite3"), repository


def run(session, ledger, raw=COMPLEX, pub=PUB, settings=None, **kwargs):
    settings = settings or load_settings("DeepSeek")
    session.prepare(raw, pub, "natural", settings)
    return session.run(raw, pub, "natural", settings, ledger, confirmed=True, **kwargs)


def test_local_clear_task_skips_all_clients_and_usage(setup):
    session, ledger, repo = setup
    tasks = run(session, ledger, SIMPLE)
    assert session.plan.route == "local"
    assert session.plan.request_json is None
    assert not session.mock.requests
    assert tasks[0].due_at.day == 14
    assert tasks[0].deadline_changed
    assert not repo.list_tasks()
    assert ledger.summary()["calls"] == 0


def test_mock_send_is_exact_preview_redacted_and_never_networks(setup, monkeypatch):
    session, ledger, repo = setup
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("unexpected network"))
    raw = COMPLEX + "\n联系人:张小明，微信号:fictional_wx，电话:13800138000。\n今晚喝奶茶？"
    settings = load_settings("MiMo")
    plan = session.prepare(raw, PUB, "natural", settings)
    assert plan.route == "mock"
    assert "max_completion_tokens" in plan.request_json
    with pytest.raises(ValueError, match="确认"):
        session.run(raw, PUB, "natural", settings, ledger, confirmed=False)
    session.run(raw, PUB, "natural", settings, ledger, confirmed=True)
    assert session.mock.requests == [plan.request_json]
    assert all(value not in plan.request_json for value in ("张小明", "fictional_wx", "13800138000", "奶茶"))
    assert not repo.list_tasks()
    assert ledger.summary()["charged_micros"] == 0


@pytest.mark.parametrize("raw", [COMPLEX + "\nCookie:fake-only", "请私聊我报名，明晚截止。"])
def test_privacy_block_cannot_be_approved_into_mock_or_live(setup, raw):
    session, ledger, _ = setup
    settings = load_settings("DeepSeek")
    session.prepare(raw, PUB, "natural", settings)
    with pytest.raises(ValueError, match="阻断"):
        session.run(raw, PUB, "natural", settings, ledger, confirmed=True, real_confirmed=True)
    assert not session.mock.requests
    assert session.results is None


@pytest.mark.parametrize("change", ["raw", "mode", "publication", "provider", "model", "price", "key", "url"])
def test_any_context_change_invalidates_approval(setup, change):
    session, ledger, _ = setup
    settings = load_settings("DeepSeek")
    session.prepare(COMPLEX, PUB, "natural", settings)
    raw, pub, mode = COMPLEX, PUB, "natural"
    if change == "raw": raw += "\n补充通知。"
    if change == "mode": mode = "structured"
    if change == "publication": pub = PUB.replace(day=10)
    if change == "provider": settings = load_settings("MiMo")
    if change == "model": settings = replace(settings, model="deepseek-v4-pro")
    if change == "price": settings = replace(settings, input_price=Decimal("3"))
    if change == "key": settings = replace(settings, api_key="fake-only")
    if change == "url": settings = replace(settings, base_url="https://example.invalid")
    with pytest.raises(ValueError, match="预览"):
        session.run(raw, pub, mode, settings, ledger, confirmed=True)
    assert session.plan is None
    assert not session.mock.requests


def test_tampered_preview_is_rejected(setup):
    session, ledger, _ = setup
    settings = load_settings("DeepSeek")
    plan = session.prepare(COMPLEX, PUB, "natural", settings)
    session.plan = replace(plan, request_json='{"model":"fake"}')
    with pytest.raises(ValueError, match="校验"):
        session.run(COMPLEX, PUB, "natural", settings, ledger, confirmed=True)
    assert not session.mock.requests


def test_save_is_separate_confirmed_action_with_metadata_and_no_overwrite(setup):
    session, ledger, repo = setup
    settings = load_settings("DeepSeek")
    tasks = run(session, ledger, SIMPLE, settings=settings)
    with pytest.raises(ValueError, match="确认保存"):
        session.save(repo, SIMPLE, PUB, "natural", settings, tasks, confirmed=False)
    assert not repo.list_tasks()
    saved = session.save(repo, SIMPLE, PUB, "natural", settings, tasks, confirmed=True)
    assert saved[0].source == "wechat"
    assert saved[0].due_at.day == 14
    with sqlite3.connect(repo.path) as connection:
        metadata = connection.execute("SELECT deadline_changed,deadline_text FROM task_extractions").fetchone()
    assert metadata == (1, "下周三晚20:00")
    with pytest.raises(ValueError, match="失效或已保存"):
        session.save(repo, SIMPLE, PUB, "natural", settings, tasks, confirmed=True)
    assert len(repo.list_tasks()) == 1


def test_conflict_is_atomic_even_if_new_task_precedes_duplicate(setup):
    session, ledger, repo = setup
    tasks = run(session, ledger, SIMPLE)
    existing = repo.add_task(tasks[0].title, category=tasks[0].category)
    with pytest.raises(TaskConflict) as exc:
        repo.add_reviewed_tasks((replace(tasks[0], title="新任务", category="另一课程", deadline_changed=False), tasks[0]))
    assert exc.value.task_ids == (existing.id,)
    assert repo.list_tasks() == [existing]


def test_change_in_same_course_requires_manual_check_even_with_new_title(setup):
    session, ledger, repo = setup
    tasks = run(session, ledger, SIMPLE)
    existing = repo.add_task("旧版任务名称", category="虚构课程")
    with pytest.raises(TaskConflict):
        repo.add_reviewed_tasks(tasks)
    assert repo.list_tasks() == [existing]


def test_selection_and_manual_correction_still_require_final_review(setup):
    session, ledger, repo = setup
    tasks = run(session, ledger, SIMPLE)
    corrected = replace(tasks[0], title="核对后的虚构练习", due_at=None)
    session.save(repo, SIMPLE, PUB, "natural", load_settings("DeepSeek"), (corrected,), confirmed=True)
    assert repo.list_tasks()[0].needs_confirmation


def test_new_private_content_cannot_be_saved_after_model_review(setup):
    session, ledger, repo = setup
    tasks = run(session, ledger, SIMPLE)
    with pytest.raises(ValueError, match="疑似个人信息"):
        session.save(repo, SIMPLE, PUB, "natural", load_settings("DeepSeek"), (replace(tasks[0], title="任务 13800138000"),), confirmed=True)
    assert not repo.list_tasks()


def live_settings(**changes):
    base = AISettings("DeepSeek", "deepseek-flash", True, "https://api.deepseek.com", "fictional-only", Decimal("2"), Decimal("8"), Decimal("1"), 2048, 5, True)
    return replace(base, **changes)


class FakeLive:
    def __init__(self, fail=False, **changes):
        self.calls = []
        self.fail = fail
        self.changes = changes

    def complete(self, body, settings, notices):
        self.calls.append(body)
        if self.fail:
            raise TimeoutError("DO-NOT-LEAK-fictional-secret")
        result = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"tasks": [{"source_index": 1, "title": "虚构活动报名", "category": "虚构校园", "task_type": "活动报名", "deadline_text": "明晚八点", "due_at": "2026-10-12T20:00:00+08:00", "deadline_changed": False, "review_notes": []}]}, ensure_ascii=False)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        result.update(self.changes)
        return result


def test_live_requires_second_explicit_approval_before_transport(setup):
    session, ledger, _ = setup
    settings, fake = live_settings(), FakeLive()
    session.prepare(COMPLEX, PUB, "natural", settings)
    with pytest.raises(ValueError, match="单独明确授权"):
        session.run(COMPLEX, PUB, "natural", settings, ledger, confirmed=True, client=fake)
    assert not fake.calls
    assert ledger.summary()["calls"] == 0
    session.run(COMPLEX, PUB, "natural", settings, ledger, confirmed=True, real_confirmed=True, client=fake)
    assert fake.calls == [session.plan.request_json]
    assert ledger.summary()["prompt_tokens"] == 100
    assert ledger.summary()["charged_micros"] == 600


def test_timeout_preserves_reservation_consumes_confirmation_and_hides_secret(setup):
    session, ledger, _ = setup
    settings, fake = live_settings(), FakeLive(fail=True)
    session.prepare(COMPLEX, PUB, "natural", settings)
    with pytest.raises(ValueError, match="预算保留") as exc:
        session.run(COMPLEX, PUB, "natural", settings, ledger, confirmed=True, real_confirmed=True, client=fake)
    assert "DO-NOT-LEAK" not in str(exc.value)
    assert ledger.summary()["charged_micros"] == session.plan.reserve_micros
    assert ledger.summary()["uncertain"] == 1
    with pytest.raises(ValueError):
        session.run(COMPLEX, PUB, "natural", settings, ledger, confirmed=True, real_confirmed=True, client=fake)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("bad_response", [
    {"usage": None}, {"usage": {"prompt_tokens": True, "completion_tokens": 0}},
    {"choices": []}, {"choices": [{"finish_reason": "length"}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "not-json"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"tool_calls": [{"command": "run"}], "content": "{}"}}]},
], ids=["no-usage", "bad-usage", "no-choices", "truncated", "bad-json", "tools"])
def test_invalid_responses_never_save_and_record_or_reserve_usage(setup, bad_response):
    session, ledger, repo = setup
    with pytest.raises(ValueError):
        run(session, ledger, settings=live_settings(), client=FakeLive(**bad_response), real_confirmed=True)
    assert session.results is None
    assert not repo.list_tasks()
    assert ledger.summary()["charged_micros"] > 0


def test_daily_limit_prevents_transport_and_survives_ledger_reopen(setup):
    session, ledger, _ = setup
    fake = FakeLive()
    with pytest.raises(ValueError, match="每日费用上限"):
        run(session, ledger, settings=live_settings(daily_limit=Decimal("0.00001")), client=fake, real_confirmed=True)
    assert not fake.calls
    settings = live_settings()
    ledger.reserve(settings, 1000)
    reopened = UsageLedger(ledger.path)
    with pytest.raises(ValueError, match="每日费用上限"):
        reopened.reserve(replace(settings, daily_limit=Decimal("0.02")), 1000)


def test_missing_publication_stays_pending_in_mock_and_can_save(setup):
    session, ledger, repo = setup
    tasks = run(session, ledger, SIMPLE, pub=None)
    assert tasks[0].due_at is None
    assert tasks[0].review_notes
    assert not session.mock.requests  # AI cannot invent the missing publication.
    session.save(repo, SIMPLE, None, "natural", load_settings("DeepSeek"), tasks, confirmed=True)
    assert repo.list_tasks()[0].needs_confirmation


def test_only_unresolved_notices_enter_request(setup):
    session, ledger, _ = setup
    raw = SIMPLE + "\n---\n" + COMPLEX
    tasks = run(session, ledger, raw)
    content = json.loads(json.loads(session.plan.request_json)["messages"][1]["content"])
    assert [notice["source_index"] for notice in content["notices"]] == [2]
    assert [task.source_index for task in tasks] == [1, 2]


def test_v1_database_migration_preserves_old_tasks(tmp_path):
    path = tmp_path / "v1.sqlite3"
    repo = TaskRepository(path)
    repo.initialize()
    original = repo.add_task("旧版虚构任务")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE task_extractions")
        connection.execute("PRAGMA user_version=1")
    repo.initialize()
    assert repo.list_tasks() == [original]
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
