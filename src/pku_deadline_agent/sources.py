"""Stage 5 source adapter contract.

Platform collection stays behind this boundary so the task logic never depends on
one platform or one third-party tool. This module is offline: it only defines the
data contract, validation and the adapter base class. Real network access belongs
to stage 6 and is not implemented here.

Contract rules:
 - a missing value stays empty; never invent a course, deadline or status;
 - every datetime coming from a platform must carry a timezone and is normalized
   to Asia/Shanghai, so nothing depends on the host clock;
 - records rejected by validation are reported as rejections and are never
   silently dropped or silently accepted.
"""

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from pku_deadline_agent.time_utils import BEIJING, beijing_now, normalize_datetime

# Status literals are local to the adapter boundary. Platform wording is mapped by
# the adapter; an unknown status must become None instead of a guessed value.
STATUS_PENDING = "未完成"
STATUS_DONE = "已完成"
STATUS_CANCELLED = "已取消"
STATUSES = (STATUS_PENDING, STATUS_DONE, STATUS_CANCELLED)

TASK_TYPE_OTHER = "其他"

MAX_IDENTIFIER_LENGTH = 200
MAX_TITLE_LENGTH = 200
MAX_COURSE_LENGTH = 100
MAX_URL_LENGTH = 2000


def normalize_text(value, *, limit: int, label: str) -> str:
    """Trim, collapse whitespace and bound length; never guess a missing value."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceRejected(f"{label}必须是文本。")
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) > limit:
        raise SourceRejected(f"{label}超过 {limit} 个字符。")
    return text


def optional_text(value, *, limit: int, label: str) -> str | None:
    text = normalize_text(value, limit=limit, label=label)
    return text or None


def normalize_optional_datetime(value, label: str) -> datetime | None:
    """Accept only explicit, timezone-aware times; naive values are rejected."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            raise SourceRejected(f"{label}不是有效的 ISO 8601 时间。") from None
    if not isinstance(value, datetime):
        raise SourceRejected(f"{label}必须是 datetime 或 ISO 8601 文本。")
    try:
        return normalize_datetime(value)
    except ValueError:
        raise SourceRejected(f"{label}缺少时区，不能按本机时区解释。") from None


def normalize_status(value) -> str | None:
    """Unknown platform wording becomes None; we never guess completion state."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SourceRejected("完成状态必须是文本。")
    text = value.strip()
    return text if text in STATUSES else None


class SourceError(RuntimeError):
    """Base class for adapter failures that a caller must surface, not swallow."""


class SourceUnavailable(SourceError):
    """Temporary failure: network, DNS, timeout, HTTP 5xx. Retry may help."""


class SourceAuthRequired(SourceError):
    """Session missing or expired. Needs an explicit, user-approved login."""


class SourceRateLimited(SourceError):
    """Platform asked us to slow down. Do not hammer the platform."""


class SourceRejected(SourceError):
    """One record is unusable. Other records in the same batch stay valid."""


class SourceUnsupported(SourceError):
    """The adapter cannot serve this request; say so instead of returning [].

    Returning an empty list would look like "the platform has no tasks", which is
    a different and more dangerous statement than "this adapter cannot do that".
    """


class SourceUnverified(SourceError):
    """The batch is not whole enough to justify removing or completing anything.

    Raised by ``FetchResult.require_trustworthy``. Stage 6 must treat this as a
    stop signal for destructive decisions, not as "the source is empty".
    """


def label_identifier(value) -> str | None:
    """Rejection labels keep the raw platform id, or None when it is missing."""
    text = "" if value is None else str(value).strip()
    return text or None


@dataclass(frozen=True)
class SourceRecord:
    """One task as reported by a platform, already normalized to Beijing time.

    Empty means "the platform did not give this", never "we filled in a default".
    """

    source: str
    source_id: str
    task_title: str
    course: str = ""
    task_type: str = TASK_TYPE_OTHER
    published_at: datetime | None = None
    deadline: datetime | None = None
    source_url: str | None = None
    status: str | None = None
    last_updated: datetime | None = None


@dataclass(frozen=True)
class Rejection:
    """A platform record that could not be used, with the reason for the user."""

    source_id: str | None
    reason: str


@dataclass(frozen=True)
class FetchResult:
    """Normalized output of one adapter call.

    ``records`` never contains rejected items and ``rejections`` never contains
    accepted ones, so a caller can apply the batch or refuse it without guessing.

    Two different questions, two different fields — do not confuse them:

    - ``complete``: could every *entry we read* be converted? It says nothing
      about entries we could not read at all, so ``complete`` is **not** proof
      that the batch is whole.
    - ``trustworthy``: was *everything accounted for*? False whenever the adapter
      had to report something it could not confirm, including unreadable shapes
      and fields that were missing where a task list was expected.
    """

    source: str
    records: tuple[SourceRecord, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    since: datetime | None = None
    fetched_at: datetime | None = None
    warnings: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """False when the adapter had to skip something it could not normalize.

        A True value here only means no entry was rejected. It does **not** mean
        the platform has no tasks, and it does **not** authorize removing local
        tasks. Use ``trustworthy`` / ``confirmed_empty`` before any destructive
        decision.
        """
        return not self.rejections

    @property
    def trustworthy(self) -> bool:
        """True only when nothing was rejected *and* nothing is unconfirmed.

        This is the flag a sync must require before it treats an empty result as
        "this source has no tasks", and before it deletes, cancels or completes
        anything locally.
        """
        return self.complete and not self.warnings

    @property
    def confirmed_empty(self) -> bool:
        """True only when the platform explicitly reported zero tasks.

        Requires ``trustworthy``: an empty ``records`` list caused by damage or by
        an unconfirmed field must never satisfy this.
        """
        return self.trustworthy and not self.records

    def require_trustworthy(self) -> None:
        """Raise unless this batch can safely drive destructive sync decisions."""
        if not self.trustworthy:
            reasons = len(self.rejections) + len(self.warnings)
            raise SourceUnverified(
                f"本次结果有 {reasons} 项无法确认，不能据此删除、取消或标记完成本地任务。"
            )


@dataclass(frozen=True)
class SourceCapabilities:
    """What an adapter can really do, so callers can stop guessing."""

    readable: tuple[str, ...]
    incremental: bool = False
    reports_status: bool = False
    needs_login: bool = False
    notes: str = ""


def validate_record(record: SourceRecord, *, source: str) -> SourceRecord:
    """Check and normalize one record. Another adapter's ``source`` is rejected."""
    if not isinstance(record, SourceRecord):
        raise SourceRejected("适配器必须返回 SourceRecord。")
    if record.source != source:
        raise SourceRejected(f"记录来源 {record.source!r} 与适配器 {source!r} 不一致。")
    source_id = normalize_text(record.source_id, limit=MAX_IDENTIFIER_LENGTH, label="来源任务标识")
    if not source_id:
        raise SourceRejected("必须提供来源任务标识，否则无法稳定去重。")
    title = normalize_text(record.task_title, limit=MAX_TITLE_LENGTH, label="任务标题")
    if not title:
        raise SourceRejected("任务标题为空，不能编造标题。")
    status = normalize_status(record.status)
    url = optional_text(record.source_url, limit=MAX_URL_LENGTH, label="原始链接")
    if url and not re.match(r"^https?://", url):
        raise SourceRejected("原始链接必须是 http 或 https 地址。")
    return SourceRecord(
        source=source,
        source_id=source_id,
        task_title=title,
        course=normalize_text(record.course, limit=MAX_COURSE_LENGTH, label="课程名称"),
        task_type=normalize_text(record.task_type, limit=MAX_COURSE_LENGTH, label="任务类型") or TASK_TYPE_OTHER,
        published_at=normalize_optional_datetime(record.published_at, "发布时间"),
        deadline=normalize_optional_datetime(record.deadline, "截止时间"),
        source_url=url,
        status=status,
        last_updated=normalize_optional_datetime(record.last_updated, "最后更新时间"),
    )


def external_key(record: SourceRecord) -> str:
    """Deterministic dedup key: platform identity, never fuzzy title matching.

    Stage 4 fuzzy matching stays a separate, user-reviewed suggestion layer; this
    key only answers "is this the same platform object?".
    """
    if not record.source_id:
        raise SourceRejected("缺少来源任务标识，无法生成去重键。")
    return f"{record.source}:{record.source_id}"


def notice_key(external: str) -> str:
    """Hash an external key into the 64-hex form the task database stores.

    Stage 4 already keeps a stable source key per linked task, so stage 6 reuses
    that column instead of adding a mapping table. Hashing keeps it a fixed width
    and avoids depending on the length of a platform identifier.
    """
    if not isinstance(external, str) or not re.fullmatch(r"[a-z_]+:[0-9A-Za-z._:-]{1,190}", external):
        raise SourceRejected("来源标识格式无效，已中止而不是生成可能碰撞的键。")
    return hashlib.sha256(external.encode("utf-8")).hexdigest()


def sort_records(records) -> tuple[SourceRecord, ...]:
    """Stable output order: known deadlines first, then by source id.

    Platform order is not trusted, so two runs over the same data compare equal.
    A sentinel keeps the key deterministic instead of reading the clock.
    """
    latest = datetime.max.replace(tzinfo=BEIJING)
    return tuple(sorted(records, key=lambda item: (item.deadline is None, item.deadline or latest, item.source_id)))


class SourceAdapter(ABC):
    """Base class for every platform. Subclasses implement ``_load`` only.

    A subclass must not perform network access in ``__init__``; nothing happens
    until ``fetch`` is called with an explicit sync window.
    """

    source: str = ""
    display_name: str = ""
    capabilities: SourceCapabilities = SourceCapabilities(readable=())

    def __init__(self) -> None:
        if not self.source:
            raise ValueError("适配器必须声明 source。")

    @abstractmethod
    def _load(self, since: datetime | None) -> FetchResult:
        """Return platform data. Raise SourceError subclasses on failure."""

    def fetch(self, *, since: datetime | None = None) -> FetchResult:
        """Fetch, validate and stabilize one batch.

        ``since`` is an inclusive lower bound on the platform's update time. An
        adapter that cannot filter incrementally must raise SourceUnsupported
        rather than silently returning the full history.
        """
        if since is not None:
            since = normalize_optional_datetime(since, "增量起点")
            if not self.capabilities.incremental:
                raise SourceUnsupported(f"{self.display_name or self.source} 不支持增量同步，请申请全量数据。")
        raw = self._load(since)
        if not isinstance(raw, FetchResult):
            raise SourceRejected("适配器内部错误：_load 必须返回 FetchResult。")
        records, rejections = [], list(raw.rejections)
        for item in raw.records:
            try:
                records.append(validate_record(item, source=self.source))
            except SourceRejected as error:
                rejections.append(Rejection(label_identifier(getattr(item, "source_id", None)), str(error)))
        return FetchResult(
            source=self.source,
            records=sort_records(records),
            rejections=tuple(rejections),
            since=since,
            fetched_at=normalize_datetime(raw.fetched_at or beijing_now()),
            warnings=tuple(raw.warnings),
        )


@dataclass
class AdapterInfo:
    """Registry entry: metadata only, so listing adapters runs no platform code."""

    source: str
    display_name: str
    capabilities: SourceCapabilities
    factory: object = field(repr=False, default=None)


_REGISTRY: dict[str, AdapterInfo] = {}


def register_adapter(adapter_class) -> object:
    """Register an adapter class so the UI can list sources without them running."""
    source = getattr(adapter_class, "source", "")
    if not source or not isinstance(source, str):
        raise ValueError("注册适配器前必须声明字符串 source。")
    if source in _REGISTRY:
        raise ValueError(f"适配器来源 {source!r} 已注册，请勿重复注册。")
    _REGISTRY[source] = AdapterInfo(source, getattr(adapter_class, "display_name", source), adapter_class.capabilities, adapter_class)
    return adapter_class


def adapter_sources() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def describe_adapters() -> tuple[AdapterInfo, ...]:
    return tuple(_REGISTRY[source] for source in sorted(_REGISTRY))


def create_adapter(source: str, **options) -> SourceAdapter:
    """Instantiate a registered adapter. Real credentials are never passed here."""
    if source not in _REGISTRY:
        raise ValueError(f"没有注册来源为 {source!r} 的适配器。")
    return _REGISTRY[source].factory(**options)
