"""Routing catalogue helpers built on canonical ToolDefinition records."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Iterable

from app.domain import ToolDefinition
from app.domain.intent_routing import ToolGroupDescriptor, ToolRoutingMetadata


_FAMILY_DESCRIPTIONS = {
    "agent": "Inspect or update agent definitions",
    "command": "Run guarded commands in an approved execution environment",
    "memory": "Search, read, or manage stored memories",
    "tool": "Inspect or update registered tool definitions",
    "workflow": "Inspect, run, or update workflows",
}


def routing_metadata_for_tool(tool: ToolDefinition) -> ToolRoutingMetadata:
    """Return explicit metadata when present, otherwise a safe canonical-ID fallback.

    Existing persisted tools predate routing metadata. The fallback keeps them routable
    without widening their capability: it derives the group only from a stable Agency ID
    and the already-enforced security settings.
    """
    if tool.routing is not None:
        actual_read_only = tool.security.read_only and tool.routing.read_only
        actual_requires_confirmation = tool.security.requires_approval or tool.routing.requires_confirmation
        actual_risk = (
            tool.routing.risk_level
            if actual_read_only
            else ("confirmation" if actual_requires_confirmation else "write")
        )
        # Persisted routing hints may classify capabilities, but cannot downgrade canonical security metadata.
        return tool.routing.model_copy(
            update={
                "read_only": actual_read_only,
                "risk_level": actual_risk,
                "requires_confirmation": actual_requires_confirmation,
            }
        )

    parts = tool.id.split(".")
    family = parts[1] if len(parts) > 1 and parts[0] == "agency" else "other"
    access = "read" if tool.security.read_only else "write"
    group = "code.execution" if family == "command" else f"{family}.{access}"
    risk = "read" if tool.security.read_only else ("confirmation" if tool.security.requires_approval else "write")
    return ToolRoutingMetadata(
        group=group,
        short_description=_FAMILY_DESCRIPTIONS.get(family, "Use an Agency capability"),
        intents=[family],
        keywords=[family],
        read_only=tool.security.read_only,
        risk_level=risk,
        requires_confirmation=tool.security.requires_approval,
    )


def compact_tool_groups(tools: Iterable[ToolDefinition]) -> list[ToolGroupDescriptor]:
    """Create stable compact descriptors without leaking provider function schemas."""
    by_group: dict[str, ToolRoutingMetadata] = {}
    for tool in sorted(tools, key=lambda item: item.id):
        metadata = routing_metadata_for_tool(tool)
        if metadata.enabled:
            for group_id in _routing_group_ids(metadata):
                by_group.setdefault(group_id, metadata)
    return [
        ToolGroupDescriptor(
            id=group_id,
            description=metadata.short_description,
            risk=metadata.risk_level,
        )
        for group_id, metadata in sorted(by_group.items())
    ]


def resolve_tool_groups(tools: Iterable[ToolDefinition], group_ids: Iterable[str]) -> list[ToolDefinition]:
    """Resolve approved groups with deterministic order and no duplicate tools."""
    requested = set(group_ids)
    resolved: list[ToolDefinition] = []
    for tool in sorted(tools, key=lambda item: item.id):
        metadata = routing_metadata_for_tool(tool)
        if metadata.enabled and requested.intersection(_routing_group_ids(metadata)):
            resolved.append(tool)
    return resolved


def tool_catalogue_version(tools: Iterable[ToolDefinition]) -> str:
    """Hash compact routing facts so cache keys invalidate when exposure changes."""
    entries = []
    for tool in sorted(tools, key=lambda item: item.id):
        metadata = routing_metadata_for_tool(tool)
        entries.append(
            {
                "id": tool.id,
                "groups": _routing_group_ids(metadata),
                "enabled": metadata.enabled,
                "read_only": metadata.read_only,
                "risk": metadata.risk_level,
                "requires_confirmation": metadata.requires_confirmation,
            }
        )
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _routing_group_ids(metadata: ToolRoutingMetadata) -> list[str]:
    return list(dict.fromkeys([metadata.group, *metadata.additional_groups]))
