"""Resolve dates against a frozen Beijing reference, with explicit year overrides."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from pku_deadline_agent.time_utils import BEIJING, beijing_now, normalize_datetime

CHANGE = re.compile(r"延期|延长|推迟|提前|改为|更改|调整|修正|更正|纠正|取消")
_CHANGE_TARGET = re.compile(r"(?:延期|推迟|延长|提前|更改|调整|修正|更正|纠正|改)\s*(?:截止(?:时间|日期)?\s*)?(?:至|到|为)")
_DATE = re.compile(r"(?P<year>\d{4})[-/年](?P<month>\d{1,2})[-/月](?P<day>\d{1,2})日?")
_MONTH = re.compile(r"(?<![\d/-])(\d{1,2})(?:月(\d{1,2})(?!\d)[日号]?|[-/](\d{1,2})(?![\d/-]))")
_RELATIVE = re.compile(r"(?:下下|下|本|这|上)?(?:周|星期)[一二三四五六日天]|明天|明晚|明早|明日|后天|今天|今晚|今日")
_CLOCK = re.compile(r"(?:(凌晨|早上|上午|中午|下午|晚上|晚|早)\s*)?(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})(?::(\d{2})|[点时](半|(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})分?)?)")


@dataclass(frozen=True)
class DeadlineResolution:
    due_at: datetime | None
    reason: str = ""


def _number(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = "零一二三四五六七八九"
    value = value.replace("两", "二").replace("〇", "零")
    if "十" in value:
        left, right = value.split("十")
        return (digits.index(left) if left else 1) * 10 + (digits.index(right) if right else 0)
    return digits.index(value)


def resolve_deadline(expression: str | None, published_at: datetime | None, *, year_context: str | None = None) -> DeadlineResolution:
    if not expression:
        return DeadlineResolution(None, "没有明确截止时间")
    text = expression.strip()
    if published_at:
        published_at = normalize_datetime(published_at)
    if re.search(r"UTC|GMT|\b(?:EST|EDT|PST|PDT)\b|美东|美西|纽约时间|伦敦时间|东京时间", text, re.I):
        return DeadlineResolution(None, "通知含其他时区标记，请人工核对并转换北京时间")
    # Ranges, alternatives, cancelled/deferred/negated deadlines need a person.
    if re.search(r"或|或者|待定|待确认|另行|取消|不再|不是|并非|尚未|未明确|不确定|可能|预计|未定|左右|大约|暂定|刻|点多|点许|:\d{2}:\d{2}|之前.*之后|[~～—]|\d\s*-\s*\d{1,2}(?:点|时|:)", text):
        return DeadlineResolution(None, "截止表达存在范围、取消或不确定信息")
    dates = list(_DATE.finditer(text))
    months = list(_MONTH.finditer(text)) if not dates else []
    relatives = list(_RELATIVE.finditer(text))
    clocks = list(_CLOCK.finditer(text))
    # A single expression only: old and new deadlines cannot be conflated.
    if len(dates) + len(months) + len(relatives) != 1 or len(clocks) != 1:
        return DeadlineResolution(None, "日期或时刻缺失、多值或含歧义")
    try:
        if dates:
            date = datetime(int(dates[0]["year"]), int(dates[0]["month"]), int(dates[0]["day"]), tzinfo=BEIJING)
        elif months:
            reference = published_at or beijing_now()
            context = year_context if year_context is not None else text
            years = {int(value) for value in re.findall(r"(?<!\d)(\d{4})年", context)}
            offsets = {"今年": 0, "本年": 0, "明年": 1, "后年": 2, "去年": -1}
            years.update(reference.year + offset for phrase, offset in offsets.items() if phrase in context)
            if len(years) > 1:
                return DeadlineResolution(None, "通知涉及多个年份，无法唯一确定截止年份")
            year = next(iter(years)) if years else reference.year
            date = datetime(year, int(months[0][1]), int(months[0][2] or months[0][3]), tzinfo=BEIJING)
        else:
            if not published_at:
                return DeadlineResolution(None, "相对日期缺少通知发布时间")
            phrase = relatives[0][0]
            date = published_at.replace(hour=0, minute=0, second=0, microsecond=0)
            if "周" in phrase or "星期" in phrase:
                if phrase.startswith("上"):
                    return DeadlineResolution(None, "过去的相对周表达需要核对")
                weekday = "一二三四五六日".index(phrase[-1].replace("天", "日"))
                weeks = 2 if phrase.startswith("下下") else 1 if phrase.startswith("下") else 0
                date += timedelta(days=weeks * 7 - date.weekday() + weekday)
            else:
                date += timedelta(days=2 if phrase == "后天" else 1 if phrase.startswith("明") else 0)
        weekdays = re.findall(r"(?:周|星期)([一二三四五六日天])", text)
        if weekdays and any("一二三四五六日".index(day.replace("天", "日")) != date.weekday() for day in weekdays):
            return DeadlineResolution(None, "日期和星期不一致，请核对原通知")
        clock = clocks[0]
        hour = _number(clock[2])
        minute = int(clock[3]) if clock[3] else 30 if clock[4] == "半" else _number(clock[4].rstrip("分")) if clock[4] else 0
        period = clock[1] or ("晚" if "明晚" in text or "今晚" in text else "早" if "明早" in text else "")
        if period in {"晚上", "晚"} and hour in {0, 12}:
            return DeadlineResolution(None, "晚上十二点的日期归属需要人工确认")
        if period == "下午" and hour == 0:
            return DeadlineResolution(None, "时段和时刻矛盾")
        if hour < 12:
            if period in {"下午", "晚上", "晚"}:
                hour += 12
            elif not period and clock[3] is None:
                return DeadlineResolution(None, "十二小时制未说明上午或下午")
        elif hour == 12 and period in {"凌晨", "早上", "上午", "早"}:
            return DeadlineResolution(None, "十二点的时段存在歧义")
        if period == "中午" and hour < 11:
            return DeadlineResolution(None, "中午时刻存在歧义")
        if period in {"凌晨", "早上", "上午", "早"} and hour > 12:
            return DeadlineResolution(None, "时段和时刻矛盾")
        result = date.replace(hour=hour, minute=minute)
        if published_at and result < published_at:
            # A past date remains a known date. Never silently roll it to next year/week.
            return DeadlineResolution(result, "截止时间早于解析时间基准，任务可能已过期；未自动顺延年份或周次")
        return DeadlineResolution(result)
    except (ValueError, IndexError):
        return DeadlineResolution(None, "日期或时刻无效")


def deadline_evidence(text: str) -> str | None:
    """Return one explicit deadline clause. Bare event time is not a deadline."""
    clauses = [part.strip() for part in re.split(r"[\n，,。；;]", text) if part.strip()]
    changed = [part for part in clauses if _CHANGE_TARGET.search(part)]
    if len(changed) == 1:
        # Remove the old expression in the same clause, keeping the new clause.
        return _CHANGE_TARGET.split(changed[0])[-1].strip()
    candidates = [part for part in clauses if re.search(r"截止|(?<!提)交(?:作业|报告|论文)|提交|上交|补交", part)]
    return candidates[0] if len(candidates) == 1 else None
