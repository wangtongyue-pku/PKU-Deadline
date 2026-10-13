"""Stage 7C offline tests: classification, importance, digest, feedback, AI gate.

Everything runs on fictional messages. No network, no credentials, no mailbox —
the AI path is only *prepared* here, never dispatched.
"""

from datetime import date, datetime

import pytest

from pku_deadline_agent.email_intel import (
    AI_SNIPPET_CHARS,
    ClassificationFeedback,
    EmailFeedbackStore,
    analyze_email,
    build_ai_payload,
    build_daily_digest,
    classify_email,
    detect_actions,
)
from pku_deadline_agent.email_models import (
    ADVERTISEMENT,
    COMPETITION,
    COURSE_ADMIN,
    EMAIL_CATEGORIES,
    EVENT_REGISTRATION,
    EVENT_TIME,
    GENERAL_NEWS,
    HOMEWORK_DEADLINE,
    IMPORTANCE_HIGH,
    IMPORTANCE_LOW,
    IMPORTANCE_MEDIUM,
    LECTURE,
    MATERIAL_SUBMISSION_DEADLINE,
    NEEDS_REVIEW,
    REGISTRATION_DEADLINE,
    RESEARCH_OPPORTUNITY,
    SCHOOL_NOTICE,
    TASK_DEADLINE,
    UNCLEAR_TIME,
)
from pku_deadline_agent.email_parse import parse_raw_email
from pku_deadline_agent.time_utils import BEIJING
from tests.test_stage7_email import make_email

# A fixed "now" so importance windows and digest days are deterministic.
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=BEIJING)


def record_for(subject="虚构通知", body="这是一封虚构通知。",
               sender="教务处 <jwc@pku.edu.cn>", uid=1, date=None):
    raw = make_email(subject=subject, body=body, sender=sender, date=date)
    record, _ = parse_raw_email(raw, uid=uid, folder="INBOX", uidvalidity=1)
    return record


def analyze(subject="虚构通知", body="这是一封虚构通知。",
            sender="教务处 <jwc@pku.edu.cn>", uid=1, date=None):
    # The message date is pinned to NOW's day so year defaults and importance
    # windows are deterministic; a caller can override it explicitly.
    return analyze_email(
        record_for(subject, body, sender, uid, date or NOW),
        now=NOW,
    )


# --------------------------------------------------------------- classification


def test_every_category_is_reachable_and_the_taxonomy_is_closed():
    """Requirement 1: nine categories, each one produced by an explicit rule."""
    assert EMAIL_CATEGORIES == (
        COURSE_ADMIN, SCHOOL_NOTICE, LECTURE, RESEARCH_OPPORTUNITY, COMPETITION,
        EVENT_REGISTRATION, GENERAL_NEWS, ADVERTISEMENT, NEEDS_REVIEW,
    )
    cases = [
        # body is given wherever the default body's wording would decide the
        # category on its own (it contains 通知).
        ("关于期末考试安排的通知", "jwc@pku.edu.cn", COURSE_ADMIN, None),
        ("关于放假安排的通知", "dean@pku.edu.cn", SCHOOL_NOTICE, None),
        ("学术讲座：虚构主题", "t@pku.edu.cn", LECTURE, None),
        ("课题组科研助理招募", "prof@pku.edu.cn", RESEARCH_OPPORTUNITY, None),
        ("ACM 程序设计竞赛报名", "club@pku.edu.cn", COMPETITION, None),
        ("校园探索活动报名", "club@pku.edu.cn", EVENT_REGISTRATION, None),
        ("校园生活小贴士", "life@pku.edu.cn", GENERAL_NEWS, "图书馆开放时间调整。"),
        ("限时优惠", "shop@example.com", ADVERTISEMENT, None),
        ("周末去哪玩", "friend@qq.com", NEEDS_REVIEW, None),
    ]
    produced = set()
    for subject, sender, expected, body in cases:
        record = record_for(subject=subject, sender=sender, body=body or "这是一封虚构通知。")
        category, reason = classify_email(record)
        assert category == expected, (subject, category, expected)
        assert reason  # every verdict carries its reason
        produced.add(category)
    assert produced == set(EMAIL_CATEGORIES)


def test_off_campus_mail_is_never_silently_absorbed_into_a_campus_category():
    """An unknown outside sender must land in 待确认邮件, not 普通资讯."""
    category, _ = classify_email(record_for(subject="周末去哪玩", sender="friend@qq.com"))
    assert category == NEEDS_REVIEW


def test_advertisement_needs_both_the_wording_and_a_non_campus_sender():
    """A campus mail that merely says 广告 is not demoted to 广告."""
    campus, _ = classify_email(record_for(subject="讲座广告：虚构主题", sender="t@pku.edu.cn"))
    assert campus == LECTURE
    outside, _ = classify_email(record_for(subject="讲座广告", sender="shop@example.com"))
    assert outside == ADVERTISEMENT


# ------------------------------------------------------------------ importance


def test_importance_is_explainable_and_high_inside_the_window():
    analysis = analyze(subject="作业", body="作业提交截止2026年11月3日。")
    assert analysis.deadlines
    assert analysis.importance == IMPORTANCE_HIGH
    assert any("3 天窗口" in reason for reason in analysis.importance_reasons)


def test_importance_medium_for_a_later_deadline():
    analysis = analyze(subject="活动", body="活动时间2026年12月20日举行。")
    assert analysis.importance == IMPORTANCE_MEDIUM
    assert any("12-20" in reason for reason in analysis.importance_reasons)


def test_importance_low_for_a_plain_message_and_still_reasoned():
    analysis = analyze(subject="校园生活小贴士", body="图书馆开放时间调整。")
    assert analysis.importance == IMPORTANCE_LOW
    assert analysis.importance_reasons  # 「低」 is an explanation, not silence


def test_importance_of_an_advertisement_is_low_even_with_a_date():
    analysis = analyze(subject="限时优惠", sender="shop@example.com",
                       body="活动时间2026年12月20日举行，点击退订。")
    assert analysis.category == ADVERTISEMENT
    assert analysis.importance == IMPORTANCE_LOW


def test_unclear_time_is_medium_and_actionable():
    analysis = analyze(subject="通知", body="请于近期完成，具体时间另行通知。")
    assert any(d.kind == UNCLEAR_TIME for d in analysis.deadlines) or analysis.importance in (
        IMPORTANCE_LOW, IMPORTANCE_MEDIUM,
    )
    assert analysis.importance in (IMPORTANCE_LOW, IMPORTANCE_MEDIUM)


def test_detect_actions_reports_its_evidence():
    analysis = analyze(subject="通知", body="请务必于2026年11月2日前提交回执。")
    action_needed, reasons = detect_actions(analysis)
    assert action_needed
    assert reasons
    quiet = analyze(subject="校园生活小贴士", body="图书馆开放时间调整。")
    quiet_needed, _ = detect_actions(quiet)
    assert not quiet_needed


def test_importance_ranking_helper_is_monotonic():
    """IMPORTANCE_LEVELS is weakest→strongest, so max() raises importance.

    The order is not cosmetic: assess_importance uses max() over this tuple, and
    listing 高 first silently downgraded a 3-day deadline to 中.
    """
    from pku_deadline_agent.email_models import IMPORTANCE_LEVELS
    assert IMPORTANCE_LEVELS == (IMPORTANCE_LOW, IMPORTANCE_MEDIUM, IMPORTANCE_HIGH)
    assert IMPORTANCE_LEVELS.index(IMPORTANCE_LOW) < IMPORTANCE_LEVELS.index(IMPORTANCE_MEDIUM)
    assert IMPORTANCE_LEVELS.index(IMPORTANCE_MEDIUM) < IMPORTANCE_LEVELS.index(IMPORTANCE_HIGH)


# --------------------------------------------------------------------- digest


def test_digest_keeps_every_message_somewhere():
    """Requirement 5: low importance is grouped later, never dropped."""
    analyses = [
        analyze(subject="作业", body="作业提交截止2026年11月2日。", uid=1),
        analyze(subject="学术讲座：虚构主题", sender="t@pku.edu.cn",
                body="讲座将于2026年12月1日举行。", uid=2),
        analyze(subject="校园生活小贴士", body="图书馆开放时间调整。", uid=3),
    ]
    digest = build_daily_digest(analyses, day=date(2026, 11, 1), now=NOW)
    assert digest.total == 3
    listed = (
        {entry.stable_key for entry in digest.priority_reads}
        | {entry.stable_key for entry in digest.events}
        | {entry.stable_key for entry in digest.rest}
    )
    assert listed == {analysis.record.stable_key for analysis in analyses}


def test_digest_counts_today_older_and_undated_separately():
    today = analyze(uid=1, date=datetime(2026, 11, 1, 8, 0, tzinfo=BEIJING))
    older = analyze(uid=2, date=datetime(2026, 10, 20, 8, 0, tzinfo=BEIJING))
    digest = build_daily_digest([today, older], day=date(2026, 11, 1), now=NOW)
    assert digest.received_today == 1
    assert digest.older_than_today == 1
    assert digest.undated == 0
    assert "今日收到 1 封" in digest.summary_line()


def test_digest_separates_deadlines_from_events_and_marks_unconfirmed():
    analyses = [
        analyze(subject="活动", uid=1, body="报名截止2026年11月10日，讲座将于2026年11月20日举行。"),
    ]
    digest = build_daily_digest(analyses, day=date(2026, 11, 1), now=NOW)
    kinds = {item.kind for item in digest.deadlines}
    assert REGISTRATION_DEADLINE in kinds
    assert EVENT_TIME in kinds
    assert not any(item.needs_confirmation for item in digest.deadlines
                   if item.kind != UNCLEAR_TIME)


def test_truncated_body_is_flagged_as_incomplete_not_as_absence_of_deadlines():
    raw = make_email(subject="长通知", body="内容" * 20000)
    record, warnings = parse_raw_email(raw, uid=1, folder="INBOX", uidvalidity=1)
    analysis = analyze_email(record, now=NOW)
    assert analysis.record.body_truncated or any("截断" in w for w in warnings)
    digest = build_daily_digest([analysis], day=date(2026, 11, 1), now=NOW)
    if analysis.record.body_truncated:
        assert analysis.incomplete
        assert any(entry.stable_key == record.stable_key for entry in digest.incomplete)
        assert "未提取到的 Deadline" in digest.incomplete[0].note


def test_digest_entries_always_carry_a_reason():
    analyses = [analyze(subject="学术讲座：虚构主题", sender="t@pku.edu.cn", uid=1)]
    digest = build_daily_digest(analyses, day=date(2026, 11, 1), now=NOW)
    for group in (digest.priority_reads, digest.events, digest.rest, digest.incomplete):
        for entry in group:
            assert entry.reasons, entry.subject


# ------------------------------------------------------------------- feedback


def test_feedback_store_records_a_correction_and_rejects_a_no_op():
    store = EmailFeedbackStore()
    feedback = ClassificationFeedback(
        stable_key="msgid:<fictional-1@pku.edu.cn>",
        subject="虚构通知",
        predicted_category=NEEDS_REVIEW,
        corrected_category=LECTURE,
        note="其实是讲座通知",
    )
    store.record(feedback)
    assert len(store) == 1
    assert store.all()[0].corrected_category == LECTURE
    with pytest.raises(ValueError):
        store.record(ClassificationFeedback(
            stable_key="msgid:<fictional-2@pku.edu.cn>",
            subject="虚构通知",
            predicted_category=LECTURE,
            corrected_category=LECTURE,
            note="没有改",
        ))
    assert len(store) == 1


# ------------------------------------------------------------------- AI gate


def test_ai_payload_refuses_to_build_without_per_batch_confirmation():
    analysis = analyze()
    with pytest.raises(PermissionError):
        build_ai_payload([analysis], confirmed=False)
    with pytest.raises(PermissionError):
        build_ai_payload([analysis])


def test_ai_payload_excludes_the_body_and_keeps_only_the_minimum():
    body = "课程作业提交截止2026年11月20日23:59，请勿外传本段正文内容。"
    analysis = analyze(subject="作业通知", body=body)
    payload = build_ai_payload([analysis], confirmed=True)
    assert payload.confirmed
    item = payload.items[0]
    assert item.subject == "作业通知"
    assert item.sender_domain == "pku.edu.cn"
    assert item.category == COURSE_ADMIN
    assert item.deadlines
    assert len(item.snippet) <= AI_SNIPPET_CHARS
    # Only the cued time clauses travel; a paragraph with no cue does not.
    assert "2026年11月20日" in item.snippet
    assert item.privacy_kinds == ()


def test_ai_payload_has_no_channel_for_body_text():
    """Structural isolation: a message with no extracted time contributes no text."""
    subject = "关于期末安排的通知"
    body = "本段正文仅供本地阅读，不应上传。"
    analysis = analyze(subject=subject, body=body)
    item = build_ai_payload([analysis], confirmed=True).items[0]
    assert item.deadlines == ()
    assert body not in item.snippet
    assert not hasattr(item, "body_text")


def test_ai_snippet_is_the_cued_clause_only():
    """The subject is always included; body prose joins only via a cued date."""
    analysis = analyze(subject="作业通知", body="课程作业提交截止2026年11月20日23:59。")
    snippet = build_ai_payload([analysis], confirmed=True).items[0].snippet
    assert "作业通知" in snippet
    assert "2026年11月20日" in snippet


def test_ai_payload_drops_any_message_whose_scanned_text_is_blocked():
    """A credential-like string blocks that message from the payload entirely."""
    analysis = analyze(subject="通知", body="API Key: sk-fictionalaabbccddeeff0011 截止2026年11月20日。")
    payload = build_ai_payload([analysis], confirmed=True)
    assert payload.items == ()
    assert "sk-fictionalaabbccddeeff0011" not in repr(payload)


# ------------------------------------------------- time semantics (7C fixes)


def test_publication_date_is_not_promoted_to_a_deadline():
    """「发布于」 states a fact about the mail, not a date to act on."""
    analysis = analyze(subject="回执", body="本通知发布于2026年11月1日。")
    assert analysis.deadlines == ()


def test_a_cue_from_another_sentence_is_not_borrowed():
    analysis = analyze(subject="通知", body="本通知发布于2026年11月1日。报名截止11月30日。")
    kinds = {(d.kind, d.due_at.date()) for d in analysis.deadlines}
    assert kinds == {(REGISTRATION_DEADLINE, date(2026, 11, 30))}


def test_registration_cue_is_still_borrowed_from_the_adjacent_clause():
    """The documented behaviour: one registration deadline and one event time."""
    analysis = analyze(subject="活动", body="报名截止2026年11月13日15:00，讲座将于2026年11月20日举行。")
    found = {(d.kind, d.due_at) for d in analysis.deadlines}
    assert found == {
        (REGISTRATION_DEADLINE, datetime(2026, 11, 13, 15, 0, tzinfo=BEIJING)),
        (EVENT_TIME, datetime(2026, 11, 20, 0, 0, tzinfo=BEIJING)),
    }


def test_one_date_is_reported_once_not_once_per_pattern():
    """「2026年11月1日」 also matches the bare 月日 pattern; it is one fact."""
    analysis = analyze(subject="活动", body="报名截止2026年11月13日。")
    raw_texts = [d.raw_text for d in analysis.deadlines]
    assert raw_texts == ["2026年11月13日"]


def test_material_deadline_is_not_swallowed_by_the_registration_rule():
    analysis = analyze(subject="奖学金申请", body="请于2026年12月10日前提交申请材料。")
    assert any(d.kind == MATERIAL_SUBMISSION_DEADLINE for d in analysis.deadlines)
    assert not any(d.kind == REGISTRATION_DEADLINE for d in analysis.deadlines)


def test_homework_deadline_is_named_explicitly():
    analysis = analyze(subject="作业", body="作业提交截止2026-12-01。")
    assert any(d.kind == HOMEWORK_DEADLINE for d in analysis.deadlines)


def test_course_selection_start_is_not_reported_as_an_event():
    """「选课」 is administrative; without a settlement the value stays 待确认."""
    analysis = analyze(subject="选课", body="2025级选课于2026年11月20日开始。")
    kinds = {d.kind for d in analysis.deadlines}
    assert EVENT_TIME not in kinds
    assert kinds <= {UNCLEAR_TIME, TASK_DEADLINE}


def test_a_plain_mentioned_date_stays_unpromoted():
    analysis = analyze(subject="校园生活小贴士", body="图书馆开放时间调整自2026年11月1日起施行。")
    assert analysis.deadlines == ()


def test_unresolvable_kind_is_marked_for_confirmation(): 
    """A bare deadline cue with no settlement stays 待确认 instead of guessed."""
    analysis = analyze(subject="通知", body="相关工作请于2026年11月20日前完成，细则另行通知。")
    deadline = analysis.deadlines[0]
    assert deadline.kind in {TASK_DEADLINE, UNCLEAR_TIME}
    if deadline.kind == UNCLEAR_TIME:
        assert deadline.needs_confirmation
