"""Persistent, atomic CNY budget reservations; never persist prompts or secrets."""

import sqlite3
import uuid
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from pku_deadline_agent.ai_config import AISettings
from pku_deadline_agent.time_utils import beijing_now


def micros(amount: Decimal) -> int:
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def token_cost(prompt: int, completion: int, settings: AISettings) -> int:
    return int((prompt * settings.input_price + completion * settings.output_price).to_integral_value(rounding=ROUND_CEILING))


class UsageLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS ai_usage (
                id TEXT PRIMARY KEY, day TEXT NOT NULL, provider TEXT NOT NULL,
                model TEXT NOT NULL, live INTEGER NOT NULL, status TEXT NOT NULL,
                reserved_micros INTEGER NOT NULL, charged_micros INTEGER NOT NULL,
                prompt_tokens INTEGER, completion_tokens INTEGER,
                input_price TEXT NOT NULL, output_price TEXT NOT NULL)""")
            connection.commit()
            yield connection
            connection.commit()
        finally:
            connection.close()

    def reserve(self, settings: AISettings, prompt_bound: int, *, day: str | None = None) -> str:
        settings.validate()
        day = day or beijing_now().date().isoformat()
        reserved = token_cost(prompt_bound, settings.max_output_tokens, settings) if settings.live else 0
        request_id = uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # An unresolved request from a previous day may still incur a bill today.
            spent = connection.execute("SELECT coalesce(sum(charged_micros),0) FROM ai_usage WHERE live=1 AND (day=? OR status='reserved')", (day,)).fetchone()[0]
            if settings.live and spent + reserved > micros(settings.daily_limit):
                raise ValueError("本次预算预留会超过北京时间每日费用上限；未调用 API。")
            connection.execute("INSERT INTO ai_usage VALUES (?,?,?,?,?,'reserved',?,?,NULL,NULL,?,?)", (request_id, day, settings.provider, settings.model, int(settings.live), reserved, reserved, str(settings.input_price), str(settings.output_price)))
        return request_id

    def settle(self, request_id: str, prompt: int, completion: int, *, valid_result: bool) -> None:
        if type(prompt) is not int or type(completion) is not int or not 0 <= prompt <= 2_000_000 or not 0 <= completion <= 2_000_000:
            raise ValueError("API Token 用量无效；保留预算预留。")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM ai_usage WHERE id=?", (request_id,)).fetchone()
            if row is None or row["status"] != "reserved":
                raise ValueError("用量记录状态无效。")
            amount = int((prompt * Decimal(row["input_price"]) + completion * Decimal(row["output_price"])).to_integral_value(rounding=ROUND_CEILING)) if row["live"] else 0
            connection.execute("UPDATE ai_usage SET status=?,charged_micros=?,prompt_tokens=?,completion_tokens=? WHERE id=?", ("success" if valid_result else "invalid_result", amount, prompt, completion, request_id))

    def summary(self, day: str | None = None) -> dict:
        day = day or beijing_now().date().isoformat()
        with self.connect() as connection:
            row = connection.execute("""SELECT count(*) calls, coalesce(sum(charged_micros),0) charged_micros,
                coalesce(sum(prompt_tokens),0) prompt_tokens, coalesce(sum(completion_tokens),0) completion_tokens,
                sum(status='reserved') uncertain FROM ai_usage WHERE live=1 AND (day=? OR status='reserved')""", (day,)).fetchone()
            return dict(row)
