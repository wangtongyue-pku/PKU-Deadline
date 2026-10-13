"""Stage 7D offline tests for the mail -> task import planner.

The planner is the part that decides what the user is offered. Everything here
runs on fictional mail: no network, no credentials, no mailbox.
"""

from datetime import datetime, timedelta
from itertools import count

import pytest

from pku_deadline_agent.email_adapter import DEFAULT_FOLDER, MockEmailAdapter
from pku_deadline_agent.email_import import (
    EMAIL_SOURCE,
    EmailNotTrustworthy,
    apply_email_plan,
    default_decisions,
    map_email_task_type,
    plan_email_import,
    sender_label,
    task_title,
)
from pku_deadline_agent.email_intel import analyze_email
from pku_deadline_agent.email_sync import EmailSyncReport, EmailSyncService
from pku_deadline_agent.database import SCHEMA_VERSION, TaskRepository
from pku_deadline_agent.sync import DEADLINE_CHANGED, NEW, POSSIBLE_DUPLICATE, UNCHANGED
from pku_deadline_agent.task_management import ImportDecision
from pku_deadline_agent.time_utils import BEIJING
from tests.test_stage7_email import make_email

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=BEIJING)


@pytest.fixture
def repo(tmp_path):
    result = TaskRepository(tmp_path / "tasks.sqlite3")
    result.initialize()
    return result


def mailbox(*messages, uidvalidity=1):
    """A mock INBOX whose messages keep the given UIDs."""
    return MockEmailAdapter({DEFAULT_FOLDER: dict(messages)}, uidvalidity=uidvalidity)


_MESSAGE_IDS = count(1)


def raw(subject, body, sender="教务处 <jwc@pku.edu.cn>", date=None, message_id=None):
    """One fictional message. Each call gets its own Message-ID so two messages
    are genuinely two messages — the whole point of identity-by-message."""
    return make_email(subject=subject, body=body, sender=sender, date=date or NOW,
                      message_id=message_id or f"<fictional-{next(_MESSAGE_IDS)}@pku.edu.cn>")


def truncated_raw(subject, body, sender="教务处 <jwc@pku.edu.cn>"):
    """A message whose body is cut at the parser's size limit.

    Built by hand (rather than with make_email) so the encoding never degrades to
    base64 and so the truncation warning reports a real truncation.
    """
    head = (f"From: {sender}\r\nTo: student@stu.pku.edu.cn\r\nSubject: {subject}\r\n"
            f"Date: Mon, 01 Nov 2026 09:00:00 +0800\r\n"
            f"Message-ID: <fictional-truncated@example.invalid>\r\n"
            "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=\"utf-8\"\r\n"
            "Content-Transfer-Encoding: 8bit\r\n\r\n")
    return (head + body).encode("utf-8")


def report_for(adapter, *, state_store=None):
    service = EmailSyncService(lambda: adapter, state_store=state_store)
    return service.sync()


def plan_for(repo, adapter):
    return plan_email_import(report_for(adapter), repo)


def kinds(plan):
    return {item.kind for item in plan.proposals}


# ------------------------------------------------------------- pure mapping


def test_deadline_kind_maps_to_the_task_type_the_database_accepts():
    assert map_email_task_type("报名截止") == ("活动报名", None)
    assert map_email_task_type("课程作业截止") == ("作业", None)
    assert map_email_task_type("材料提交截止") == ("其他", None)
    assert map_email_task_type("任务截止") == ("其他", None)


def test_unknown_kind_falls_back_and_keeps_its_original_wording():
    """ Requirement: an unrecognised kind lands in 其他, never silently dropped."""
    assert map_email_task_type("突然出现的类型") == ("其他", "突然出现的类型")


def test_sender_label_prefers_the_display_name_then_the_domain():
    assert sender_label("jwc@pku.edu.cn", "教务处") == "教务处"
    assert sender_label("教务处 <jwc@pku.edu.cn>") == "教务处"
    assert sender_label("club@pku.edu.cn") == "pku.edu.cn"
    assert sender_label("") == "学校邮箱"


def test_title_names_the_kind_only_when_one_message_has_several_dates():
    assert task_title("活动", "报名截止", multiple=False) == "活动"
    assert task_title("活动", "报名截止", multiple=True) == "活动（报名截止）"
    assert task_title("", "报名截止", multiple=False) == "(无主题邮件)"


# ------------------------------------------------------------------ planning


def test_a_new_deadline_becomes_one_new_proposal(repo):
    plan = plan_for(repo, mailbox((1, raw("奖学金申请", "请于2026年12月10日前提交申请材料。"))))
    assert kinds(plan) == {NEW}
    proposal = plan.proposals[0]
    assert proposal.record.source == EMAIL_SOURCE
    assert proposal.record.deadline == datetime(2026, 12, 10, 0, 0, tzinfo=BEIJING)
    assert proposal.record.course == "教务处"
    assert plan.importable_count == 1


def test_one_message_with_two_deadlines_separates_event_from_deadline(repo):
    plan = plan_for(repo, mailbox(
        (1, raw("讲座通知", "报名截止2026年11月13日15:00，讲座将于2026年11月20日举行。")),
    ))
    # One importable deadline; the event time is shown, never auto-imported.
    assert kinds(plan) == {NEW}
    assert len(plan.proposals) == 1
    assert plan.event_only[0].kind == "活动举办时间"
    assert plan.proposals[0].record.task_title == "讲座通知（报名截止）"


def test_two_importable_dates_in_one_message_get_distinguishable_titles(repo):
    plan = plan_for(repo, mailbox(
        (1, raw("综合通知", "报名截止2026年11月13日15:00，作业提交截止2026年11月20日。")),
    ))
    assert len(plan.proposals) == 2
    titles = sorted(item.record.task_title for item in plan.proposals)
    assert titles == ["综合通知（报名截止）", "综合通知（课程作业截止）"]


def test_activity_time_alone_never_creates_a_task(repo):
    plan = plan_for(repo, mailbox((1, raw("讲座", "讲座将于2026年11月20日举行。"))))
    assert plan.proposals == ()
    assert len(plan.event_only) == 1
    assert plan.importable_count == 0


def test_undated_time_is_reported_but_never_imported(repo):
    """A cue without a usable date stays 待确认: shown for review, never imported."""
    plan = plan_for(repo, mailbox((1, raw("截止通知", "作业提交截止，具体日期待定，将另行通知。"))))
    assert plan.proposals == ()
    assert len(plan.undated) == 1
    assert plan.undated[0].kind == "待确认时间"
    assert plan.undated[0].uid == 1


def test_a_publication_date_is_not_even_a_review_item(repo):
    """「发布于」 is a fact about the mail, so it is neither a task nor 待确认."""
    plan = plan_for(repo, mailbox((1, raw("活动", "本通知发布于11月20日。"))))
    assert plan.proposals == ()
    assert plan.undated == ()
    assert plan.event_only == ()


def test_truncated_body_is_skipped_and_never_imported(repo):
    """A body cut at the size limit proves nothing, so nothing is imported from it."""
    plan = plan_for(repo, mailbox((1, truncated_raw("长通知", "截止2026年12月1日。" + "正文内容" * 6000))))
    assert plan.proposals == ()
    assert len(plan.skipped) == 1
    assert plan.skipped[0].uid == 1
    assert "截断" in plan.skipped[0].reasons[0]
    # A single truncated message does not stop the rest of the batch.
    mixed = plan_for(repo, mailbox(
        (1, truncated_raw("长通知", "截止2026年12月1日。" + "正文内容" * 6000)),
        (2, raw("作业", "作业提交截止2026-12-05。")),
    ))
    assert len(mixed.skipped) == 1
    assert len(mixed.proposals) == 1


def test_plain_information_mail_produces_no_proposal(repo):
    plan = plan_for(repo, mailbox((1, raw("校园生活小贴士", "图书馆开放时间调整。"))))
    assert plan.proposals == ()
    assert plan.importable_count == 0


# --------------------------------------------------------------- idempotency


def test_re_syncing_the_same_message_is_unchanged_not_duplicated(repo):
    """Identity is the message id, so re-reading the same mail links, not duplicates."""
    same = "<fictional-same@pku.edu.cn>"
    first = plan_for(repo, mailbox((1, raw("作业", "作业提交截止2026-12-01。", message_id=same))))
    decisions = default_decisions(first)
    created = apply_email_plan(repo, first, decisions, confirmed=True)
    assert len(created) == 1
    assert created[0].source == EMAIL_SOURCE

    second_plan = plan_for(repo, mailbox((1, raw("作业", "作业提交截止2026-12-01。", message_id=same))))
    assert kinds(second_plan) == {UNCHANGED}
    assert len(repo.list_tasks()) == 1


def test_changed_deadline_offers_an_update_without_preselecting_it(repo):
    same = "<fictional-changed@pku.edu.cn>"
    first = plan_for(repo, mailbox((1, raw("作业", "作业提交截止2026-12-01。", message_id=same))))
    apply_email_plan(repo, first, default_decisions(first), confirmed=True)

    later = plan_for(repo, mailbox((1, raw("作业", "作业提交截止2026-12-05。", message_id=same))))
    assert kinds(later) == {DEADLINE_CHANGED}
    decision = default_decisions(later)[0]
    assert decision.action == "skip"  # never auto-applied
    task = repo.list_tasks()[0]
    assert [row["action"] for row in repo.history(task.id)] == ["create"]


def test_a_different_message_with_the_same_title_is_a_review_item_not_a_merge(repo):
    first = plan_for(repo, mailbox((1, raw("作业", "作业提交截止2026-12-01。"))))
    apply_email_plan(repo, first, default_decisions(first), confirmed=True)

    second = plan_for(repo, mailbox((2, raw("作业", "作业提交截止2026-12-01。"))))
    assert kinds(second) == {POSSIBLE_DUPLICATE}
    # A possible duplicate is offered as its own task, never merged by title.
    assert default_decisions(second)[0].action == "new"


# ------------------------------------------------------------------- gating


def test_an_untrustworthy_report_cannot_produce_a_plan(repo):
    adapter = mailbox((1, raw("通知", "截止2026年12月1日。")))
    report = report_for(adapter)
    from dataclasses import replace

    damaged = replace(report, warnings=("有邮件缺少 Message-ID。",))
    with pytest.raises(EmailNotTrustworthy) as error:
        plan_email_import(damaged, repo)
    assert "数据库未被修改" in str(error.value)
    assert repo.list_tasks() == []


def test_rejections_also_stop_the_plan(repo):
    adapter = mailbox((1, raw("通知", "截止2026年12月1日。")))
    from dataclasses import replace

    damaged = replace(report_for(adapter), rejections=("UID 2: 无法解析。",))
    with pytest.raises(EmailNotTrustworthy):
        plan_email_import(damaged, repo)


def test_apply_refuses_without_confirmation_and_writes_nothing(repo):
    plan = plan_for(repo, mailbox((1, raw("作业", "作业提交截止2026-12-01。"))))
    with pytest.raises(ValueError):
        apply_email_plan(repo, plan, default_decisions(plan))
    assert repo.list_tasks() == []


def test_apply_with_all_skipped_writes_nothing(repo):
    plan = plan_for(repo, mailbox((1, raw("作业", "作业提交截止2026-12-01。"))))
    decisions = tuple(ImportDecision("skip", reason="本轮不导入") for _ in plan.proposals)
    assert apply_email_plan(repo, plan, decisions, confirmed=True) == []
    assert repo.list_tasks() == []


def test_update_requires_a_reason(repo):
    first = plan_for(repo, mailbox((1, raw("作业", "作业提交截止2026-12-01。"))))
    apply_email_plan(repo, first, default_decisions(first), confirmed=True)
    task = repo.list_tasks()[0]

    later = plan_for(repo, mailbox((1, raw("作业", "作业提交截止2026-12-05。"))))
    proposal = later.proposals[0]
    decisions = (ImportDecision("update", target_id=task.id,
                                expected_revision=task.revision, reason="  "),)
    with pytest.raises(ValueError):
        apply_email_plan(repo, later, decisions, confirmed=True)
    assert repo.get_task(task.id).due_at == datetime(2026, 12, 1, 0, 0, tzinfo=BEIJING)


def test_a_failed_sync_never_deletes_existing_tasks(repo):
    """Requirement: 禁止同步失败时删除现有任务."""
    plan = plan_for(repo, mailbox((1, raw("作业", "作业提交截止2026-12-01。"))))
    apply_email_plan(repo, plan, default_decisions(plan), confirmed=True)
    existing = repo.list_tasks()
    assert len(existing) == 1

    # A later pass that reads nothing (fictional empty mailbox) proposes nothing
    # and must not touch what is already stored.
    empty = plan_for(repo, mailbox())
    assert empty.proposals == ()
    assert repo.list_tasks() == existing


def test_import_writes_the_email_source_and_a_hashed_identity(repo):
    plan = plan_for(repo, mailbox((1, raw("奖学金申请", "请于2026年12月10日前提交申请材料。"))))
    created = apply_email_plan(repo, plan, default_decisions(plan), confirmed=True)
    stored = created[0]
    assert stored.source == EMAIL_SOURCE
    assert stored.task_type == "其他"
    assert stored.category == "教务处"
    mappings = repo.source_mappings(EMAIL_SOURCE)
    assert len(mappings) == 1
    key = next(iter(mappings))
    assert len(key) == 64
    assert mappings[key] == stored.id
    # The readable message id is never stored: only its hash-based key is.
    assert "fictional-1" not in key
    assert "pku.edu.cn" not in key


def test_imported_task_type_follows_the_deadline_kind(repo):
    plan = plan_for(repo, mailbox(
        (1, raw("活动报名", "报名截止2026年11月13日15:00。")),
        (2, raw("作业", "作业提交截止2026-12-01。")),
    ))
    created = apply_email_plan(repo, plan, default_decisions(plan), confirmed=True)
    assert {task.task_type for task in created} == {"活动报名", "作业"}


def test_migration_version_is_not_changed_by_import(repo):
    plan = plan_for(repo, mailbox((1, raw("作业", "作业提交截止2026-12-01。"))))
    apply_email_plan(repo, plan, default_decisions(plan), confirmed=True)
    import sqlite3

    with sqlite3.connect(repo.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_describe_mentions_every_group_it_hides_nothing_in(repo):
    plan = plan_for(repo, mailbox(
        (1, raw("综合通知", "报名截止2026年11月13日15:00，讲座将于2026年11月20日举行。")),
        (2, raw("截止通知", "作业提交截止，具体日期待定。")),
        (3, truncated_raw("长通知", "截止2026年12月1日。" + "正文内容" * 6000)),
    ))
    text = plan.describe()
    assert "仅展示活动时间" in text
    assert "待确认时间" in text
    assert "无法解析" in text


def test_analysis_context_is_available_for_each_proposal(repo):
    plan = plan_for(repo, mailbox((1, raw("奖学金申请", "请于2026年12月10日前提交申请材料。"))))
    context = plan.context_for(plan.proposals[0].source_key)
    assert context is not None
    assert context.category in {"活动报名", "学校通知"}
    assert context.reason  # the classification reason travels with the task
    assert context.raw_text
    assert context.record_uid == 1
    assert context.sent_at is not None
