"""One privacy/approval boundary for local, simulated and real extraction."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from pku_deadline_agent.ai_client import HTTPSClient, MockExtractionClient
from pku_deadline_agent.ai_config import AISettings
from pku_deadline_agent.extraction import ExtractedTask, SYSTEM_PROMPT, local_candidates, validate_tasks
from pku_deadline_agent.notices import FilteredBatch, FilteredNotice, filter_notices
from pku_deadline_agent.preview import context_fingerprint
from pku_deadline_agent.privacy import scan_privacy
from pku_deadline_agent.usage import UsageLedger, token_cost
from pku_deadline_agent.task_management import ImportDecision, origin_for_notice


@dataclass(frozen=True)
class ParsePlan:
    fingerprint: str
    batch: FilteredBatch = field(repr=False)
    route: str
    request_json: str | None = field(repr=False)
    ai_notices: tuple[FilteredNotice, ...] = field(repr=False)
    local_tasks: tuple[ExtractedTask, ...] = field(repr=False)
    prompt_bound: int = 0
    reserve_micros: int = 0


def fingerprint(raw: str, published_at: datetime | None, mode: str, settings: AISettings) -> str:
    return hashlib.sha256((context_fingerprint(raw, published_at, settings.provider, mode) + settings.fingerprint()).encode()).hexdigest()


def make_plan(raw: str, published_at: datetime | None, mode: str, settings: AISettings) -> ParsePlan:
    settings.validate()
    batch = filter_notices(raw, published_at, mode=mode)
    digest = fingerprint(raw, published_at, mode, settings)
    if batch.blocked:
        return ParsePlan(digest, batch, "blocked", None, (), ())
    local, unresolved = [], []
    for notice in batch.notices:
        candidates, complete = local_candidates((notice,))
        if complete:
            local.extend(validate_tasks(json.dumps({"tasks": [task.to_dict() for task in candidates]}, ensure_ascii=False), (notice,)))
        else:
            unresolved.append(notice)
    if not unresolved:
        return ParsePlan(digest, batch, "local", None, (), tuple(local))
    content = {"timezone": "Asia/Shanghai", "notices": [notice.to_payload() for notice in unresolved]}
    body = {
        "model": settings.model, "stream": False, "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(content, ensure_ascii=False)}],
    }
    body["max_tokens" if settings.provider == "DeepSeek" else "max_completion_tokens"] = settings.max_output_tokens
    request = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2)
    # Byte count + envelope allowance is intentionally pessimistic for text-only APIs.
    bound = len(request.encode("utf-8")) + 4096
    reservation = token_cost(bound, settings.max_output_tokens, settings) if settings.live else 0
    return ParsePlan(digest, batch, "live" if settings.live else "mock", request, tuple(unresolved), tuple(local), bound, reservation)


class ParseSession:
    def __init__(self):
        self.plan: ParsePlan | None = None
        self.results: tuple[ExtractedTask, ...] | None = None
        self.mock = MockExtractionClient()
        self.consumed = False
        self.saved = False
        self._digest: str | None = None

    def invalidate(self):
        self.plan = None
        self.results = None
        self.consumed = False
        self.saved = False

    def observe(self, raw, published_at, mode, settings) -> bool:
        digest = fingerprint(raw, published_at, mode, settings)
        if digest == self._digest:
            return False
        self._digest = digest
        self.invalidate()
        return True

    def prepare(self, raw, published_at, mode, settings) -> ParsePlan:
        self.observe(raw, published_at, mode, settings)
        self.invalidate()
        self.plan = make_plan(raw, published_at, mode, settings)
        return self.plan

    def run(self, raw, published_at, mode, settings, ledger: UsageLedger, *, confirmed: bool, real_confirmed: bool = False, client=None):
        if not confirmed:
            raise ValueError("请先查看脱敏预览并主动确认本次解析。")
        if self.observe(raw, published_at, mode, settings) or self.plan is None or self.consumed:
            raise ValueError("没有有效的未使用预览，请重新扫描并确认。")
        current = make_plan(raw, published_at, mode, settings)
        if current != self.plan or current.batch.blocked:
            self.invalidate()
            raise ValueError("隐私预览校验失败，已阻断解析。")
        if current.route == "live" and not real_confirmed:
            raise ValueError("真实 API（含首次调用）需要单独明确授权本次上传和额度使用。")
        self.consumed = True  # Errors/timeouts cannot reuse an approval.
        self.results = None
        if current.route == "local":
            self.results = current.local_tasks
            return self.results
        request_id = ledger.reserve(settings, current.prompt_bound)
        transport = client if client is not None else HTTPSClient() if settings.live else self.mock
        try:
            response = transport.complete(current.request_json, settings, current.ai_notices)
        except Exception:
            # Preserve reservation. Never surface an arbitrary transport exception,
            # which may contain a key, URL, request, or private server body.
            raise ValueError("API 未能完成（可能超时、认证或服务错误）；没有自动重试，预算保留，请核对提供方账单。") from None
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            raise ValueError("API 缺少有效 Token 用量，预算保留，未保存结果。")
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        valid = False
        try:
            choices = response.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or choices[0].get("finish_reason") != "stop":
                raise ValueError("API 输出截断、拒绝或数量无效；未保存结果。")
            message = choices[0].get("message")
            if not isinstance(message, dict) or message.get("tool_calls"):
                raise ValueError("API 返回了非文本结果；工具调用永不执行。")
            tasks = validate_tasks(message.get("content"), current.ai_notices)
            valid = True
        except (ValueError, TypeError, AttributeError, KeyError):
            raise ValueError("模型 JSON、字段、时间依据或隐私校验未通过；未保存结果，请核对后重新预览。") from None
        finally:
            ledger.settle(request_id, prompt, completion, valid_result=valid)
        if prompt > current.prompt_bound or completion > settings.max_output_tokens:
            raise ValueError("API 用量超过预设边界，已记录费用；请先核对提供方配置，结果未保存。")
        self.results = tuple(sorted((*current.local_tasks, *tasks), key=lambda task: task.source_index))
        return self.results

    def save(self, repository, raw, published_at, mode, settings, selected: tuple[ExtractedTask, ...], *, confirmed: bool, decisions=None, origins=None):
        if not confirmed:
            raise ValueError("请先审核最终任务并确认保存。")
        if self.observe(raw, published_at, mode, settings) or self.results is None or self.saved:
            raise ValueError("解析结果已失效或已保存，请重新预览。")
        if not selected:
            raise ValueError("请至少选择一条任务。")
        # Only user-visible title/category/type/due may be edited; evidence and
        # provenance must still belong to a reviewed candidate.
        for item in selected:
            if not any((item.source_index, item.deadline_text, item.deadline_changed, item.review_notes) == (base.source_index, base.deadline_text, base.deadline_changed, base.review_notes) for base in self.results):
                raise ValueError("任务来源或审核信息已改变。")
            if scan_privacy(json.dumps(item.to_dict(), ensure_ascii=False)).findings:
                raise ValueError("最终任务含疑似个人信息或认证内容，请修改后保存。")
        sources = {notice.source_index: notice for notice in self.plan.batch.notices}
        if origins is None:
            origins = tuple(origin_for_notice(sources[item.source_index]) for item in selected)
        if decisions is None:
            decisions = tuple(ImportDecision("new") for item in selected)
        tasks = repository.apply_reviewed_tasks(selected, decisions, origins, confirmed=True)
        self.saved = True
        return tasks
