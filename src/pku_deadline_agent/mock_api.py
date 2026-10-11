"""An in-memory stand-in, with no HTTP client, URL, key, or model invocation."""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class MockReceipt:
    request_id: str
    notice_count: int


class MockAPI:
    def __init__(self) -> None:
        self._requests: list[str] = []

    @property
    def requests(self) -> tuple[str, ...]:
        return tuple(self._requests)

    def send(self, request_json: str) -> MockReceipt:
        """Record the exact preview bytes locally; do not parse deadlines/save tasks."""
        body = json.loads(request_json)
        notices = json.loads(body["messages"][1]["content"])["notices"]
        self._requests.append(request_json)
        return MockReceipt(f"mock-{len(self._requests)}", len(notices))
