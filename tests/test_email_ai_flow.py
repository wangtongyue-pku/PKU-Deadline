"""Mail AI shares the real privacy/budget gateway; every transport here is fake."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pku_deadline_agent.ai_config import load_settings
from pku_deadline_agent.database import TaskRepository
from pku_deadline_agent.email_ai import prepare_email_input
from pku_deadline_agent.email_credentials import SessionCredentialStore, MailCredentials
from pku_deadline_agent.email_intel import analyze_email
from pku_deadline_agent.email_models import RawEmailRecord
from pku_deadline_agent.email_page import EmailPageDependencies, MODE_REAL
from pku_deadline_agent.email_sync import EmailSyncReport, MailboxStateStore
from pku_deadline_agent.parse_service import ParseSession
from pku_deadline_agent.task_management import ImportDecision
from pku_deadline_agent.time_utils import BEIJING
from pku_deadline_agent.usage import UsageLedger

APP = Path(__file__).resolve().parents[1] / "app.py"
SENT = datetime(2026, 10, 10, 9, tzinfo=BEIJING)


def message(body="虚构探索活动明晚八点截止报名。\n联系人:张小明，电话:13800138000。", uid=1):
    return RawEmailRecord(uid, "INBOX", 1, f"<fictional-{uid}@example.invalid>", "fictional-sender@example.invalid", ("fictional-recipient@example.invalid",), "虚构探索报名通知", SENT, body, False, True, 1000)


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("PKU_REAL_API_ENABLED", "1")
    monkeypatch.setenv("PKU_DEEPSEEK_API_KEY", "FICTIONAL_ONLY_KEY")
    return load_settings("DeepSeek", live=True)


class Transport:
    def __init__(self): self.requests = []
    def complete(self, request, settings, notices):
        self.requests.append(request)
        return {"usage": {"prompt_tokens": 100, "completion_tokens": 50}, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"tasks": [{"source_index": 1, "title": "虚构探索活动报名", "category": "虚构探索", "task_type": "活动报名", "deadline_text": "明晚八点", "due_at": "2026-10-11T20:00:00+08:00", "deadline_changed": False, "review_notes": []}]}, ensure_ascii=False)}}]}


def test_mail_cannot_call_before_approval_or_upload_headers_contacts(settings, tmp_path):
    context = prepare_email_input((analyze_email(message()),))
    session = ParseSession()
    plan = session.prepare(context.raw, context.reference, "natural", settings)
    assert plan.route == "live"
    assert all(value not in plan.request_json for value in ("张小明", "13800138000", "fictional-sender", "fictional-recipient", "fictional-1@", "FICTIONAL_ONLY_KEY"))
    assert "2026-10-10T09:00:00+08:00" in plan.request_json
    transport = Transport()
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    with pytest.raises(ValueError): session.run(context.raw, context.reference, "natural", settings, ledger, confirmed=False, client=transport)
    assert not transport.requests
    tasks = session.run(context.raw, context.reference, "natural", settings, ledger, confirmed=True, real_confirmed=True, client=transport)
    assert transport.requests == [plan.request_json]
    assert tasks[0].due_at == datetime(2026, 10, 11, 20, tzinfo=BEIJING)
    with pytest.raises(ValueError): session.run(context.raw, context.reference, "natural", settings, ledger, confirmed=True, real_confirmed=True, client=transport)
    assert len(transport.requests) == 1


@pytest.mark.parametrize("credential", ["Token:fake-only", "Cookie:session=fake", "password=fictional"])
def test_credentials_in_mail_block_all_external_upload(settings, credential):
    context = prepare_email_input((analyze_email(message("虚构活动明晚八点截止报名。\n" + credential)),))
    plan = ParseSession().prepare(context.raw, context.reference, "natural", settings)
    assert plan.route == "blocked" and plan.request_json is None


def test_truncated_or_oversize_mail_requires_smaller_selection():
    with pytest.raises(ValueError): prepare_email_input((analyze_email(replace(message(), body_truncated=True)),))
    with pytest.raises(ValueError): prepare_email_input((analyze_email(replace(message(), body_text="x" * 20001)),))


def test_date_only_email_is_never_saved_as_invented_midnight(tmp_path):
    from pku_deadline_agent.email_import import plan_email_import
    analysis = analyze_email(message("作业提交截止2026年11月20日。"))
    assert analysis.deadlines and analysis.deadlines[0].due_at is None
    assert analysis.deadlines[0].needs_confirmation
    report = EmailSyncReport("INBOX", "initial", 1, 1, (analysis,), False)
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    plan = plan_email_import(report, repo)
    assert not plan.proposals and plan.undated


def test_explicit_email_date_without_date_header_uses_beijing():
    analysis = analyze_email(replace(message("作业提交截止2026年11月20日15:00。"), sent_at=None))
    assert analysis.deadlines[0].due_at == datetime(2026, 11, 20, 15, tzinfo=BEIJING)


def test_ai_registration_identity_aligns_with_local_parser_event_before_deadline(tmp_path):
    from pku_deadline_agent.email_import import plan_email_import
    from pku_deadline_agent.extraction import ExtractedTask
    analysis = analyze_email(message("讲座将于2026年11月20日14:00举行。报名截止2026年11月13日15:00。"))
    report = EmailSyncReport("INBOX", "initial", 1, 1, (analysis,), False)
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    local = plan_email_import(report, repo)
    assert len(local.proposals) == 1 and len(local.event_only) == 1
    candidate = ExtractedTask(1, "虚构讲座报名", "虚构讲座", "活动报名", "2026年11月13日15:00", datetime(2026, 11, 13, 15, tzinfo=BEIJING), False, ())
    origin = prepare_email_input((analysis,)).origin(candidate, 1)
    assert origin.notice_hash == local.proposals[0].stored_key


def test_body_separator_cannot_change_mail_source_identity(settings):
    context = prepare_email_input((analyze_email(message("虚构活动报名明晚八点截止。\n---\n虚构材料明晚八点截止。")),))
    plan = ParseSession().prepare(context.raw, context.reference, "natural", settings)
    assert len(plan.batch.notices) == 1


def test_same_mail_ai_rewording_needs_update_not_duplicate_insert(settings, tmp_path):
    context = prepare_email_input((analyze_email(message()),))
    session = ParseSession()
    session.prepare(context.raw, context.reference, "natural", settings)
    tasks = session.run(context.raw, context.reference, "natural", settings, UsageLedger(tmp_path / "usage.sqlite3"), confirmed=True, real_confirmed=True, client=Transport())
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    origin = context.origin(tasks[0], 1)
    created = session.save(repo, context.raw, context.reference, "natural", settings, tasks, confirmed=True, decisions=(ImportDecision("new", allow_distinct=True),), origins=(origin,))[0]
    changed = replace(tasks[0], title="另一个模型改写的活动标题")
    assert repo.suggestions(changed, origin)[0].task.id == created.id
    with pytest.raises(ValueError): repo.apply_reviewed_tasks((changed,), (ImportDecision("new", allow_distinct=True),), (origin,), confirmed=True)
    assert len(repo.list_tasks()) == 1
    assert repo.history(created.id)[0]["source"] == "email"


def test_mail_page_runs_ai_and_saves_in_place(settings, tmp_path, monkeypatch):
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    monkeypatch.setenv("PKU_DATABASE_PATH", str(repo.path))
    credentials = SessionCredentialStore()
    credentials.save(MailCredentials("fictional@stu.pku.edu.cn", "FICTIONAL_AUTH"))
    dependency = EmailPageDependencies(MODE_REAL, lambda: None, credentials, MailboxStateStore(), "fictional")
    monkeypatch.setattr("pku_deadline_agent.email_page.build_dependencies", lambda mode: dependency)
    transport = Transport()
    monkeypatch.setattr("pku_deadline_agent.ai_client.HTTPSClient.complete", lambda client, *args: transport.complete(*args))
    report = EmailSyncReport("INBOX", "initial", 1, 1, (analyze_email(message()),), False)
    app = AppTest.from_file(APP)
    app.session_state["page"] = "学校邮箱"
    app.session_state["email_report"] = report
    app.run()
    app.toggle(key="email_use_ai").set_value(True).run()
    options = app.multiselect(key="email_ai_selection").options
    app.multiselect(key="email_ai_selection").set_value([options[0]]).run()
    app.button(key="email_ai_prepare").click().run()
    assert not transport.requests
    shown = app.session_state["email_ai_session"].plan.request_json
    app.button(key="email_ai_run").click().run()
    assert not app.exception
    assert transport.requests == [shown]
    assert not repo.list_tasks()
    app.button(key="email_ai_save").click().run()
    assert not app.exception
    assert len(repo.list_tasks()) == 1 and repo.list_tasks()[0].source == "email"
    assert repo.source_mappings("email")
    app.radio(key="page").set_value("手动待办").run()
    assert "email_ai_session" not in app.session_state
