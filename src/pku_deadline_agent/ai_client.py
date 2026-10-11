"""Bounded HTTPS transport, no redirects/retries/proxy/tool execution/body logs."""

import http.client
import json
import socket
import ssl
from urllib.parse import urlsplit

from pku_deadline_agent.ai_config import AISettings
from pku_deadline_agent.extraction import local_candidates, parse_json
from pku_deadline_agent.notices import FilteredNotice


class APIError(ValueError):
    pass


class MockExtractionClient:
    def __init__(self):
        self.requests: list[str] = []

    def complete(self, request_json: str, settings: AISettings, notices: tuple[FilteredNotice, ...]) -> dict:
        if settings.live:
            raise APIError("本地模拟器不能进行真实调用。")
        self.requests.append(request_json)
        tasks, _ = local_candidates(notices)
        content = json.dumps({"tasks": [task.to_dict() for task in tasks]}, ensure_ascii=False)
        return {"choices": [{"finish_reason": "stop", "message": {"content": content}}], "usage": {"prompt_tokens": len(request_json.encode()) // 3, "completion_tokens": len(content.encode()) // 3}}


class HTTPSClient:
    def complete(self, request_json: str, settings: AISettings, notices: tuple[FilteredNotice, ...]) -> dict:
        settings.validate()
        if not settings.live:
            raise APIError("真实客户端不能用于模拟模式。")
        url = urlsplit(settings.base_url)
        headers = {"Content-Type": "application/json"}
        if settings.provider == "MiMo":
            headers["api-key"] = settings.api_key
        else:
            headers["Authorization"] = "Bearer " + settings.api_key
        connection = http.client.HTTPSConnection(url.hostname, timeout=settings.timeout, context=ssl.create_default_context())
        try:
            connection.request("POST", url.path.rstrip("/") + "/chat/completions", body=request_json.encode("utf-8"), headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                # Never reveal the vendor's response or URL/headers in the exception.
                raise APIError("API 请求失败（认证、额度、限流或服务状态）；未自动重试，预算暂按预留计。")
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise APIError("API 响应过长，已拒绝处理；预算暂按预留计。")
            return parse_json(raw.decode("utf-8"))
        except (TimeoutError, socket.timeout):
            raise APIError("API 请求超时，服务端可能已计费；未自动重试，预算保留。") from None
        except APIError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise APIError("API 连接或响应无效；未自动重试，预算暂按预留计。") from None
        finally:
            connection.close()
