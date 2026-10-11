"""Shared privacy boundary and dispatch for structured/natural notice filtering."""

import re
from dataclasses import dataclass, field
from datetime import datetime

from pku_deadline_agent.models import TASK_TYPES
from pku_deadline_agent.privacy import Finding, scan_privacy
from pku_deadline_agent.time_utils import BEIJING, normalize_datetime

MAX_INPUT_LENGTH = 20_000
MAX_NOTICES = 20
INPUT_MODES = {"natural": "自然语言通知", "structured": "结构化输入"}

_ALIASES = {
    "标题": "title", "任务": "title", "任务标题": "title", "title": "title",
    "课程": "category", "活动": "category", "课程/活动": "category",
    "course": "category", "activity": "category",
    "截止时间": "deadline_text", "截止日期": "deadline_text", "deadline": "deadline_text",
    "类型": "task_type", "任务类型": "task_type", "tasktype": "task_type",
    "发布时间": "published_at", "发布日期": "published_at", "publishedat": "published_at",
}
_FIELD_LINE = re.compile(r"^\s*([^:=]{1,30})\s*[:=]\s*(.*?)\s*$")
_LIMITS = {"title": 200, "category": 100, "deadline_text": 160, "task_type": 20, "published_at": 50}
# Stage 2 keeps a literal date phrase, not a guessed/resolved datetime.
# Unsupported prose in this field is omitted instead of forwarding the sentence.
_DATE_PHRASE = re.compile(
    r"[0-9年月日号周星期一二三四五六七八九十两〇零天明今后本下个早晨晚午上点时分秒"
    r"前至截止到之内待确认定北京间:\-/ .~—～+()（）]+"
)
_RELATIVE_DATE = re.compile(r"明|今|后天|周|星期|本|下个")


@dataclass(frozen=True)
class FilteredNotice:
    source_index: int
    title: str
    category: str
    task_type: str
    deadline_text: str | None
    published_at: datetime | None
    sanitized_text: str | None = field(default=None, repr=False)

    def to_payload(self) -> dict:
        if self.sanitized_text is not None:
            return {
                "source_index": self.source_index,
                "text": self.sanitized_text,
                "published_at": self.published_at.isoformat() if self.published_at else None,
                "requires_review": True,
            }
        return {
            "source_index": self.source_index,
            "title": self.title,
            "category": self.category,
            "task_type": self.task_type,
            "deadline_text": self.deadline_text,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "requires_review": True,
        }


@dataclass(frozen=True)
class FilteredBatch:
    notices: tuple[FilteredNotice, ...] = ()
    findings: tuple[Finding, ...] = ()
    ignored_lines: int = 0
    skipped_notices: int = 0
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.errors) or any(item.blocks_sending for item in self.findings)


def _parse_publication(value: str) -> datetime | None:
    # A date alone is not an explicit publication timestamp: do not invent midnight.
    if not re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:[+]08:00)?", value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("/", "-"))
    except ValueError:
        return None
    return normalize_datetime(parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None else parsed)


def filter_notices(raw: str, published_at: datetime | None = None, *, mode: str = "structured") -> FilteredBatch:
    """Scan the WHOLE batch before selecting fields; a discarded secret still blocks."""
    if mode not in INPUT_MODES:
        raise ValueError("不支持的通知输入模式。")
    if not raw.strip():
        return FilteredBatch(errors=("请先输入至少一条通知。",))
    if len(raw) > MAX_INPUT_LENGTH:
        return FilteredBatch(errors=("通知过长，请限制在 20000 个字符以内。",))
    if published_at is not None:
        published_at = normalize_datetime(published_at)
    report = scan_privacy(raw)
    if report.blocked:
        return FilteredBatch(findings=report.findings, errors=("检测到认证凭据或不明高风险长串，整批发送已阻断。请在本地移除后重新扫描。",))
    sections = re.split(r"(?m)^[ \t]*---[ \t]*$", report.sanitized_text)
    if len(sections) > MAX_NOTICES:
        return FilteredBatch(findings=report.findings, errors=("每批最多 20 条通知，请分批处理。",))
    if mode == "natural":
        from pku_deadline_agent.natural_notices import filter_natural_sections

        return filter_natural_sections(sections, report.findings, published_at)

    notices: list[FilteredNotice] = []
    errors: list[str] = []
    warnings: list[str] = []
    ignored = skipped = 0
    for index, section in enumerate(sections, start=1):
        values: dict[str, str] = {}
        for line in section.splitlines():
            if not line.strip():
                continue
            match = _FIELD_LINE.fullmatch(line)
            key = re.sub(r"[\s_-]", "", match[1]).casefold() if match else ""
            field_name = _ALIASES.get(key)
            if field_name is None:
                ignored += 1
                continue
            if field_name in values:
                errors.append(f"第 {index} 条通知存在重复字段，请保留一个明确值后重试。")
                continue
            value = match[2].strip()
            if len(value) > _LIMITS[field_name]:
                errors.append(f"第 {index} 条通知的白名单字段过长，请在本地缩短后重试。")
                continue
            values[field_name] = value

        title = values.get("title", "")
        if not title.replace("[已移除敏感信息]", "").strip(" -:.,，。"):
            skipped += 1
            warnings.append(f"第 {index} 条通知没有可用的标题，已排除；请用“标题: …”明确填写任务。")
            continue
        publication = published_at
        if "published_at" in values:
            publication = _parse_publication(values["published_at"])
            if publication is None:
                warnings.append(f"第 {index} 条通知发布时间不明确，保留为未知。")
        deadline = values.get("deadline_text") or None
        if deadline is not None and not _DATE_PHRASE.fullmatch(deadline):
            deadline = None
            warnings.append(f"第 {index} 条通知的截止字段含非日期内容，已排除，截止时间待确认。")
        if deadline and _RELATIVE_DATE.search(deadline) and publication is None:
            warnings.append(f"第 {index} 条通知含相对时间但没有明确发布时间，截止时间待确认。")
        task_type = values.get("task_type", "其他")
        if task_type not in TASK_TYPES:
            task_type = "其他"
            warnings.append(f"第 {index} 条通知类型不在允许范围，已使用“其他”。")
        notices.append(FilteredNotice(index, title, values.get("category", ""), task_type, deadline, publication))
    if not notices:
        errors.append("没有可发送的任务信息。请按白名单格式填写标题等字段。")
    return FilteredBatch(tuple(notices), report.findings, ignored, skipped, tuple(errors), tuple(warnings))
