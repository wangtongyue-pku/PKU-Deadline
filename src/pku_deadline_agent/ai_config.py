"""Provider settings. Only explicit live mode reads keys; repr never includes them."""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit


# CNY / million tokens, conservative cache-miss / peak rates; reviewed 2026-10-11.
PROVIDERS = {
    "DeepSeek": ("https://api.deepseek.com", {"deepseek-flash": ("2", "8"), "deepseek-v4-pro": ("9", "27")}),
    "MiMo": ("https://api.xiaomimimo.com/v1", {"mimo-v2.6-flash": ("1", "2"), "mimo-v2.6-pro": ("3", "6")}),
}


def decimal_setting(value: str, label: str, *, allow_zero: bool = False) -> Decimal:
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}必须是有限的非负数。") from None
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0) or number > 1_000_000:
        raise ValueError(f"{label}超出允许范围。")
    return number


@dataclass(frozen=True)
class AISettings:
    provider: str
    model: str
    live: bool = False
    base_url: str = ""
    api_key: str = field(default="", repr=False, compare=False)
    input_price: Decimal = Decimal("0")
    output_price: Decimal = Decimal("0")
    daily_limit: Decimal = Decimal("1")
    max_output_tokens: int = 2048
    timeout: int = 30
    enabled: bool = False

    def validate(self) -> None:
        if self.provider not in PROVIDERS or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", self.model):
            raise ValueError("提供方或模型名称无效。")
        if type(self.max_output_tokens) is not int or not 128 <= self.max_output_tokens <= 4096:
            raise ValueError("输出上限必须在 128–4096 tokens 之间。")
        if type(self.timeout) is not int or not 5 <= self.timeout <= 60:
            raise ValueError("超时必须在 5–60 秒之间。")
        for value, label in ((self.input_price, "输入单价"), (self.output_price, "输出单价"), (self.daily_limit, "每日上限")):
            decimal_setting(str(value), label, allow_zero=not self.live)
        if self.live:
            # No arbitrary gateway, URL credentials, query, fragment, proxy or redirect.
            official = PROVIDERS[self.provider][0]
            if self.base_url.rstrip("/") not in {official, "https://api.deepseek.com/v1" if self.provider == "DeepSeek" else official}:
                raise ValueError("当前仅支持提供方官方 HTTPS 地址，请核对 Base URL。")
            url = urlsplit(self.base_url)
            if url.username or url.password or url.query or url.fragment or url.port is not None:
                raise ValueError("Base URL 不得包含认证信息、查询参数或端口。")
            if not self.enabled:
                raise ValueError("真实调用尚未启用；请先明确授权并设置 PKU_REAL_API_ENABLED=1。")
            if not self.api_key or any(ord(char) < 33 or ord(char) > 126 for char in self.api_key):
                raise ValueError(
                    "API Key 未配置或格式无效：请在「AI 设置」页保存，或设置 "
                    "PKU_DEEPSEEK_API_KEY / PKU_MIMO_API_KEY 环境变量。"
                )

    def fingerprint(self) -> str:
        # Key rotations invalidate approvals without revealing the key.
        values = {key: str(value) for key, value in self.__dict__.items() if key != "api_key"}
        values["key_digest"] = hashlib.sha256(self.api_key.encode()).hexdigest()
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def load_settings(provider: str, *, live: bool = False, model: str | None = None,
                  key_store=None) -> AISettings:
    """Build settings for one provider.

    ``key_store`` is only used in live mode: it resolves the API key, with the
    environment variable taking priority over anything stored in the Windows
    credential manager (stage 3 behaviour is therefore unchanged for an existing
    setup). The key itself is never logged, returned in a message, or written
    anywhere.
    """
    if provider not in PROVIDERS:
        raise ValueError("不支持的提供方。")
    base, models = PROVIDERS[provider]
    if not live:
        # Simulated mode must not read real API configuration, even if configured.
        return AISettings(provider, model or next(iter(models)))
    if os.environ.get("PKU_REAL_API_ENABLED") != "1":
        # Disabled real mode must not even query the user's credential vault.
        raise ValueError("真实调用尚未启用；请先明确授权并设置 PKU_REAL_API_ENABLED=1。")
    prefix = "PKU_DEEPSEEK" if provider == "DeepSeek" else "PKU_MIMO"
    selected = model or os.environ.get(prefix + "_MODEL", next(iter(models)))
    defaults = models.get(selected, ("", ""))
    try:
        output = int(os.environ.get("PKU_AI_MAX_OUTPUT_TOKENS", "2048"))
        timeout = int(os.environ.get("PKU_AI_TIMEOUT_SECONDS", "30"))
    except ValueError:
        raise ValueError("输出上限和超时必须是整数。") from None
    settings = AISettings(
        provider, selected, True, os.environ.get(prefix + "_BASE_URL", base).rstrip("/"),
        _resolve_api_key(provider, key_store),
        decimal_setting(os.environ.get(prefix + "_INPUT_PRICE", defaults[0]), "输入单价"),
        decimal_setting(os.environ.get(prefix + "_OUTPUT_PRICE", defaults[1]), "输出单价"),
        decimal_setting(os.environ.get("PKU_AI_DAILY_LIMIT_CNY", "1"), "每日上限"),
        output, timeout, os.environ.get("PKU_REAL_API_ENABLED") == "1",
    )
    settings.validate()
    return settings


def _resolve_api_key(provider: str, key_store) -> str:
    """Environment variable first, then secure storage. Never raises for absence."""
    if key_store is not None:
        return key_store.resolve(provider)[0]
    from pku_deadline_agent.api_key_store import ApiKeyStore

    return ApiKeyStore().resolve(provider)[0]
