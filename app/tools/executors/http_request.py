from __future__ import annotations

import httpx
import json
from urllib.parse import urlparse

from app.core.config import get_settings
from app.core.outbound_http import validate_outbound_http_url
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
        try:
            validate_outbound_http_url(
                str(url),
                allowed_hosts=get_settings().parsed_tool_http_allowed_hosts,
            )
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc

        body = arguments.get("body")
        data = None if body is None else json.dumps(body).encode("utf-8")
        caller_headers = dict(arguments.get("headers", {}))
        configured_headers = tool.implementation.config.get("headers", {})
        if configured_headers and arguments.get("url") and str(url) != str(
                tool.implementation.config.get("url") or tool.implementation.target
        ):
            raise ToolExecutionError("Tools with configured credentials cannot override their destination URL")
        headers = {
            "Content-Type": "application/json",
            **caller_headers,
            **configured_headers,
        }
        response = httpx.request(
            method,
            str(url),
            content=data,
            headers=headers,
            timeout=tool.implementation.config.get("timeout", 30),
            follow_redirects=False,
            trust_env=False,
        )
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return {"status_code": response.status_code, "body": response.json()}
        return {"status_code": response.status_code, "body": response.text}
