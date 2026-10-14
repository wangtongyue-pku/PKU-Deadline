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

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pku_deadline_agent.email_adapter import DEFAULT_FOLDER, MockEmailAdapter
from pku_deadline_agent.time_utils import beijing_now


@dataclass(frozen=True)
class DemoDeadline:
    """One date a demo message states, as an offset from the demo's reference day."""

    days_from_now: int
    hour: int = 20
    minute: int = 0


@dataclass(frozen=True)
class DemoMessage:
    """One fictional mail. ``dates`` is also the placeholder order in ``body``."""

    sent_days_ago: int
    subject: str
    sender: str
    body: str            # uses {0}, {1} … for the dates, in `dates` order
    dates: tuple[DemoDeadline, ...] = field(default_factory=tuple)


# Deadline offsets are chosen so the board shows a 3-day (高) mail and several
# further-out ones, and so a mail with two dates states two *different* dates —
# an event and its registration deadline are not the same day.
_MESSAGES = (
    DemoMessage(4, "虚构课程作业通知", "教务处 <jwc@pku.edu.cn>",
                "《虚构课程》第三次作业提交截止{0}，请按时提交。",
                (DemoDeadline(2),)),
    DemoMessage(3, "虚构学术讲座报名", "讲座组委会 <lecture@pku.edu.cn>",
                "报名截止{0}，讲座将于{1}在虚构楼举行。",
                (DemoDeadline(5, 15), DemoDeadline(12, 14))),
    DemoMessage(2, "虚构奖学金材料提交", "学生工作部 <xgb@pku.edu.cn>",
                "请于{0}前提交申请材料，材料不齐不予受理。",
                (DemoDeadline(12),)),
    DemoMessage(5, "虚构竞赛报名通知", "竞赛组委会 <contest@pku.edu.cn>",
                "ACM 虚构竞赛报名截止{0}，欢迎组队参加。",
                (DemoDeadline(9, 15),)),
    DemoMessage(1, "虚构科研助理招募", "虚构课题组 <lab@pku.edu.cn>",
                "课题组招募研究助理，有意者请于{0}前提交材料。",
                (DemoDeadline(6),)),
    DemoMessage(0, "虚构活动时间待定", "学生会 <union@pku.edu.cn>",
                "虚构校园活动具体时间另行通知，报名从速。"),
    DemoMessage(6, "虚构校园生活小贴士", "后勤 <logistics@pku.edu.cn>",
                "虚构食堂本周开放时间调整，请留意。"),
)

DEMO_SUBJECTS = tuple(message.subject for message in _MESSAGES)


def demo_now() -> datetime:
    """The reference "now" for one demo mailbox, in Beijing time.

    Rounded to 09:00 so the demo's stated times read like a real notice
    (「…日 15:00」) instead of inheriting whatever minute the reviewer happened to
    open the page.
    """
    return beijing_now().replace(hour=9, minute=0, second=0, microsecond=0)


def _format(moment: datetime) -> str:
    """A natural Chinese date phrase; includes a time only when it is not midnight."""
    if moment.hour or moment.minute:
        return f"{moment.year}年{moment.month}月{moment.day}日{moment.hour}:{moment.minute:02d}"
    return f"{moment.year}年{moment.month}月{moment.day}日"


def _raw_message(subject: str, sender: str, body: str, sent_at: datetime) -> bytes:
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


def _moment(now: datetime, item: DemoDeadline) -> datetime:
    """The demo clock time on the target day.

    The day offset moves the date; the hour is a *clock time on that day*, not
    extra elapsed time. Adding both as one timedelta would silently push a 15:00
    deadline into the next day.
    """
    target = (now + timedelta(days=item.days_from_now)).date()
    return datetime(target.year, target.month, target.day,
                    item.hour, item.minute, tzinfo=now.tzinfo)


def demo_mailbox() -> MockEmailAdapter:
    """A fresh offline INBOX holding the fictional messages above."""
    now = demo_now()
    mailbox = {}
    for index, message in enumerate(_MESSAGES, start=1):
        text = message.body.format(*[
            _format(_moment(now, item)) for item in message.dates
        ])
        sent_at = now - timedelta(days=message.sent_days_ago)
        mailbox[index] = _raw_message(message.subject, message.sender, text, sent_at)
    return MockEmailAdapter({DEFAULT_FOLDER: mailbox})


def demo_deadline_days(subject: str) -> tuple[int, ...]:
    """Days from the demo's reference day to each date a subject states."""
    for message in _MESSAGES:
        if message.subject == subject:
            return tuple(item.days_from_now for item in message.dates)
    raise KeyError(subject)
