from __future__ import annotations

import json
import os
import subprocess
from itertools import count
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.domain import MCPServerDefinition, MCPTransportType
from app.integrations.secrets import resolve_secret_ref
from .schemas import MCPDiscoverySnapshot, MCPPromptDescriptor, MCPResourceDescriptor, MCPToolDescriptor


class MCPClientError(RuntimeError):
    """Raised when an MCP client operation fails."""


class BaseMCPClient:
    def discover(self) -> MCPDiscoverySnapshot:
        raise NotImplementedError

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


DEFAULT_MCP_PROCESS_PATHS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)

SAFE_MCP_PROCESS_ENV_VARS = (
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)


def _split_paths(value: str | None) -> list[str]:
    if not value:
        return []
    return [item for item in value.split(os.pathsep) if item]


def _mcp_process_path() -> str:
    configured_paths = _split_paths(os.getenv("MCP_SERVER_EXTRA_PATHS") or get_settings().mcp_server_extra_paths)
    existing_paths = _split_paths(os.getenv("PATH"))
    paths = [*configured_paths, *DEFAULT_MCP_PROCESS_PATHS, *existing_paths]
    return os.pathsep.join(dict.fromkeys(paths))


def _env_name_for_ref(secret_ref: Any) -> str | None:
    if secret_ref.key and secret_ref.key.strip():
        return secret_ref.key.strip()
    if secret_ref.ref.startswith("env://"):
        return secret_ref.ref[len("env://"):].strip() or None
    if secret_ref.ref.startswith("env:"):
        return secret_ref.ref[len("env:"):].strip() or None
    return None


def _source_env_name(secret_ref: Any) -> str | None:
    if secret_ref.ref.startswith("env://"):
        return secret_ref.ref[len("env://"):].strip() or None
    if secret_ref.ref.startswith("env:"):
        return secret_ref.ref[len("env:"):].strip() or None
    return None


def _settings_env_fallback(env_name: str) -> str | None:
    mapping = {
        "FIRECRAWL_API_KEY": "firecrawl_api_key",
        "CONTEXT7_API_KEY": "context7_api_key",
    }
    setting_name = mapping.get(env_name)
    if setting_name is None:
        return None
    value = getattr(get_settings(), setting_name, None)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def build_mcp_process_environment(definition: MCPServerDefinition) -> dict[str, str]:
    env = {
        key: value
        for key in SAFE_MCP_PROCESS_ENV_VARS
        if (value := os.getenv(key)) is not None
    }
    env["PATH"] = _mcp_process_path()
    for secret_ref in definition.env_refs:
        env_name = _env_name_for_ref(secret_ref)
        if not env_name:
            raise MCPClientError(
                f"MCP server '{definition.id}' has env ref '{secret_ref.ref}' without a target env var key"
            )
        settings = get_settings()
        if settings.app_env != "test" and env_name not in settings.parsed_mcp_server_allowed_env_vars:
            raise MCPClientError(
                f"MCP server '{definition.id}' env var '{env_name}' is not allowed by MCP_SERVER_ALLOWED_ENV_VARS"
            )
        source_env_name = _source_env_name(secret_ref)
        if settings.app_env != "test" and source_env_name is not None:
            if source_env_name not in settings.parsed_mcp_server_allowed_env_vars:
                raise MCPClientError(
                    f"MCP server '{definition.id}' source env var is not allowed by MCP_SERVER_ALLOWED_ENV_VARS"
                )
            if source_env_name != env_name:
                # Prevent an allowed child key from becoming a laundering target
                # for a different parent-process secret.
                raise MCPClientError(
                    f"MCP server '{definition.id}' env ref source must match its target env var"
                )
        resolved = resolve_secret_ref(secret_ref.ref)
        if resolved.error:
            fallback_value = _settings_env_fallback(env_name)
            if fallback_value is None:
                raise MCPClientError(
                    f"MCP server '{definition.id}' could not resolve env ref for '{env_name}': {resolved.error}"
                )
            env[env_name] = fallback_value
            continue
        env[env_name] = resolved.value or ""
    return env


def resolve_mcp_command(command: str, env: dict[str, str]) -> str:
    if os.path.sep in command:
        return command
    for directory in _split_paths(env.get("PATH")):
        candidate = Path(directory) / command
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return command


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
        env = build_mcp_process_environment(self.definition)
        command = resolve_mcp_command(self.definition.command, env)
        process = subprocess.Popen(  # noqa: S603
            [command, *self.definition.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
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
