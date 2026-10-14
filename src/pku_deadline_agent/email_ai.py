"""Mail -> existing privacy/approval/usage gateway; no mailbox or AI transport."""

from dataclasses import dataclass, field
import re

from pku_deadline_agent.email_import import deadline_message_key, EMAIL_TYPE_MAP
from pku_deadline_agent.notices import MAX_INPUT_LENGTH
from pku_deadline_agent.sync import notice_key
from pku_deadline_agent.task_management import NoticeOrigin
from pku_deadline_agent.time_utils import beijing_now


@dataclass(frozen=True)
class EmailAIInput:
    raw: str = field(repr=False)
    records: tuple = field(repr=False)
    reference: object
    analyses: tuple = field(default=(), repr=False)

    def origin(self, candidate, ordinal):
        record = self.records[candidate.source_index - 1]
        # The local mail parser counts event-only dates too. AI only returns
        # tasks: align an unambiguous known deadline with that existing index,
        # otherwise an event before registration would shift every source id.
        if self.analyses:
            dates = self.analyses[candidate.source_index - 1].deadlines
            matching = [index for index, deadline in enumerate(dates, 1)
                        if deadline.due_at is not None and deadline.due_at == candidate.due_at
                        and EMAIL_TYPE_MAP.get(deadline.kind) == candidate.task_type]
            if len(matching) == 1:
                ordinal = matching[0]
        key = "email:" + deadline_message_key(record.stable_key, ordinal)
        return NoticeOrigin("email", notice_key(key), record.sent_at)


def prepare_email_input(analyses):
    if not 1 <= len(analyses) <= 20:
        raise ValueError("每次请选择 1–20 封邮件。")
    records, parts = [], []
    for analysis in analyses:
        message = analysis.record
        if message.body_truncated:
            raise ValueError("所选邮件正文不完整，请取消这封后重试；不会上传被截断的内容。")
        reference = message.sent_at
        prefix = f"发布时间: {reference:%Y-%m-%d %H:%M}\n" if reference else ""
        text = re.sub(r"(?m)^\s*---\s*$", "（邮件分隔线）", message.subject + "\n" + message.body_text)
        parts.append(prefix + text)
        records.append(message)
    raw = "\n---\n".join(parts)
    if len(raw) > MAX_INPUT_LENGTH:
        raise ValueError("所选邮件内容过多，请减少封数后重试，不会静默截断。")
    return EmailAIInput(raw, tuple(records), beijing_now().replace(microsecond=0), tuple(analyses))
