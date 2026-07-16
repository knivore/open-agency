"""Registry for configured MCP servers and their discovered capabilities."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from app.domain import MCPServerDefinition
from app.core.config import get_settings
from .client import HttpMCPClient, MCPClientError, StdioMCPClient
from .prompt_adapter import mcp_prompt_to_dict
from .resource_adapter import mcp_resource_to_dict
from .schemas import MCPDiscoverySnapshot
from .tool_adapter import mcp_tool_to_definition


class MCPRegistryError(RuntimeError):
    """Raised when MCP registry operations fail."""


_NPM_PACKAGE_NAME_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)
_NPM_EXACT_VERSION_RE = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_PYTHON_PACKAGE_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?$"
)


def _has_exact_package_pin(command_name: str, args: list[str]) -> bool:
    """Accept only immutable package specs for package-manager-backed MCP servers."""
    normalized_args = [str(arg).strip() for arg in args if str(arg).strip()]
    # Package-selection flags can introduce a second mutable dependency even
    # when the positional package is pinned, so reject those invocation shapes.
    if command_name == "npx" and any(
        arg in {"-p", "--package", "-c", "--call"} or arg.startswith("--package=")
        for arg in normalized_args
    ):
        return False
    if command_name == "uvx" and any(
        arg in {"--from", "--with", "--with-editable", "-e", "--editable", "-r", "--requirements"}
        or arg.startswith(
            ("--from=", "--with=", "--with-editable=", "--editable=", "--requirements=")
        )
        for arg in normalized_args
    ):
        return False

    package_spec = next((arg for arg in normalized_args if not arg.startswith("-")), None)
    if not package_spec:
        return False
    if command_name == "npx":
        separator = package_spec.rfind("@")
        if separator <= 0:
            return False
        package_name = package_spec[:separator]
        version = package_spec[separator + 1:]
        return bool(_NPM_PACKAGE_NAME_RE.fullmatch(package_name) and _NPM_EXACT_VERSION_RE.fullmatch(version))

    package_name, separator, version = package_spec.partition("==")
    if not separator or not _PYTHON_PACKAGE_NAME_RE.fullmatch(package_name):
        return False
    try:
        # PEP 440 parsing rejects tags, wildcards, URLs, paths, VCS refs, and
        # range syntax while preserving exact prerelease/local-version pins.
        Version(version)
    except InvalidVersion:
        return False
    return True


class MCPClientRegistry:
    """Register MCP server definitions, discover catalogs, and call remote tools."""

    def __init__(self):
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
        command_name = Path(definition.command).name
        settings = get_settings()
        allowed_commands = settings.parsed_mcp_server_allowed_commands
        # Test fixtures use the active Python interpreter as a deterministic MCP
        # server; production still requires an explicit operator allowlist.
        if settings.app_env != "test" and command_name not in allowed_commands:
            raise MCPRegistryError(
                f"MCP server '{definition.id}' command '{command_name}' is not allowed by MCP_SERVER_ALLOWED_COMMANDS"
            )
        if settings.app_env != "test" and command_name in {"npx", "uvx"}:
            if not _has_exact_package_pin(command_name, definition.args):
                # Package runners otherwise resolve mutable registry state at
                # launch time, turning catalog discovery into a supply-chain RCE.
                raise MCPRegistryError(
                    f"MCP server '{definition.id}' must use one exact package version in args before "
                    f"{command_name} may run"
                )
        if definition.allowlisted_command:
            if definition.allowlisted_command != command_name:
                raise MCPRegistryError(
                    f"MCP server '{definition.id}' allowlisted command "
                    f"'{definition.allowlisted_command}' does not match command '{command_name}'"
                )
            return
        raise MCPRegistryError(f"MCP server '{definition.id}' does not declare an allowed command")

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
