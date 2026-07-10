"""Convert discovered MCP tool descriptors into Agency tool definitions."""

from __future__ import annotations

import re

from app.domain import FrameworkHints, MCPExposureSettings, SecuritySettings, ToolImplementationReference
from app.domain import MCPServerDefinition, ToolDefinition, ToolType
from .computer_use_adapter import canonical_computer_use_schema
from .schemas import MCPToolDescriptor

COMPUTER_USE_TOOL_NAME_MAP = {
    "click": "click",
    "type": "type",
    "scroll": "scroll",
    "move": "move",
    "shortcut": "press_key",
    "wait": "wait",
    "snapshot": "snapshot",
    "screenshot": "screenshot",
    "app": "app",
    "shell": "shell",
    "scrape": "scrape",
    "multiselect": "multi_select",
    "multiedit": "multi_edit",
    "clipboard": "clipboard",
    "process": "process",
    "notification": "notification",
    "registry": "registry",
}


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "tool"


def _normalized_tool_name(server: MCPServerDefinition, tool: MCPToolDescriptor) -> str:
    if server.metadata.get("family") == "computer_use":
        return COMPUTER_USE_TOOL_NAME_MAP.get(tool.name.lower(), _slugify(tool.name))
    return tool.name


def _normalized_tool_description(server: MCPServerDefinition, tool: MCPToolDescriptor, canonical_name: str) -> str:
    base = tool.description or f"MCP tool from {server.name}"
    if server.metadata.get("family") == "computer_use" and canonical_name != tool.name:
        return f"{base} Normalized as Agency computer-use tool '{canonical_name}'."
    return base


def _is_high_risk(tool: MCPToolDescriptor) -> bool:
    annotations = tool.annotations or {}
    metadata = tool.metadata or {}
    name = tool.name.lower()
    description = tool.description.lower()
    risky_words = ("shell", "browser", "filesystem", "network", "write", "delete", "modify")
    if metadata.get("risk_level") == "high":
        return True
    if annotations.get("destructiveHint") is True or annotations.get("readOnlyHint") is False:
        return True
    return any(word in name or word in description for word in risky_words)


def mcp_tool_to_definition(server: MCPServerDefinition, tool: MCPToolDescriptor) -> ToolDefinition:
    from app.tools.names import make_tool_display_name

    requires_approval = _is_high_risk(tool)
    canonical_name = _normalized_tool_name(server, tool)
    remote_input_schema = tool.input_schema or {"type": "object"}
    input_schema = (
        canonical_computer_use_schema(canonical_name, remote_input_schema)
        if server.metadata.get("family") == "computer_use"
        else remote_input_schema
    )
    return ToolDefinition(
        id=f"mcp:{server.id}:{canonical_name}",
        name=canonical_name,
        display_name=make_tool_display_name(canonical_name),
        description=_normalized_tool_description(server, tool, canonical_name),
        tool_type=ToolType.MCP_TOOL,
        input_schema=input_schema,
        output_schema={"type": "object"},
        implementation=ToolImplementationReference(
            implementation_type="mcp_tool",
            target=server.id,
            callable_name=tool.name,
            config={
                "mcp_tool_name": tool.name,
                "canonical_tool_name": canonical_name,
                "tool_family": server.metadata.get("family"),
                "tool_platform": server.metadata.get("platform"),
                "remote_input_schema": remote_input_schema,
            },
        ),
        security=SecuritySettings(
            requires_approval=requires_approval,
            allowlisted_mcp_servers=[server.id],
            redaction_enabled=requires_approval,
            redaction_rules=["authorization", "token", "secret", "password"],
        ),
        mcp_exposure=MCPExposureSettings(expose_as_mcp_tool=False),
        tags=[
            "mcp",
            server.id,
            *(["computer_use"] if server.metadata.get("family") == "computer_use" else []),
            *([str(server.metadata.get("platform"))] if server.metadata.get("platform") else []),
        ],
        framework_hints=FrameworkHints(
            preferred_adapter="native",
            metadata={
                "mcp_server_id": server.id,
                "canonical_tool_name": canonical_name,
                "remote_tool_name": tool.name,
            },
        ),
    )
