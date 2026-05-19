from __future__ import annotations

import json
import subprocess
from itertools import count
from typing import Any

from app.domain import MCPServerDefinition, MCPTransportType
from .schemas import MCPDiscoverySnapshot, MCPPromptDescriptor, MCPResourceDescriptor, MCPToolDescriptor


class MCPClientError(RuntimeError):
    """Raised when an MCP client operation fails."""


class BaseMCPClient:
    def discover(self) -> MCPDiscoverySnapshot:
        raise NotImplementedError

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class StdioMCPClient(BaseMCPClient):
    def __init__(self, definition: MCPServerDefinition):
        self.definition = definition
        self._ids = count(1)

    def _run_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params or {},
        }
        process = subprocess.Popen(  # noqa: S603
            [self.definition.command, *self.definition.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(json.dumps(payload) + "\n", timeout=30)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise MCPClientError(f"MCP server '{self.definition.id}' timed out during {method}") from exc

        if process.returncode not in {0, None} and not stdout.strip():
            raise MCPClientError(
                stderr.strip() or f"MCP server '{self.definition.id}' exited with code {process.returncode}")

        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise MCPClientError(f"MCP server '{self.definition.id}' returned no response for {method}")

        for line in reversed(lines):
            response = json.loads(line)
            if response.get("id") != payload["id"]:
                continue
            if "error" in response:
                raise MCPClientError(str(response["error"]))
            return response.get("result", {})
        raise MCPClientError(f"MCP server '{self.definition.id}' returned no matching response for {method}")

    def discover(self) -> MCPDiscoverySnapshot:
        if self.definition.transport != MCPTransportType.STDIO:
            raise MCPClientError(f"Unsupported MCP transport '{self.definition.transport.value}'")

        self._run_request("initialize", {"clientInfo": {"name": "agency", "version": "0.1.0"}})
        tools = self._run_request("tools/list")
        resources = self._safe_request("resources/list")
        prompts = self._safe_request("prompts/list")
        return MCPDiscoverySnapshot(
            server_id=self.definition.id,
            tools=[MCPToolDescriptor.model_validate(item) for item in tools.get("tools", [])],
            resources=[MCPResourceDescriptor.model_validate(item) for item in resources.get("resources", [])],
            prompts=[MCPPromptDescriptor.model_validate(item) for item in prompts.get("prompts", [])],
        )

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._run_request("tools/call", {"name": tool_name, "arguments": arguments})
        if isinstance(result, dict):
            return result
        return {"result": result}

    def _safe_request(self, method: str) -> dict[str, Any]:
        try:
            result = self._run_request(method)
        except MCPClientError:
            return {}
        return result if isinstance(result, dict) else {}


class HttpMCPClient(BaseMCPClient):
    def __init__(self, definition: MCPServerDefinition):
        self.definition = definition

    def discover(self) -> MCPDiscoverySnapshot:
        return MCPDiscoverySnapshot(server_id=self.definition.id)

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise MCPClientError(f"HTTP/SSE MCP transport is not implemented yet for '{self.definition.id}'")
