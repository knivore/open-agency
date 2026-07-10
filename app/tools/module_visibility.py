"""Module-availability filtering for tool discovery surfaces.

Backend modules can be intentionally disabled while older persisted tool rows or
contract registries still exist. Keep this filtering close to tool discovery so
agents and clients do not mistake disabled-module tools for usable affordances.
"""

from __future__ import annotations

from typing import Iterable, TypeVar

from app.domain import ToolDefinition
from app.modules.registry import hidden_tool_names_for_disabled_modules

T = TypeVar("T")


def tool_name_hidden_by_disabled_modules(tool_name: str) -> bool:
    return tool_name in hidden_tool_names_for_disabled_modules()


def tool_definition_visible_with_enabled_modules(tool: ToolDefinition) -> bool:
    return not tool_name_hidden_by_disabled_modules(tool.id)


def filter_visible_tool_definitions(tools: Iterable[ToolDefinition]) -> list[ToolDefinition]:
    return [tool for tool in tools if tool_definition_visible_with_enabled_modules(tool)]


def filter_visible_items_by_tool_name(items: Iterable[T], name_getter) -> list[T]:
    return [item for item in items if not tool_name_hidden_by_disabled_modules(str(name_getter(item)))]
