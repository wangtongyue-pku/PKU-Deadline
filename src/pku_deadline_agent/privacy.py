"""Local detection only. Never include matched private values in findings/errors.

Rules are deliberately conservative, not a guarantee of complete detection.
Offsets refer to normalize_text() output, not the user's original input.
"""

import re
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int
    blocks_sending: bool = False


@dataclass(frozen=True)
class PrivacyReport:
    sanitized_text: str = field(repr=False)
    findings: tuple[Finding, ...]

    @property
    def blocked(self) -> bool:
        return any(finding.blocks_sending for finding in self.findings)

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted({finding.kind for finding in self.findings}))


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    # Remove invisible formatting/control characters that can split labels/digits.
    return "".join(
        character for character in text
        if character in "\n\t" or unicodedata.category(character) not in {"Cf", "Cc"}
    )


# Authentication markers block even when they occur on an otherwise ignored line.
# Mentioning these terms can produce false positives; remove the whole credential
# from the local source rather than providing an override button.
_CREDENTIAL_LABEL = re.compile(
    r"(?i)(?<![a-z0-9])(?:password|passwd|pwd|cookie|set[-_ ]?cookie|"
    r"(?:access|refresh|auth|id)[-_ ]?token|token|authorization|bearer|"
    r"api[-_ ]?key|apikey|secret(?:[-_ ]?key)?|client[-_ ]?secret|"
    r"session(?:[-_ ]?id)?|jsessionid|phpsessid|sessdata|csrftoken|"
    r"private[-_ ]?key|jwt|access[-_ ]?key)(?![a-z0-9])"
    r"|密\s*码(?!学)|口令|验证码|令牌|密钥|登录凭据"
)
_SECRET_VALUE = re.compile(
    r"(?i)(?<![a-z0-9])(?:sk-[a-z0-9_-]{8,}|gh[pousr]_[a-z0-9]{12,}|"
    r"AKIA[A-Z0-9]{16}|eyJ[a-z0-9_-]{5,}\.[a-z0-9_-]{5,}\.[a-z0-9_-]{5,})"
    r"(?![a-z0-9])"
)
_EMBEDDED_LOGIN = re.compile(r"(?i)https?://[^/\s:@]+:[^/\s@]+@")
_OPAQUE_VALUE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_+/=-]{24,}(?![A-Za-z0-9_])")
_ID_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){17}[\dXx](?!\d)|(?<!\d)\d{15}(?!\d)")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[ \t().-]*)?1[3-9](?:[ \t().-]*\d){9}(?!\d)")
_INTERNATIONAL_PHONE = re.compile(r"(?<!\w)\+(?:\d[ \t().-]*){8,15}(?!\d)")
_STUDENT_LABEL = re.compile(r"(?i)(?:学\s*号|student[-_ ]?(?:id|number))\s*[:=]?\s*[A-Za-z0-9 -]{6,20}")
_STUDENT_NUMBER = re.compile(r"(?<!\d)\d{10,12}(?!\d)")
_EMAIL = re.compile(r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_URL = re.compile(r"(?i)(?:https?://|www\.)[^\s<>，。；！？\u4e00-\u9fff]+")
_CONTACT_HANDLE = re.compile(
    r"(?i)(?:微信号|微信|QQ号?|(?<![a-z])(?:wx|vx)(?![a-z]))[ \t]*(?:[:=]|是|为)?[ \t]*[a-z0-9_.@+-]{3,40}"
)
_MENTION = re.compile(r"@[^\s@，。；！？:：]{1,40}")
_SURNAMES = "赵钱孙李周吴郑王冯陈蒋沈韩杨朱秦许何吕施张孔曹严华金魏陶姜谢邹喻柏章云苏潘葛范彭鲁韦马方俞任袁柳唐罗薛伍余姚孟顾尹江钟杜程陆林徐黄郭梁宋曾邓萧董肖田刘侯邵白龙石廖贾夏叶阎熊赖龚谭贺段雷万戴崔毛卢"
_NAME = rf"(?:(?:欧阳|司马|上官|诸葛)[\u4e00-\u9fff]{{1,2}}|[{_SURNAMES}][\u4e00-\u9fff]{{1,3}})"
_PERSON_NAME = rf"(?:{_NAME}|[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){{0,2}})"
_LABELED_NAME = re.compile(
    rf"(?:姓名|联系人|负责人|主讲人|报告人|授课教师|助教|我叫|我是)[ \t]*(?:[:=]|是|为)?[ \t]*({_NAME}|[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){{0,2}})(?=[ \t，。；:：]|同学|老师|负责|联系|手机|学号|微信|$)"
)
_NAME_CONTEXT = re.compile(rf"(?:联系(?!方式|电话|信息|人)|找|交给|发给|请教|由)[ \t]*({_PERSON_NAME})(?=同学|老师|助教|负责|统一|报名|提交|收集|收取|确认|咨询|询问|领取|登记|拿|明天|明晚|下周|[ \t，。；:：]|$)")
_NAME_ROLE = re.compile(rf"(?<![下本上])(?!周[一二三四五六日天]同学)({_NAME}|[{_SURNAMES}]|[A-Z][a-z]+)(?:老师|同学|助教|教授|学长|学姐)")
_NAME_ACTION = re.compile(rf"({_NAME})(?=提醒|通知|表示|负责|收集|收取|的作业|说[，,:：])")
_NAME_DATE = re.compile(rf"(?:^|[，。；\n])[ \t]*({_NAME})(?=明天|明晚|今晚|下周|本周|后天)")
_CHAT_NAME = re.compile(rf"(?m)^[ \t]*({_NAME})(?=[ \t]*(?:[:：]|\d{{1,2}}:\d{{2}}))")
_STRUCTURED_LABELS = {"任务", "任务标题", "课程", "活动", "标题", "类型", "任务类型", "截止时间", "截止日期", "发布时间", "发布日期"}


def scan_privacy(text: str) -> PrivacyReport:
    normalized = normalize_text(text)
    findings: list[Finding] = []
    for kind, pattern, blocks in (
        ("认证凭据标记", _CREDENTIAL_LABEL, True),
        ("疑似认证密钥", _SECRET_VALUE, True),
        ("链接内认证凭据", _EMBEDDED_LOGIN, True),
        ("不明长串（需本地核查）", _OPAQUE_VALUE, True),
        ("身份证号", _ID_CARD, False),
        ("手机号", _PHONE, False),
        ("手机号", _INTERNATIONAL_PHONE, False),
        ("学号", _STUDENT_LABEL, False),
        ("疑似学号", _STUDENT_NUMBER, False),
        ("邮箱", _EMAIL, False),
        ("联系方式", _CONTACT_HANDLE, False),
        ("链接", _URL, False),
        ("群昵称或提及", _MENTION, False),
    ):
        findings.extend(Finding(kind, match.start(), match.end(), blocks) for match in pattern.finditer(normalized))
    # Contextual heuristics, not a general Chinese name recognizer. Keep matched
    # values out of findings. Do not mistake a structured key such as 任务 for a name.
    for pattern in (_LABELED_NAME, _NAME_CONTEXT, _NAME_ROLE, _NAME_ACTION, _NAME_DATE, _CHAT_NAME):
        findings.extend(
            Finding("疑似姓名", match.start(1), match.end(1))
            for match in pattern.finditer(normalized)
            if match[1] not in _STRUCTURED_LABELS
        )

    # Merge overlapping matches (e.g. a phone also matching an unlabeled ID).
    intervals: list[list[int]] = []
    for finding in sorted(findings, key=lambda item: (item.start, item.end)):
        if intervals and finding.start <= intervals[-1][1]:
            intervals[-1][1] = max(intervals[-1][1], finding.end)
        else:
            intervals.append([finding.start, finding.end])
    parts: list[str] = []
    previous_end = 0
    for start, end in intervals:
        parts.extend((normalized[previous_end:start], "[已移除敏感信息]"))
        previous_end = end
    parts.append(normalized[previous_end:])
    # A blocked source has no sanitized export: never surface a partly masked secret.
    sanitized = "" if any(item.blocks_sending for item in findings) else "".join(parts)
    return PrivacyReport(sanitized, tuple(findings))
