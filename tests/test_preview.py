import json
import socket
from dataclasses import replace
from datetime import datetime

import pytest

from pku_deadline_agent.mock_api import MockAPI
from pku_deadline_agent.notices import filter_notices
from pku_deadline_agent.preview import ReviewSession, prepare_preview
from pku_deadline_agent.time_utils import BEIJING

SAFE = "标题: 虚构作业\n课程: 虚构课程\n截止时间: 下周三 20:00\n类型: 作业"
PUBLISHED = datetime(2026, 10, 11, 9, tzinfo=BEIJING)


def payload(preview):
    return json.loads(json.loads(preview.request_json)["messages"][1]["content"])


def test_allowlist_discards_chat_contacts_links_and_unknown_fields():
    raw = SAFE + "\n联系人: 小甲\n随便聊: 私人谈话不应传输\n网址: https://example.invalid/private\n昵称: 小乙"
    preview = prepare_preview(raw, PUBLISHED)
    assert preview.batch.ignored_lines == 4
    assert all(value not in preview.request_json for value in ("小甲", "私人谈话", "https://", "小乙", "联系人"))
    notice = payload(preview)["notices"][0]
    assert notice == {
        "source_index": 1, "title": "虚构作业", "category": "虚构课程", "task_type": "作业",
        "deadline_text": "下周三 20:00", "published_at": "2026-10-11T09:00:00+08:00", "requires_review": True,
    }


def test_redacted_identifiers_never_reach_mock_request(monkeypatch):
    def forbid_network(*args, **kwargs):
        pytest.fail("Mock API must not establish a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    raw = SAFE.replace("虚构作业", "虚构作业 13800138000 2600012345 11010120000101001X")
    raw += "\n联系人: fictional@example.invalid"
    client = MockAPI()
    review = ReviewSession(client)
    preview = review.prepare(raw, PUBLISHED, "DeepSeek")
    receipt = review.send(raw, PUBLISHED, "DeepSeek", confirmed=True)
    assert receipt.notice_count == 1
    assert client.requests == (preview.request_json,)
    for private in ("13800138000", "2600012345", "11010120000101001X", "fictional@example.invalid"):
        assert private not in client.requests[0]
    assert "[已移除敏感信息]" in client.requests[0]


@pytest.mark.parametrize("secret", [
    "密码: FictionalPass!", "Cookie: sessionid=fake-only", "Token: fake-only",
    "API Key: fake-only", "Authorization: Bearer fake-only", "sk-fictional-only-not-valid-123456",
])
def test_credential_anywhere_blocks_entire_batch_and_mock(secret):
    raw = SAFE + "\n---\n无关原文: " + secret
    review = ReviewSession()
    preview = review.prepare(raw, None, "DeepSeek")
    assert preview.batch.blocked
    assert preview.request_json is None
    with pytest.raises(ValueError):
        review.send(raw, None, "DeepSeek", confirmed=True)
    assert not review.client.requests


def test_cannot_send_without_preview_or_confirmation_or_reuse_approval():
    review = ReviewSession()
    with pytest.raises(ValueError):
        review.send(SAFE, None, "DeepSeek", confirmed=True)
    preview = review.prepare(SAFE, None, "DeepSeek")
    with pytest.raises(ValueError, match="明确确认"):
        review.send(SAFE, None, "DeepSeek", confirmed=False)
    assert not review.client.requests
    review.send(SAFE, None, "DeepSeek", confirmed=True)
    with pytest.raises(ValueError, match="已模拟发送"):
        review.send(SAFE, None, "DeepSeek", confirmed=True)
    assert review.client.requests == (preview.request_json,)


@pytest.mark.parametrize("raw, published, provider", [
    (SAFE + "\n聊天: 新信息", PUBLISHED, "DeepSeek"),
    (SAFE.replace("虚构作业", "新标题"), PUBLISHED, "DeepSeek"),
    (SAFE, None, "DeepSeek"),
    (SAFE, datetime(2026, 10, 12, 9, tzinfo=BEIJING), "DeepSeek"),
    (SAFE, PUBLISHED, "MiMo"),
    (SAFE + "\nCookie: fake-only", PUBLISHED, "DeepSeek"),
])
def test_context_changes_invalidate_even_a_stale_confirmation(raw, published, provider):
    review = ReviewSession()
    review.prepare(SAFE, PUBLISHED, "DeepSeek")
    with pytest.raises(ValueError, match="变化"):
        review.send(raw, published, provider, confirmed=True)
    assert review.preview is None
    assert not review.client.requests


def test_tampered_preview_cannot_bypass_rescan():
    review = ReviewSession()
    preview = review.prepare(SAFE, PUBLISHED, "DeepSeek")
    review.preview = replace(preview, request_json='{"secret": "fake-only"}')
    with pytest.raises(ValueError, match="校验失败"):
        review.send(SAFE, PUBLISHED, "DeepSeek", confirmed=True)
    assert not review.client.requests


def test_batch_supports_individual_publication_and_unknown_relative_dates():
    raw = SAFE + "\n发布时间: 2026-10-10 12:30\n---\n标题: 虚构活动\n截止时间: 明晚\n发布时间: 昨天"
    preview = prepare_preview(raw, PUBLISHED, "MiMo")
    notices = payload(preview)["notices"]
    assert notices[0]["published_at"] == "2026-10-10T12:30:00+08:00"
    assert notices[1]["published_at"] is None
    assert notices[1]["deadline_text"] == "明晚"
    assert any("待确认" in message for message in preview.batch.warnings)
    assert "昨天" not in preview.request_json
    assert json.loads(preview.request_json)["model"] == "mock-mimo"


@pytest.mark.parametrize("raw", [
    "大家好，下周三交作业。私人聊天不应作为兜底上传。",
    "标题: 13800138000", "标题: 虚构练习\n标题: 冲突标题",
    "标题: " + "字" * 201, "标题: x" * 4000,
    "\n---\n".join([SAFE] * 21), "",
], ids=["unstructured", "only-phone", "duplicate", "long-field", "long-batch", "too-many", "empty"])
def test_no_fallback_to_raw_and_invalid_input_has_no_request(raw):
    preview = prepare_preview(raw)
    assert preview.batch.blocked
    assert preview.request_json is None


def test_arbitrary_deadline_prose_and_task_types_are_not_exported():
    preview = prepare_preview("标题: 虚构练习\n类型: 私人信息\n截止时间: 明晚联系私人昵称再交\n我的名字: 小甲")
    notice = payload(preview)["notices"][0]
    assert notice["deadline_text"] is None
    assert notice["task_type"] == "其他"
    assert all(value not in preview.request_json for value in ("私人信息", "私人昵称", "小甲"))


def test_naive_publication_and_unknown_provider_are_rejected():
    with pytest.raises(ValueError, match="时区"):
        filter_notices(SAFE, datetime(2026, 10, 11))
    with pytest.raises(ValueError, match="提供方"):
        prepare_preview(SAFE, provider="unknown")


def test_new_preview_can_be_confirmed_after_changes():
    review = ReviewSession()
    review.prepare(SAFE, PUBLISHED, "DeepSeek")
    changed = SAFE.replace("虚构作业", "更新的虚构作业")
    assert review.observe(changed, PUBLISHED, "MiMo")
    preview = review.prepare(changed, PUBLISHED, "MiMo")
    review.send(changed, PUBLISHED, "MiMo", confirmed=True)
    assert review.client.requests == (preview.request_json,)


@pytest.mark.parametrize("identifier", [
    "13800138000", "+86 138-0013-8000", "138.0013.8000", "138\t0013\t8000",
    "+1 (202) 555-0100", "１３８００１３８０００", "138\u200b00138000",
    "学号: ABC123456", "2600012345", "11010120000101001X", "110101900101001",
    "fictional@example.invalid",
], ids=["phone", "spaced-phone", "dot-phone", "tab-phone", "international", "fullwidth", "invisible", "student-label", "student", "id18", "id15", "email"])
def test_each_identifier_is_removed_from_actual_mock_request(identifier):
    review = ReviewSession()
    raw = f"标题: 虚构练习 {identifier}\n课程: 虚构课程"
    preview = review.prepare(raw, None, "DeepSeek")
    assert not preview.batch.blocked
    assert preview.batch.findings
    review.send(raw, None, "DeepSeek", confirmed=True)
    notice = payload(preview)["notices"][0]
    assert notice["title"] == "虚构练习 [已移除敏感信息]"
    assert review.client.requests == (preview.request_json,)
    assert identifier not in review.client.requests[0]


def test_mock_ignores_real_api_configuration(monkeypatch):
    monkeypatch.setenv("PKU_AI_API_KEY", "fake-only-env-key")
    monkeypatch.setenv("PKU_AI_BASE_URL", "https://example.invalid/never-connect")
    monkeypatch.setenv("PKU_AI_MODEL", "never-call-this")
    review = ReviewSession()
    review.prepare(SAFE, None, "MiMo")
    review.send(SAFE, None, "MiMo", confirmed=True)
    request = review.client.requests[0]
    assert all(value not in request for value in ("fake-only-env-key", "never-connect", "never-call-this"))
