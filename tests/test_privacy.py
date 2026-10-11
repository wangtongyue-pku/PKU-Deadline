import pytest

from pku_deadline_agent.privacy import scan_privacy


@pytest.mark.parametrize("private_value, kind", [
    ("13800138000", "手机号"),
    ("+86 138-0013-8000", "手机号"),
    ("138.0013.8000", "手机号"),
    ("138\t0013\t8000", "手机号"),
    ("+1 202 555 0100", "手机号"),
    ("１３８００１３８０００", "手机号"),
    ("138\u200b00138000", "手机号"),
    ("2600012345", "疑似学号"),
    ("学号: ABC123456", "学号"),
    ("11010120000101001X", "身份证号"),
    ("110101900101001", "身份证号"),
    ("fictional@example.invalid", "邮箱"),
])
def test_removes_personal_identifiers(private_value, kind):
    report = scan_privacy(f"标题: 虚构练习 {private_value}")
    assert not report.blocked
    assert kind in report.kinds
    assert "[已移除敏感信息]" in report.sanitized_text
    assert private_value not in report.sanitized_text
    assert private_value not in repr(report)


@pytest.mark.parametrize("credential", [
    "密码: FictionalPass!", "密 码=FictionalPass!", "口令: fake-only",
    '"password": "fake-only"', "PWD=fake-only", "Cookie: sessionid=fake-only",
    "Set-Cookie: sid=fake-only", "Token: fake-only", "access_token=fake-only",
    "refresh-token=fake-only", "Authorization: Bearer fake-only", "API Key: fake-only",
    "api_key=fake-only", "apikey=fake-only", "client_secret=fake-only", "密钥: fake-only",
    "session_id=fake-only", "JWT: fake-only", "sk-fictional-only-not-valid-123456",
    "JSESSIONID=fake-only", "PHPSESSID=fake-only", "csrftoken=fake-only",
    "private_key: fake-only", "https://fictional:fake-only@example.invalid/",
    "eyJmaWtlMTIz.eyJmYWtlNDU2.fakeSignature789",
    "ＡＰＩ＿ＫＥＹ：fake-only", "To\u200bken: fake-only",
    "abcdefghijklmnopqrstuvwxyz1234567890",
])
def test_credentials_block_and_do_not_return_partial_secret(credential):
    report = scan_privacy(f"标题: 虚构练习\n无关聊天: {credential}")
    assert report.blocked
    assert report.sanitized_text == ""
    assert "fake-only" not in repr(report)
    assert credential not in repr(report)


def test_ordinary_course_and_dates_are_preserved():
    source = "标题: 密码学练习\n课程: 虚构课程\n截止时间: 2026-10-22 20:00"
    report = scan_privacy(source)
    assert not report.findings
    assert report.sanitized_text == source
