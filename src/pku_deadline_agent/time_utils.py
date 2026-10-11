"""Use Beijing time throughout the application, independent of Windows settings."""

from datetime import datetime
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


def normalize_datetime(value: datetime) -> datetime:
    """Reject ambiguous naive values; convert explicit timezones to Beijing."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区。请使用北京时间 Asia/Shanghai。")
    return value.astimezone(BEIJING)


def format_deadline(value: datetime | None) -> str:
    if value is None:
        return "待确认"
    return normalize_datetime(value).strftime("%Y-%m-%d %H:%M:%S")
