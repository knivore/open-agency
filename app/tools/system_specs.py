"""Declarative specs for simpler Agency system-tool families.

The canonical human-owned spec data lives in `config/agency_tools.yaml`. This
module remains as the import surface used by the runtime so callers can keep
working with named family constants without caring where the metadata is stored.
"""

from __future__ import annotations

from typing import Any

from app.tools.registry_config import load_system_tool_spec_config


def _system_tool_specs_for(family: str) -> list[dict[str, Any]]:
    specs = load_system_tool_spec_config()
    family_specs = specs.get(family) or []
    return list(family_specs if isinstance(family_specs, list) else [])


WORKFLOW_SYSTEM_TOOL_SPECS: list[dict[str, Any]] = _system_tool_specs_for("workflow")
GOAL_SYSTEM_TOOL_SPECS: list[dict[str, Any]] = _system_tool_specs_for("goal")
CONNECTOR_SYSTEM_TOOL_SPECS: list[dict[str, Any]] = _system_tool_specs_for("connector")
TOOL_MANAGEMENT_SYSTEM_TOOL_SPECS: list[dict[str, Any]] = _system_tool_specs_for("tool_management")
AGENT_MANAGEMENT_SYSTEM_TOOL_SPECS: list[dict[str, Any]] = _system_tool_specs_for("agent_management")
COMMAND_SYSTEM_TOOL_SPECS: list[dict[str, Any]] = _system_tool_specs_for("command")
EXECUTION_SYSTEM_TOOL_SPECS: list[dict[str, Any]] = _system_tool_specs_for("execution")
