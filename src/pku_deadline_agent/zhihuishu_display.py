"""Local parsing of the visible deadline format, without a browser or AI.

The supplied screenshot confirms a dotted date and minute-precision clock.
It does not establish DOM selectors, source identity, or completion semantics.
Business-time interpretation is explicitly Beijing, never the host timezone.
"""

import re
from datetime import date, datetime

from pku_deadline_agent.sources import SourceRejected
from pku_deadline_agent.time_utils import BEIJING

_DISPLAY_TIME = re.compile(
    r"(?P<year>[0-9]{4})\.(?P<month>[0-9]{2})\.(?P<day>[0-9]{2})"
    r"(?:[ \t]+(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}))?"
)


def parse_display_deadline(value: str | None) -> tuple[datetime | None, str | None]:
    """Return (explicit timestamp, date-only text) for ONE deadline field.

    Input must already be scoped to the visible deadline field, not a complete
    task card or page. Missing time stays unknown; explicit 00:00 stays midnight
    on the displayed date. No year inference or natural-language interpretation.
    An unsupported or damaged field is an error, never silently "no deadline".
    """
    if value is None:
        return None, None
    if not isinstance(value, str) or len(value) > 80:
        raise SourceRejected("列表截止时间必须是有界文本。")
    text = value.strip()
    text = re.sub(r"^截止时间[ \t]*[:：][ \t]*", "", text)
    if not text:
        return None, None
    match = _DISPLAY_TIME.fullmatch(text)
    if match is None:
        raise SourceRejected("列表截止时间格式未验证，请核对显示字段；不扫描要求正文猜日期。")
    parts = match.groupdict()
    try:
        calendar_day = date(int(parts["year"]), int(parts["month"]), int(parts["day"]))
        if parts["hour"] is None:
            # Feed the existing contract's ISO date-only shape; do not add time.
            return None, calendar_day.isoformat()
        return datetime(calendar_day.year, calendar_day.month, calendar_day.day,
                        int(parts["hour"]), int(parts["minute"]), tzinfo=BEIJING), None
    except ValueError:
        raise SourceRejected("列表截止日期或时刻无效，请核对；不自动修正日期。") from None
