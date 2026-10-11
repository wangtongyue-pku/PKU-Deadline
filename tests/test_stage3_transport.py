import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import pytest

from pku_deadline_agent.ai_client import APIError, HTTPSClient
from pku_deadline_agent.ai_config import AISettings, load_settings
from pku_deadline_agent.usage import UsageLedger


def settings(provider="DeepSeek", **changes):
    base = AISettings(provider, "deepseek-flash" if provider == "DeepSeek" else "mimo-v2.6-flash", True, "https://api.deepseek.com" if provider == "DeepSeek" else "https://api.xiaomimimo.com/v1", "fictional-only-key", Decimal("2"), Decimal("8"), Decimal("1"), 2048, 5, True)
    return replace(base, **changes)


class FakeResponse:
    status = 200

    def read(self, size):
        return b'{"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":0}}'


class FakeConnection:
    calls = []
    response = FakeResponse()
    fail = None

    def __init__(self, host, **options):
        self.options = options
        self.host = host
        self.closed = False
        self.calls.append(self)

    def request(self, method, path, *, body, headers):
        self.request_data = (method, path, body, headers)
        if self.fail:
            raise self.fail

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    FakeConnection.calls = []
    FakeConnection.response = FakeResponse()
    FakeConnection.fail = None
    monkeypatch.setattr("pku_deadline_agent.ai_client.http.client.HTTPSConnection", FakeConnection)
    return HTTPSClient()


@pytest.mark.parametrize("provider, path, auth", [("DeepSeek", "/chat/completions", "Authorization"), ("MiMo", "/v1/chat/completions", "api-key")])
def test_official_transport_uses_expected_auth_exact_body_tls_and_timeout(transport, provider, path, auth):
    config = settings(provider)
    body = '{"model":"fictional-model","messages":[]}'
    response = transport.complete(body, config, ())
    connection = FakeConnection.calls[0]
    method, actual_path, actual_body, headers = connection.request_data
    assert (method, actual_path, actual_body) == ("POST", path, body.encode())
    assert "fictional-only-key" in headers[auth]
    assert "fictional-only-key" not in body
    assert connection.options["timeout"] == 5
    assert connection.options["context"].check_hostname
    assert connection.closed
    assert response["usage"]["prompt_tokens"] == 1
    assert "fictional-only-key" not in repr(config)


@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 429, 500])
def test_no_redirects_no_retries_no_response_body_in_errors(transport, status):
    FakeConnection.response.status = status
    with pytest.raises(APIError) as exc:
        transport.complete("{}", settings(), ())
    assert len(FakeConnection.calls) == 1
    assert FakeConnection.calls[0].closed
    assert "fictional-only-key" not in str(exc.value)


def test_timeout_errors_hide_arbitrary_exception_and_do_not_retry(transport):
    FakeConnection.fail = TimeoutError("fictional-only-key")
    with pytest.raises(APIError, match="超时") as exc:
        transport.complete("{}", settings(), ())
    assert "fictional-only-key" not in str(exc.value)
    assert len(FakeConnection.calls) == 1


@pytest.mark.parametrize("change", [
    {"base_url": "http://api.deepseek.com"}, {"base_url": "https://api.deepseek.com.evil.invalid"},
    {"base_url": "https://api.deepseek.com?api_key=fake"}, {"base_url": "https://fake@api.deepseek.com"},
    {"base_url": "https://api.deepseek.com:443"}, {"base_url": "https://api.deepseek.com/beta"},
    {"base_url": "https://api.xiaomimimo.com/v1"}, {"api_key": ""}, {"api_key": "fake\r\ninjected: x"},
    {"enabled": False}, {"timeout": 0}, {"max_output_tokens": 0}, {"input_price": Decimal("NaN")},
    {"daily_limit": Decimal("0")}, {"input_price": Decimal("0")}, {"model": "fake?key=x"},
], ids=["http", "suffix-host", "url-key", "userinfo", "port", "unsupported-path", "wrong-provider", "no-key", "header-injection", "disabled", "timeout", "output", "nan-price", "zero-limit", "zero-price", "model-injection"])
def test_bad_settings_block_before_connecting(transport, change):
    with pytest.raises(ValueError):
        transport.complete("{}", settings(**change), ())
    assert not FakeConnection.calls


def test_mock_settings_do_not_read_ai_environment(monkeypatch):
    monkeypatch.setattr("pku_deadline_agent.ai_config.os.environ", {})
    config = load_settings("DeepSeek")
    assert not config.live and not config.api_key
    assert config.model == "deepseek-flash"


def test_live_environment_is_independent_per_provider_and_key_hidden(monkeypatch):
    monkeypatch.setenv("PKU_REAL_API_ENABLED", "1")
    monkeypatch.setenv("PKU_DEEPSEEK_API_KEY", "fake-deepseek")
    monkeypatch.setenv("PKU_MIMO_API_KEY", "fake-mimo")
    monkeypatch.setenv("PKU_MIMO_MODEL", "mimo-v2.6-pro")
    deepseek, mimo = load_settings("DeepSeek", live=True), load_settings("MiMo", live=True)
    assert deepseek.api_key == "fake-deepseek" and mimo.api_key == "fake-mimo"
    assert mimo.model == "mimo-v2.6-pro"
    assert mimo.output_price == 6
    assert "fake-mimo" not in repr(mimo)


def test_unknown_model_requires_explicit_nonzero_prices(monkeypatch):
    monkeypatch.setenv("PKU_REAL_API_ENABLED", "1")
    monkeypatch.setenv("PKU_MIMO_API_KEY", "fake-mimo")
    with pytest.raises(ValueError):
        load_settings("MiMo", live=True, model="future-model")
    monkeypatch.setenv("PKU_MIMO_INPUT_PRICE", "10")
    monkeypatch.setenv("PKU_MIMO_OUTPUT_PRICE", "20")
    assert load_settings("MiMo", live=True, model="future-model").input_price == 10


def test_atomic_reservations_across_concurrent_connections(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    ledger.summary()  # Create schema before concurrent reservations.
    config = settings(input_price=Decimal("1"), output_price=Decimal("1"), max_output_tokens=128, daily_limit=Decimal("0.000256"))

    def reserve_once(_):
        try:
            return ledger.reserve(config, 128)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(reserve_once, range(4)))
    assert sum(item is not None for item in outcomes) == 1
    assert ledger.summary()["charged_micros"] == 256


def test_failed_yesterday_request_remains_reserved_today(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    config = settings()
    ledger.reserve(config, 1000, day="2026-10-10")
    summary = ledger.summary("2026-10-11")
    assert summary["uncertain"] == 1
    with pytest.raises(ValueError):
        ledger.reserve(replace(config, daily_limit=Decimal("0.02")), 1000, day="2026-10-11")


def test_usage_database_contains_only_metadata_never_keys_or_prompts(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    request_id = ledger.reserve(settings(), 100)
    ledger.settle(request_id, 40, 10, valid_result=False)
    with sqlite3.connect(ledger.path) as connection:
        dump = "\n".join(connection.iterdump())
    assert "fictional-only-key" not in dump
    assert "messages" not in dump and "notices" not in dump
    assert "invalid_result" in dump
    assert ledger.summary()["charged_micros"] == 160
