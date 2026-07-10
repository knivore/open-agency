from __future__ import annotations

import httpx
import json
from urllib.parse import urlparse

from app.domain import ToolDefinition, ToolType
from app.runtime.native.errors import ToolExecutionError
from .base import ToolExecutionContext


class HttpRequestToolExecutor:
    tool_type = ToolType.HTTP_REQUEST

    def execute(self, tool: ToolDefinition, arguments: dict[str, object], context: ToolExecutionContext) -> dict[
        str, object]:
        url = arguments.get("url") or tool.implementation.config.get("url") or tool.implementation.target
        method = str(arguments.get("method") or tool.implementation.config.get("method") or "GET").upper()
        parsed = urlparse(str(url))
        allowlisted_domains = set(tool.security.allowlisted_domains)
        if parsed.scheme not in {"http", "https"}:
            raise ToolExecutionError(f"URL scheme '{parsed.scheme or '<missing>'}' is not allowed for tool '{tool.id}'")
        if not parsed.hostname or parsed.hostname not in allowlisted_domains:
            raise ToolExecutionError(f"Domain '{parsed.hostname}' is not allowlisted for tool '{tool.id}'")

        body = arguments.get("body")
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            **tool.implementation.config.get("headers", {}),
            **dict(arguments.get("headers", {})),
        }
        response = httpx.request(
            method,
            str(url),
            content=data,
            headers=headers,
            timeout=tool.implementation.config.get("timeout", 30),
        )
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return {"status_code": response.status_code, "body": response.json()}
        return {"status_code": response.status_code, "body": response.text}
