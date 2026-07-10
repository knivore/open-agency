"""Canonical builtin tool assembly helpers.

This module is the one place that composes the builtin Agency tool surface from
the YAML-owned registry metadata and the Python-owned system/runtime builders.
Callers should use these helpers instead of rebuilding the builtin list ad hoc.
"""

from __future__ import annotations

from app.domain import ToolDefinition
from app.tools.definitions import get_tool_catalog_definitions
from app.tools.module_visibility import filter_visible_tool_definitions
from app.tools.system_catalog import builtin_system_tool_definitions_from_catalog


def builtin_catalog_tool_definitions() -> list[ToolDefinition]:
    """Return app-owned builtin tools declared through the catalog/implementation layer."""
    return get_tool_catalog_definitions()


def builtin_system_tool_definitions(*, include_connectors: bool = False) -> list[ToolDefinition]:
    """Return Agency-authored control-plane tools that depend on backend runtime services.

    These tools are kept separate from app-owned implementation tools because they route into
    workflow, memory, graph, execution, and other backend services instead of direct Python tool
    callables. The shared system-tool catalog owns family composition and gating so callers do not
    have to repeat that assembly logic in multiple modules.
    """
    return builtin_system_tool_definitions_from_catalog(include_connectors=include_connectors)


def builtin_tool_definitions(*, include_connectors: bool = False) -> list[ToolDefinition]:
    """Return the canonical builtin tool surface exposed by this backend.

    Keep this as the single composition point for builtin tools so CLI discovery, seed data,
    runtime inspection, and future docs/tests can all reason about one assembled registry.
    The underlying metadata is YAML-owned, while the final ToolDefinition assembly stays in Python.
    """

    tools = filter_visible_tool_definitions([
        *builtin_catalog_tool_definitions(),
        *builtin_system_tool_definitions(include_connectors=include_connectors),
    ])
    seen: set[str] = set()
    unique_tools: list[ToolDefinition] = []
    for tool in tools:
        if tool.id in seen:
            continue
        seen.add(tool.id)
        unique_tools.append(tool)
    return sorted(unique_tools, key=lambda tool: tool.id)
