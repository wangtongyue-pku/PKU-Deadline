"""Stage 7D offline demo mailbox: fictional mail for trying the page locally.

Kept out of the page module on purpose. The page needs *some* mailbox factory,
tests need to replace it, and a factory defined inside the module under test is
easy to patch into a loop. This mirrors ``mock_sources.py`` for stage 5: offline
data that exists so the whole flow can be reviewed without an account.

Every message here is invented (fictional courses, fictional organisers, the
``demo.invalid`` domain). No real address, identifier or message is involved, and
nothing in this module opens a connection.

Dates are **relative to the moment the demo is opened**, not written as fixed
calendar dates: the mailbox window is "the last 7 days", so a hard-coded date
would silently fall out of range as time passes and the demo would look empty.
The offsets below are chosen so every demo message stays inside that window and
the deadline spread still exercises all three importance levels.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from pku_deadline_agent.email_adapter import DEFAULT_FOLDER, MockEmailAdapter
from pku_deadline_agent.time_utils import beijing_now

# (sent days ago, subject, sender, body template, deadline days from now)
# `None` for the deadline means the mail mentions a time without giving a usable
# date — the 待确认 case — and no `{deadline}` placeholder is used.
_MESSAGES = (
    (4, "虚构课程作业通知", "教务处 <jwc@pku.edu.cn>",
     "《虚构课程》第三次作业提交截止{deadline}，请按时提交。", 2),
    (3, "虚构学术讲座报名", "讲座组委会 <lecture@pku.edu.cn>",
     "报名截止{deadline}，讲座将于{deadline}后在虚构楼举行。", 5),
    (2, "虚构奖学金材料提交", "学生工作部 <xgb@pku.edu.cn>",
     "请于{deadline}前提交申请材料，材料不齐不予受理。", 12),
    (5, "虚构竞赛报名通知", "竞赛组委会 <contest@pku.edu.cn>",
     "ACM 虚构竞赛报名截止{deadline}，欢迎组队参加。", 9),
    (1, "虚构科研助理招募", "虚构课题组 <lab@pku.edu.cn>",
     "课题组招募研究助理，有意者请于{deadline}前联系。", 6),
    (0, "虚构活动时间待定", "学生会 <union@pku.edu.cn>",
     "虚构校园活动具体时间另行通知，报名从速。", None),
    (6, "虚构校园生活小贴士", "后勤 <logistics@pku.edu.cn>",
     "虚构食堂本周开放时间调整，请留意。", None),
)

DEMO_SUBJECTS = tuple(item[1] for item in _MESSAGES)


@dataclass(frozen=True)
class DemoDeadline:
    """A demo deadline as a promise the tests can check without hard-coding dates."""

    subject: str
    kind_marker: str
    days_from_now: int


def demo_now():
    """The reference "now" for one demo mailbox, in Beijing time."""
    return beijing_now().replace(second=0, microsecond=0)


def _format(moment) -> str:
    """A natural Chinese date phrase; includes a time only when it is not midnight."""
    if moment.hour or moment.minute:
        return f"{moment.year}年{moment.month}月{moment.day}日{moment.hour}:{moment.minute:02d}"
    return f"{moment.year}年{moment.month}月{moment.day}日"


def _raw_message(subject: str, sender: str, body: str, sent_at) -> bytes:
    from email.message import EmailMessage
    from email.utils import format_datetime

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "student@stu.pku.edu.cn"
    message["Message-ID"] = f"<demo-{abs(hash(subject)) % 10**8}@demo.invalid>"
    message["Date"] = format_datetime(sent_at)
    message.set_content(body, subtype="plain", charset="utf-8")
    return message.as_bytes()


def demo_mailbox() -> MockEmailAdapter:
    """A fresh offline INBOX holding the fictional messages above."""
    now = demo_now()
    mailbox = {}
    for index, (sent_days_ago, subject, sender, body, deadline_days) in enumerate(_MESSAGES, start=1):
        if deadline_days is None:
            text = body
        else:
            deadline = now + timedelta(days=deadline_days)
            # A mail that states a time is the one place a clock time helps; the
            # rest stay at midnight so the parser does not invent one.
            deadline = deadline.replace(hour=15 if deadline_days % 2 else 0, minute=0)
            text = body.format(deadline=_format(deadline))
        sent_at = now - timedelta(days=sent_days_ago)
        mailbox[index] = _raw_message(subject, sender, text, sent_at)
    return MockEmailAdapter({DEFAULT_FOLDER: mailbox})


def demo_deadline_days(subject: str) -> int | None:
    """Days from the demo's reference day to a subject's stated deadline."""
    for _sent, name, _sender, _body, days in _MESSAGES:
        if name == subject:
            return days
    raise KeyError(subject)
