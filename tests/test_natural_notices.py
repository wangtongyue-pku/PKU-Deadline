import json
import socket
from datetime import datetime

import pytest

from pku_deadline_agent.preview import ReviewSession, prepare_preview
from pku_deadline_agent.privacy import scan_privacy
from pku_deadline_agent.time_utils import BEIJING

NOTICE = (
    "张小明 09:00\n大家好，《计算概论》第三次作业原定本周五23:59截止，"
    "现延期至下周三晚20:00，请在教学平台提交。\n"
    "联系人：李小明，微信号：fictional_wx，电话：13800138000，学号：2600012345。\n"
    "今晚谁去喝奶茶？"
)
PUBLISHED = datetime(2026, 10, 11, 9, tzinfo=BEIJING)


def content(preview):
    return json.loads(json.loads(preview.request_json)["messages"][1]["content"])


def test_ordinary_wechat_notice_keeps_task_course_dates_and_extension_without_labels(monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("Natural mode must use only the local mock")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    review = ReviewSession()
    preview = review.prepare(NOTICE, PUBLISHED, "DeepSeek", mode="natural")
    assert not preview.batch.blocked
    notice = content(preview)["notices"][0]
    assert set(notice) == {"source_index", "text", "published_at", "requires_review"}
    for necessary in ("《计算概论》", "第三次作业", "原定本周五23:59", "延期至下周三晚20:00", "教学平台提交"):
        assert necessary in notice["text"]
    for private in ("张小明", "李小明", "fictional_wx", "13800138000", "2600012345", "喝奶茶", "大家好"):
        assert private not in preview.request_json
    assert notice["published_at"] == "2026-10-11T09:00:00+08:00"
    assert preview.batch.ignored_lines > 0
    review.send(NOTICE, PUBLISHED, "DeepSeek", mode="natural", confirmed=True)
    assert review.client.requests == (preview.request_json,)


@pytest.mark.parametrize("raw, private", [
    ("张小明老师提醒大家明晚交作业。", "张小明"),
    ("请联系李小明同学报名，明晚截止。", "李小明"),
    ("陈小海负责收集作业，周五截止。", "陈小海"),
    ("负责人：王小明负责收集实验报告，周五截止。", "王小明"),
    ("张小明明晚负责收集作业。", "张小明"),
    ("联系人：Alice Example，明晚交实验报告。", "Alice Example"),
    ("请联系李小明报名，明晚截止。", "李小明"),
    ("请联系Alice Example报名，明晚截止。", "Alice Example"),
    ("欧阳小明老师提醒大家明晚交作业。", "欧阳小明"),
    ("@fictional_classmate 请在明晚提交作业。", "fictional_classmate"),
    ("请在明晚提交作业，微信号fictional_wx。", "fictional_wx"),
    ("请在明晚提交作业，QQ号:12345678。", "12345678"),
    ("作业明晚交，邮箱:fictional@example.invalid。", "fictional@example.invalid"),
    ("作业明晚交，电话:+86 138-0013-8000。", "138-0013-8000"),
    ("学号2600012345的同学请明晚交作业。", "2600012345"),
    ("明晚提交作业，身份证号11010120000101001X。", "11010120000101001X"),
], ids=["teacher", "contact", "responsible", "labeled", "name-before-date", "english", "contact-without-role", "english-contact", "compound-surname", "mention", "wechat", "qq", "email", "phone", "student", "identity"])
def test_contextual_names_and_contacts_do_not_reach_actual_request(raw, private):
    review = ReviewSession()
    preview = review.prepare(raw, PUBLISHED, "MiMo", mode="natural")
    assert not preview.batch.blocked
    review.send(raw, PUBLISHED, "MiMo", mode="natural", confirmed=True)
    assert private not in review.client.requests[0]
    assert any(term in content(preview)["notices"][0]["text"] for term in ("作业", "报名", "报告"))


@pytest.mark.parametrize("expression", ["明晚", "下周三", "下下周五", "另行通知", "尽快"])
def test_ambiguous_dates_are_preserved_without_inventing_a_deadline(expression):
    preview = prepare_preview(f"请{expression}提交虚构课程作业。", mode="natural")
    notice = content(preview)["notices"][0]
    assert expression in notice["text"]
    assert notice["published_at"] is None
    assert notice["requires_review"]
    assert "due_at" not in notice
    assert "title" not in notice


@pytest.mark.parametrize("secret", [
    "密码: fake-only", "Cookie: sid=fake-only", "Token: fake-only", "API Key: fake-only",
    "Authorization: Bearer fake-only", "JSESSIONID=fake-only", "sk-fictional-only-not-valid-123456",
], ids=["password", "cookie", "token", "key", "authorization", "session", "unlabeled-key"])
def test_credentials_in_task_or_ignored_chat_still_block_whole_batch(secret):
    raw = f"明晚交作业。\n---\n无关聊天：{secret}"
    review = ReviewSession()
    preview = review.prepare(raw, PUBLISHED, "DeepSeek", mode="natural")
    assert preview.batch.blocked
    assert preview.request_json is None
    assert not preview.batch.notices
    with pytest.raises(ValueError):
        review.send(raw, PUBLISHED, "DeepSeek", mode="natural", confirmed=True)
    assert not review.client.requests
    assert "fake-only" not in repr(preview)


@pytest.mark.parametrize("raw", [
    "请私聊我报名，明晚截止。",
    "作业明晚联系我的个人主页中的联系方式后提交。",
    "我的住址是虚构路一号明晚交作业。",
    "王小明生病住院所以作业延期到下周三。",
    "请学号二六零零零一二三四五的同学明晚交作业。",
], ids=["private-chat", "unresolved-contact", "address", "health-with-task", "obfuscated-id"])
def test_unresolved_privacy_cannot_be_approved_away_and_requires_editing(raw):
    review = ReviewSession()
    preview = review.prepare(raw, PUBLISHED, "DeepSeek", mode="natural")
    assert preview.batch.blocked
    assert preview.batch.notices  # Local-only draft helps the user edit, never exported.
    assert preview.request_json is None
    assert any("修改原文" in error for error in preview.batch.errors)
    with pytest.raises(ValueError):
        review.send(raw, PUBLISHED, "DeepSeek", mode="natural", confirmed=True)
    assert not review.client.requests
    corrected = "作业延期到下周三，请在教学平台提交。"
    fixed = review.prepare(corrected, PUBLISHED, "DeepSeek", mode="natural")
    review.send(corrected, PUBLISHED, "DeepSeek", mode="natural", confirmed=True)
    assert review.client.requests == (fixed.request_json,)


def test_low_risk_unclassified_language_is_not_rejected_for_missing_fields():
    preview = prepare_preview("麻烦大家把东西准备妥当。", mode="natural")
    assert not preview.batch.blocked
    assert "准备妥当" in content(preview)["notices"][0]["text"]
    assert preview.batch.warnings


def test_chatter_mixed_with_dates_is_removed_without_losing_the_task():
    preview = prepare_preview("昨晚室友吵架，明晚去喝奶茶？\n《高等数学》习题明晚交。", mode="natural")
    text = content(preview)["notices"][0]["text"]
    assert "《高等数学》习题明晚交" in text
    assert all(value not in text for value in ("室友", "吵架", "奶茶"))


def test_task_deadline_line_is_not_mistaken_for_chat_header():
    preview = prepare_preview("作业截止时间 20:00\n请交第三章习题。", mode="natural")
    assert "20:00" in content(preview)["notices"][0]["text"]


def test_chat_header_is_removed_and_not_used_as_publication():
    preview = prepare_preview("虚构群昵称 2026-10-11 09:00\n明晚交作业。", mode="natural")
    notice = content(preview)["notices"][0]
    assert "虚构群昵称" not in notice["text"]
    assert notice["published_at"] is None
    assert any("待确认" in warning for warning in preview.batch.warnings)


def test_batch_publication_overrides_shared_time_and_keeps_unknown_dates_unknown():
    raw = "明晚交作业。\n发布时间: 2026-10-10 10:00\n---\n活动延期至下周三。\n发布时间: 昨天"
    preview = prepare_preview(raw, PUBLISHED, mode="natural")
    notices = content(preview)["notices"]
    assert notices[0]["published_at"] == "2026-10-10T10:00:00+08:00"
    assert notices[1]["published_at"] is None
    assert "昨天" not in preview.request_json


def test_mode_change_revokes_stale_confirmation():
    raw = "标题: 虚构练习\n截止时间: 明晚"
    review = ReviewSession()
    review.prepare(raw, PUBLISHED, "DeepSeek", mode="structured")
    with pytest.raises(ValueError, match="变化"):
        review.send(raw, PUBLISHED, "DeepSeek", mode="natural", confirmed=True)
    assert review.preview is None
    assert not review.client.requests


def test_natural_preview_still_requires_confirmation_and_single_use():
    raw = "明晚交虚构课程作业。"
    review = ReviewSession()
    preview = review.prepare(raw, None, "DeepSeek", mode="natural")
    with pytest.raises(ValueError, match="确认"):
        review.send(raw, None, "DeepSeek", mode="natural", confirmed=False)
    review.send(raw, None, "DeepSeek", mode="natural", confirmed=True)
    with pytest.raises(ValueError, match="已模拟发送"):
        review.send(raw, None, "DeepSeek", mode="natural", confirmed=True)
    assert review.client.requests == (preview.request_json,)


def test_chatter_only_is_not_uploaded_as_raw_fallback():
    raw = "今天谁去喝奶茶？\n姓名:张小明\n微信号:fictional_wx"
    preview = prepare_preview(raw, mode="natural")
    assert not preview.batch.notices
    assert preview.request_json is None
    assert any("没有需要发送" in error for error in preview.batch.errors)


def test_link_redaction_does_not_eat_adjacent_task_verb():
    preview = prepare_preview("请于明晚前在https://example.invalid/apply报名。", mode="natural")
    text = content(preview)["notices"][0]["text"]
    assert "报名" in text
    assert "https://" not in text


def test_weekday_salutation_is_not_a_person_name():
    raw = "下周三同学们提交作业。"
    preview = prepare_preview(raw, mode="natural")
    assert "下周三" in content(preview)["notices"][0]["text"]
    assert not scan_privacy("任务: 完成虚构练习").findings


def test_invalid_mode_is_rejected_without_a_request():
    with pytest.raises(ValueError, match="输入模式"):
        prepare_preview("明晚交作业", mode="unknown")


def test_unknown_sender_nickname_is_removed_without_losing_the_message():
    preview = prepare_preview("小红：明晚交作业。", mode="natural")
    assert "小红" not in preview.request_json
    assert "明晚交作业" in content(preview)["notices"][0]["text"]


def test_removable_contact_clause_does_not_unnecessarily_block_task():
    preview = prepare_preview("作业明晚交，联系方式见我的个人主页。", mode="natural")
    assert not preview.batch.blocked
    assert "个人主页" not in preview.request_json
    assert "作业明晚交" in content(preview)["notices"][0]["text"]


@pytest.mark.parametrize("raw", [
    "奶茶制作活动本周五开始报名，明晚截止。",
    "宿舍文化节活动报名延期至下周三。",
    "请于明晚提交家庭作业。",
    "活动负责人将在明晚统一发布报名通知。",
], ids=["public-food-event", "campus-event", "homework", "public-role"])
def test_public_task_topics_are_not_mistaken_for_private_chat(raw):
    preview = prepare_preview(raw, mode="natural")
    assert not preview.batch.blocked
    assert content(preview)["notices"][0]["text"]


def test_personal_chat_entangled_with_task_requires_edit():
    preview = prepare_preview("我昨晚打游戏耽误了作业明晚补交。", mode="natural")
    assert preview.batch.blocked
    assert preview.request_json is None


def test_contact_web_link_is_removed_without_consuming_task_text():
    preview = prepare_preview("请在明晚前通过www.example.invalid/apply报名。", mode="natural")
    assert "www.example.invalid" not in preview.request_json
    assert "报名" in content(preview)["notices"][0]["text"]


def test_contact_risk_label_is_not_misread_as_a_person_name():
    report = scan_privacy("作业明晚联系我的个人主页中的联系方式后提交。")
    assert "联系方式" in report.sanitized_text
    preview = prepare_preview(report.sanitized_text, mode="natural")
    assert preview.batch.blocked
    assert preview.request_json is None


def test_uncertain_local_draft_is_not_exposed_in_diagnostic_repr():
    preview = prepare_preview("我的住址是虚构路一号明晚交作业。", mode="natural")
    assert preview.batch.blocked
    assert "虚构路一号" in preview.batch.notices[0].sanitized_text
    assert "虚构路一号" not in repr(preview)
