from __future__ import annotations

import json
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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
        if not parsed.hostname or parsed.hostname not in allowlisted_domains:
            raise ToolExecutionError(f"Domain '{parsed.hostname}' is not allowlisted for tool '{tool.id}'")

        body = arguments.get("body")
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            **tool.implementation.config.get("headers", {}),
            **dict(arguments.get("headers", {})),
        }
        request = Request(url=str(url), method=method, data=data, headers=headers)
        with urlopen(request, timeout=tool.implementation.config.get("timeout", 30)) as response:  # noqa: S310
            payload = response.read().decode("utf-8")
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return {"status_code": response.status, "body": json.loads(payload)}
            return {"status_code": response.status, "body": payload}
