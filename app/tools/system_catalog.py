"""Structured registry for Agency system-tool families.

System tools still declare their concrete ToolDefinition objects in Python, but
the family-level policy metadata now lives in `config/agency_tools.yaml`.
That keeps the registry human-readable while preserving code-owned builders for
runtime-heavy schemas and execution wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.core.config import get_settings
from app.domain import ToolDefinition
from app.modules.registry import optional_module_system_tool_family_builders
from app.services.agent_tools import (
    agent_management_system_tool_definitions,
    agent_management_system_tool_ids,
    command_system_tool_definitions,
    command_system_tool_ids,
    connector_system_tool_definitions,
    connector_system_tool_ids,
    execution_system_tool_definitions,
    execution_system_tool_ids,
    goal_system_tool_definitions,
    goal_system_tool_ids,
    graph_system_tool_definitions,
    graph_system_tool_ids,
    memory_system_tool_definitions,
    memory_system_tool_ids,
    tool_management_system_tool_definitions,
    tool_management_system_tool_ids,
    workflow_system_tool_definitions,
    workflow_system_tool_ids,
)
from app.tools.registry_config import load_system_tool_family_config

SystemToolDefinitionBuilder = Callable[[bool], list[ToolDefinition]]
SystemToolIdBuilder = Callable[[bool], list[str]]


@dataclass(frozen=True)
class SystemToolFamilySpec:
    key: str
    policy_flag: str
    default_enabled: bool
    default_enabled_setting: str | None = None
    include_in_builtin_registry: bool = True
    definition_builder: SystemToolDefinitionBuilder | None = None
    id_builder: SystemToolIdBuilder | None = None

    def is_default_enabled(self) -> bool:
        if self.default_enabled_setting == "agency_graph_context_tools_enabled":
            # Resolve feature flags at assembly time so tests and local setup can flip env after import.
            return bool(get_settings().agency_graph_context_tools_enabled)
        return self.default_enabled


BUILTIN_SYSTEM_TOOL_FAMILY_BUILDERS: dict[str, tuple[SystemToolDefinitionBuilder, SystemToolIdBuilder]] = {
    "workflow": (
        lambda enabled: workflow_system_tool_definitions(can_trigger_workflows=enabled),
        lambda enabled: workflow_system_tool_ids(can_trigger_workflows=enabled),
    ),
    "goal": (
        lambda enabled: goal_system_tool_definitions(can_manage_goals=enabled),
        lambda enabled: goal_system_tool_ids(can_manage_goals=enabled),
    ),
    "tool_management": (
        lambda enabled: tool_management_system_tool_definitions(can_manage_tools=enabled),
        lambda enabled: tool_management_system_tool_ids(can_manage_tools=enabled),
    ),
    "agent_management": (
        lambda enabled: agent_management_system_tool_definitions(can_manage_agents=enabled),
        lambda enabled: agent_management_system_tool_ids(can_manage_agents=enabled),
    ),
    "connector": (
        lambda enabled: connector_system_tool_definitions(can_manage_integrations=enabled),
        lambda enabled: connector_system_tool_ids(can_manage_integrations=enabled),
    ),
    "memory": (
        lambda enabled: memory_system_tool_definitions(can_manage_memory=enabled),
        lambda enabled: memory_system_tool_ids(can_manage_memory=enabled),
    ),
    "graph": (
        lambda enabled: graph_system_tool_definitions(can_read_graph_context=enabled),
        lambda enabled: graph_system_tool_ids(can_read_graph_context=enabled),
    ),
    "execution": (
        lambda enabled: execution_system_tool_definitions(can_inspect_executions=enabled),
        lambda enabled: execution_system_tool_ids(can_inspect_executions=enabled),
    ),
    "command": (
        lambda enabled: command_system_tool_definitions(can_run_commands=enabled),
        lambda enabled: command_system_tool_ids(can_run_commands=enabled),
    ),
}


def _system_tool_family_builders() -> dict[str, tuple[SystemToolDefinitionBuilder, SystemToolIdBuilder]]:
    """Return builtin plus currently available optional module system-tool builders."""

    return {
        **BUILTIN_SYSTEM_TOOL_FAMILY_BUILDERS,
        **optional_module_system_tool_family_builders(),
    }


def _default_enabled_from_config(config: dict[str, object]) -> bool:
    setting_name = config.get("default_enabled_setting")
    if setting_name == "agency_graph_context_tools_enabled":
        # Graph context remains opt-in at runtime even though the family itself is listed in YAML.
        return bool(get_settings().agency_graph_context_tools_enabled)
    return bool(config.get("default_enabled", False))


def _build_system_tool_families() -> tuple[SystemToolFamilySpec, ...]:
    families: list[SystemToolFamilySpec] = []
    builders_by_key = _system_tool_family_builders()
    configured_keys: set[str] = set()
    for config in load_system_tool_family_config():
        if not isinstance(config, dict):
            continue
        key = str(config.get("key") or "").strip()
        builders = builders_by_key.get(key)
        if not builders:
            continue
        configured_keys.add(key)
        definition_builder, id_builder = builders
        families.append(
            SystemToolFamilySpec(
                key=key,
                policy_flag=str(config.get("policy_flag") or key),
                default_enabled=_default_enabled_from_config(config),
                default_enabled_setting=str(config.get("default_enabled_setting") or "") or None,
                include_in_builtin_registry=bool(config.get("include_in_builtin_registry", True)),
                definition_builder=definition_builder,
                id_builder=id_builder,
            )
        )
    for key, builders in builders_by_key.items():
        if key in configured_keys or key in BUILTIN_SYSTEM_TOOL_FAMILY_BUILDERS:
            continue
        definition_builder, id_builder = builders
        # Optional packs can contribute system-tool families without adding core
        # YAML entries. The module key doubles as the default policy flag.
        families.append(
            SystemToolFamilySpec(
                key=key,
                policy_flag=key,
                default_enabled=True,
                include_in_builtin_registry=True,
                definition_builder=definition_builder,
                id_builder=id_builder,
            )
        )
    return tuple(families)


def _enabled_for_family(spec: SystemToolFamilySpec, policy: dict[str, bool] | None = None) -> bool:
    if policy is None:
        return spec.is_default_enabled()
    return bool(policy.get(spec.policy_flag, spec.is_default_enabled()))


def system_tool_family_specs(*, include_connectors: bool = False) -> list[SystemToolFamilySpec]:
    """Return ordered family specs used to assemble Agency system tools."""
    families: list[SystemToolFamilySpec] = []
    for spec in _build_system_tool_families():
        if not include_connectors and not spec.include_in_builtin_registry:
            continue
        families.append(spec)
    return families


def builtin_system_tool_definitions_from_catalog(
        *,
        include_connectors: bool = False,
        policy: dict[str, bool] | None = None,
) -> list[ToolDefinition]:
    """Assemble system-tool definitions from the shared family catalog."""
    tools: list[ToolDefinition] = []
    for spec in system_tool_family_specs(include_connectors=include_connectors):
        if not _enabled_for_family(spec, policy):
            continue
        if spec.definition_builder is None:
            continue
        tools.extend(spec.definition_builder(True))
    return tools


def builtin_system_tool_ids_from_catalog(
        *,
        include_connectors: bool = False,
        policy: dict[str, bool] | None = None,
) -> list[str]:
    """Assemble system-tool ids from the shared family catalog."""
    tool_ids: list[str] = []
    for spec in system_tool_family_specs(include_connectors=include_connectors):
        if not _enabled_for_family(spec, policy):
            continue
        if spec.id_builder is None:
            continue
        tool_ids.extend(spec.id_builder(True))
    return tool_ids
