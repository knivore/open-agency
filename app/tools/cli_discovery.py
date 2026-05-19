from __future__ import annotations

from difflib import get_close_matches
from typing import Any

from app.domain import ToolDefinition
from app.services.agent_tools import (
    command_system_tool_definitions,
    execution_system_tool_definitions,
    memory_system_tool_definitions,
    tool_management_system_tool_definitions,
    workflow_system_tool_definitions,
)
from app.tools.definitions import get_tool_catalog_definitions
from app.tools.names import make_tool_call_name, tool_call_name, tool_display_name


GENERIC_TAGS = {"agent_assignable", "catalog", "crewai", "system"}


def list_builtin_tool_definitions() -> list[ToolDefinition]:
    tools = [
        *get_tool_catalog_definitions(),
        *workflow_system_tool_definitions(can_trigger_workflows=True),
        *tool_management_system_tool_definitions(can_manage_tools=True),
        *memory_system_tool_definitions(can_manage_memory=True),
        *execution_system_tool_definitions(can_inspect_executions=True),
        *command_system_tool_definitions(can_run_commands=True),
    ]
    seen: set[str] = set()
    unique_tools: list[ToolDefinition] = []
    for tool in tools:
        if tool.id in seen:
            continue
        seen.add(tool.id)
        unique_tools.append(tool)
    return sorted(unique_tools, key=lambda tool: tool.id)


def command_alias_for_tool(tool: ToolDefinition) -> str:
    raw = tool.id.removeprefix("agency.")
    parts = raw.rsplit(".", 1)
    if len(parts) == 1:
        return raw.replace(".", " ")
    return f"{parts[0].replace('.', ' ')} {parts[1]}"


def category_for_tool(tool: ToolDefinition) -> str:
    for tag in tool.tags:
        if tag not in GENERIC_TAGS:
            return tag
    parts = tool.id.removeprefix("agency.").split(".")
    return parts[0] if parts else "uncategorized"


def side_effects_for_tool(tool: ToolDefinition) -> list[str]:
    effects: list[str] = []
    security = tool.security
    if security.read_only:
        effects.append("read-only")
    if security.allow_network:
        effects.append("network")
    if security.allow_browser:
        effects.append("browser-state")
    if security.allow_filesystem:
        effects.append("filesystem")
    if security.allow_shell:
        effects.append("shell")
    if security.requires_approval:
        effects.append("approval-required")
    if security.sandbox_required:
        effects.append("sandbox-required")
    if security.dangerous:
        effects.append("high-impact")
    return effects or ["none-declared"]


def summarize_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "id": tool.id,
        "name": tool.name,
        "display_name": tool_display_name(tool),
        "command_alias": command_alias_for_tool(tool),
        "category": category_for_tool(tool),
        "description": tool.description,
        "side_effects": side_effects_for_tool(tool),
    }


def describe_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        **summarize_tool(tool),
        "tool_type": tool.tool_type.value,
        "input_schema": tool.input_schema,
        "output_schema": tool.output_schema,
        "security": tool.security.model_dump(mode="json"),
        "implementation": tool.implementation.model_dump(mode="json"),
        "tags": tool.tags,
    }


def schema_for_tool(tool: ToolDefinition, *, which: str = "both") -> dict[str, Any]:
    if which == "input":
        return {"id": tool.id, "input_schema": tool.input_schema}
    if which == "output":
        return {"id": tool.id, "output_schema": tool.output_schema}
    return {
        "id": tool.id,
        "input_schema": tool.input_schema,
        "output_schema": tool.output_schema,
    }


def resolve_tool(identifier: str, tools: list[ToolDefinition] | None = None) -> ToolDefinition | None:
    candidates = tools or list_builtin_tool_definitions()
    normalized = identifier.strip().lower()
    for tool in candidates:
        aliases = {
            tool.id.lower(),
            tool.name.lower(),
            tool_display_name(tool).lower(),
            make_tool_call_name(tool_display_name(tool)).lower(),
            tool_call_name(tool).lower(),
            command_alias_for_tool(tool).lower(),
        }
        if normalized in aliases:
            return tool
    return None


def suggest_tool_ids(identifier: str, tools: list[ToolDefinition] | None = None, *, limit: int = 5) -> list[str]:
    candidates = tools or list_builtin_tool_definitions()
    lookup: dict[str, str] = {}
    for tool in candidates:
        lookup[tool.id] = tool.id
        lookup[tool.name] = tool.id
        lookup[tool_display_name(tool)] = tool.id
        lookup[make_tool_call_name(tool_display_name(tool))] = tool.id
        lookup[tool_call_name(tool)] = tool.id
        lookup[command_alias_for_tool(tool)] = tool.id
    matches = get_close_matches(identifier, list(lookup), n=limit, cutoff=0.3)
    suggestions: list[str] = []
    for match in matches:
        tool_id = lookup[match]
        if tool_id not in suggestions:
            suggestions.append(tool_id)
    return suggestions
