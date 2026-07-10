"""Shared YAML-backed registry config loader.

This is the human-owned configuration layer for builtin tool registry metadata.
Python modules still own schema builders, implementation callables, and other
runtime-heavy concerns, but they should read stable registry policy metadata from
this file instead of duplicating it inline.
"""

from __future__ import annotations

import yaml
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any


def agency_tool_registry_path() -> Path:
    """Return the canonical YAML registry path for builtin Agency tool metadata."""
    return Path(__file__).with_name("config") / "agency_tools.yaml"


@lru_cache(maxsize=1)
def _load_registry_config() -> dict[str, Any]:
    with agency_tool_registry_path().open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_agency_tool_registry_config() -> dict[str, Any]:
    """Return a defensive copy of the shared YAML registry config."""
    return deepcopy(_load_registry_config())


def load_system_tool_family_config() -> list[dict[str, Any]]:
    """Return ordered system-family registry metadata from the YAML config."""
    registry = load_agency_tool_registry_config()
    families = registry.get("system_families") or []
    return deepcopy(families)


def load_system_tool_spec_config() -> dict[str, list[dict[str, Any]]]:
    """Return declarative system-tool spec families from the YAML registry."""
    registry = load_agency_tool_registry_config()
    system_tools = registry.get("system_tools") or {}
    return deepcopy(system_tools if isinstance(system_tools, dict) else {})


def load_system_runtime_tool_spec_config() -> dict[str, list[dict[str, Any]]]:
    """Return runtime-heavy system-tool spec families from the YAML registry."""
    registry = load_agency_tool_registry_config()
    system_runtime_tools = registry.get("system_runtime_tools") or {}
    return deepcopy(system_runtime_tools if isinstance(system_runtime_tools, dict) else {})
