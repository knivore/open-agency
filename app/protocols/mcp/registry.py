from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.domain import MCPServerDefinition
from .client import HttpMCPClient, MCPClientError, StdioMCPClient
from .prompt_adapter import mcp_prompt_to_dict
from .resource_adapter import mcp_resource_to_dict
from .schemas import MCPDiscoverySnapshot
from .tool_adapter import mcp_tool_to_definition


class MCPRegistryError(RuntimeError):
    """Raised when MCP registry operations fail."""


class MCPClientRegistry:
    def __init__(self, *, allowlisted_commands: list[str] | None = None):
        env_allowlist = [item.strip() for item in os.getenv("MCP_SERVER_COMMAND_ALLOWLIST", "").split(",") if
                         item.strip()]
        self.allowlisted_commands = set(allowlisted_commands or env_allowlist)
        self._servers: dict[str, MCPServerDefinition] = {}
        self._snapshots: dict[str, MCPDiscoverySnapshot] = {}

    def register(self, definition: MCPServerDefinition) -> None:
        self._servers[definition.id] = definition

    def get(self, server_id: str) -> MCPServerDefinition:
        if server_id not in self._servers:
            raise MCPRegistryError(f"MCP server '{server_id}' was not found")
        return self._servers[server_id]

    def list(self) -> list[MCPServerDefinition]:
        return list(self._servers.values())

    def _validate_server(self, definition: MCPServerDefinition) -> None:
        if not definition.enabled:
            raise MCPRegistryError(f"MCP server '{definition.id}' is disabled")
        command_name = definition.allowlisted_command or Path(definition.command).name
        if command_name not in self.allowlisted_commands:
            raise MCPRegistryError(f"MCP server command '{command_name}' is not allowlisted")

    def _client_for(self, definition: MCPServerDefinition):
        self._validate_server(definition)
        if definition.transport.value == "stdio":
            return StdioMCPClient(definition)
        return HttpMCPClient(definition)

    def discover(self, server_id: str | None = None) -> dict[str, MCPDiscoverySnapshot]:
        definitions = [self.get(server_id)] if server_id else self.list()
        discovered: dict[str, MCPDiscoverySnapshot] = {}
        for definition in definitions:
            if not definition.enabled:
                continue
            client = self._client_for(definition)
            snapshot = client.discover()
            self._snapshots[definition.id] = snapshot
            discovered[definition.id] = snapshot
        return discovered

    def get_snapshot(self, server_id: str) -> MCPDiscoverySnapshot | None:
        return self._snapshots.get(server_id)

    def discovered_tool_definitions(self, server_id: str | None = None):
        snapshots = self.discover(server_id)
        tools = []
        for definition_id, snapshot in snapshots.items():
            definition = self.get(definition_id)
            tools.extend(mcp_tool_to_definition(definition, tool) for tool in snapshot.tools)
        return tools

    def discovered_resources(self, server_id: str | None = None):
        snapshots = self.discover(server_id)
        resources = []
        for definition_id, snapshot in snapshots.items():
            definition = self.get(definition_id)
            resources.extend(mcp_resource_to_dict(definition, resource) for resource in snapshot.resources)
        return resources

    def discovered_prompts(self, server_id: str | None = None):
        snapshots = self.discover(server_id)
        prompts = []
        for definition_id, snapshot in snapshots.items():
            definition = self.get(definition_id)
            prompts.extend(mcp_prompt_to_dict(definition, prompt) for prompt in snapshot.prompts)
        return prompts

    def call_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        definition = self.get(server_id)
        client = self._client_for(definition)
        try:
            return client.call_tool(tool_name, arguments)
        except MCPClientError as exc:
            raise MCPRegistryError(str(exc)) from exc
