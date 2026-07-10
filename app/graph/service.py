"""Shared graph read service helpers for API routes."""

from __future__ import annotations

import json
from typing import Any

from app.core.config import get_settings
from app.graph.neo4j_projection import create_neo4j_driver
from app.graph.neo4j_read import GraphReadConfig, Neo4jGraphReader

GRAPH_NEIGHBORHOOD_PRESETS = {
    "workflow": {
        "labels": ["Workflow"],
        "relationship_types": [
            "HAS_RUN",
            "HAS_STEP_RUN",
            "DEFINES_AGENT",
            "DEFINES_TASK",
            "DEFINES_TOOL",
            "HAS_MEMORY_LINK",
            "LINKS_MEMORY",
            "HAS_CHUNK",
            "PART_OF_DOCUMENT",
            "CAPTURES_DECISION",
            "CONSTRAINED_BY",
            "SUGGESTS_NEXT_ACTION",
            "PRODUCED_ARTIFACT",
            "FAILED_WITH",
        ],
    },
    "workflow_run": {
        "labels": ["WorkflowRun"],
        "relationship_types": [
            "HAS_RUN",
            "RUN_OF",
            "HAS_STEP_RUN",
            "SOURCE_EXECUTION",
            "LINKS_MEMORY",
            "HAS_CHUNK",
            "PART_OF_DOCUMENT",
            "HAS_CONTEXT_HEALTH",
            "RECORDED_CONTEXT_HEALTH",
            "RECORDED_USAGE",
            "HAS_BUDGET_SIGNAL",
            "HAS_COMPACTION",
            "RAISED_FINDING",
            "FAILED_WITH",
            "EMITTED_EVENT",
            "CALLED_TOOL",
            "PRODUCED_ARTIFACT",
            "CAPTURES_DECISION",
            "CONSTRAINED_BY",
            "SUGGESTS_NEXT_ACTION",
        ],
    },
    "agent": {
        "labels": ["Agent"],
        "relationship_types": [
            "DEFINES_AGENT",
            "PERFORMED_BY",
            "ASSIGNED_TO",
            "CAN_USE",
            "CAN_HANDOFF_TO",
            "USED_MODEL",
            "USES_MODEL_PROFILE",
            "HAS_STEP_RUN",
        ],
    },
    "tool": {
        "labels": ["Tool"],
        "relationship_types": [
            "DEFINES_TOOL",
            "CAN_USE",
            "USES_TOOL",
            "CALLED_TOOL",
            "INVOKED_IN_STEP",
            "HAS_STEP_RUN",
        ],
    },
    "memory": {
        "labels": ["Memory"],
        "relationship_types": [
            "LINKS_MEMORY",
            "HAS_MEMORY_LINK",
            "AVAILABLE_TO",
            "HAS_CHUNK",
            "PART_OF_DOCUMENT",
            "SOURCE_DOCUMENT",
            "HAS_CONTEXT_PACK",
            "SUMMARIZES",
            "SUPERSEDES",
            "MENTIONS",
            "SOURCE_EXECUTION",
            "SOURCE_CONVERSATION",
        ],
    },
    "entity": {
        "labels": ["Entity"],
        "relationship_types": [
            "MENTIONS",
            "PART_OF_DOCUMENT",
            "HAS_CHUNK",
            "LINKS_MEMORY",
            "HAS_MEMORY_LINK",
            "AVAILABLE_TO",
            "SOURCE_EXECUTION",
            "SOURCE_CONVERSATION",
            "SUPERSEDES",
        ],
    },
    "task": {
        "labels": ["Task", "StepRun"],
        "relationship_types": [
            "DEFINES_TASK",
            "HAS_STEP_RUN",
            "ASSIGNED_TO",
            "DEPENDS_ON",
            "USES_TOOL",
            "AVAILABLE_TO",
            "PERFORMED_BY",
            "LINKS_MEMORY",
            "HAS_MEMORY_LINK",
            "FAILED_WITH",
            "EMITTED_EVENT",
            "CALLED_TOOL",
            "PRODUCED_ARTIFACT",
            "CAPTURES_DECISION",
            "CONSTRAINED_BY",
            "SUGGESTS_NEXT_ACTION",
        ],
    },
    "persona": {
        "labels": ["Persona"],
        "relationship_types": [
            "PERSONA_HAS_DISTILLATION_RUN",
            "RUN_EXTRACTED_ITEM",
            "ITEM_DERIVED_FROM_MEMORY",
            "RUN_USED_SOURCE_MEMORY",
            "PERSONA_HAS_VERSION",
            "RUN_PRODUCED_VERSION",
            "PERSONA_PUBLISHED_MEMORY",
            "PERSONA_USES_TOOL",
            "PERSONA_FOLLOWS_WORKFLOW",
            "PERSONA_PRODUCES_ARTIFACT",
            "PERSONA_MATERIALIZED_AS_AGENT",
            "PERSONA_INVOKED_IN_CONVERSATION",
        ],
    },
    "physical_device": {
        "labels": ["Device", "Room", "Location", "Adapter", "DeviceEvent", "DeviceCommand", "Memory", "Entity"],
        "relationship_types": [
            "LOCATED_IN",
            "LOCATED_AT",
            "MANAGES_DEVICE",
            "TARGETS_DEVICE",
            "REQUESTED_DEVICE_COMMAND",
            "INFLUENCED_DEVICE_COMMAND",
            "EMITTED_DEVICE_EVENT",
            "PRODUCED_DEVICE_EVENT",
            "CORRELATES_WITH_COMMAND",
            "TRIGGERED_WORKFLOW",
            "STARTED_WORKFLOW_RUN",
        ],
    },
    "physical_audit": {
        "labels": ["Device", "Room", "Location", "Adapter", "DeviceEvent", "DeviceCommand", "Memory", "Entity",
                   "Workflow", "WorkflowRun"],
        "relationship_types": [
            "LOCATED_IN",
            "LOCATED_AT",
            "MANAGES_DEVICE",
            "TARGETS_DEVICE",
            "REQUESTED_DEVICE_COMMAND",
            "INFLUENCED_DEVICE_COMMAND",
            "EMITTED_DEVICE_EVENT",
            "PRODUCED_DEVICE_EVENT",
            "CORRELATES_WITH_COMMAND",
            "TRIGGERED_WORKFLOW",
            "STARTED_WORKFLOW_RUN",
            "HAS_RUN",
        ],
    },
}

GRAPH_NEIGHBORHOOD_MODES = {
    "operational": {
        "labels": [],
        "relationship_types": [
            "HAS_RUN",
            "HAS_STEP_RUN",
            "DEFINES_AGENT",
            "DEFINES_TASK",
            "DEFINES_TOOL",
            "ASSIGNED_TO",
            "USES_TOOL",
            "CAN_USE",
            "CAN_HANDOFF_TO",
            "PARTICIPATED_IN",
            "OCCURRED_IN",
            "CALLED_TOOL",
        ],
    },
    "knowledge": {
        "labels": [],
        "relationship_types": [
            "LINKS_MEMORY",
            "HAS_MEMORY_LINK",
            "HAS_CHUNK",
            "PART_OF_DOCUMENT",
            "SOURCE_DOCUMENT",
            "MENTIONS",
            "HAS_CONTEXT_PACK",
            "SUMMARIZES",
            "SUPERSEDES",
            "SOURCE_CONVERSATION",
            "AVAILABLE_TO",
            "CREATED_MEMORY",
            "HAS_CONTEXT_PACK",
            "SUMMARIZES",
        ],
    },
    "lineage": {
        "labels": [],
        "relationship_types": [
            "SOURCE_EXECUTION",
            "SOURCE_CONVERSATION",
            "HAS_RUN",
            "HAS_STEP_RUN",
            "EMITTED_EVENT",
            "TRIGGERED",
            "USED_RUNTIME",
            "CREATED_CONTAINER",
            "PRODUCED_ARTIFACT",
            "PART_OF_DOCUMENT",
            "HAS_CHUNK",
        ],
    },
    "health": {
        "labels": [],
        "relationship_types": [
            "FAILED_WITH",
            "EMITTED_EVENT",
            "HAS_CONTEXT_HEALTH",
            "RECORDED_CONTEXT_HEALTH",
            "HAS_BUDGET_SIGNAL",
            "HAS_COMPACTION",
            "RAISED_FINDING",
            "OCCURRED_IN",
            "CALLED_TOOL",
        ],
    },
    "cost": {
        "labels": [],
        "relationship_types": [
            "RECORDED_USAGE",
            "HAS_BUDGET_SIGNAL",
            "USED_MODEL",
            "USED_PROVIDER",
            "RECORDED_CONTEXT_HEALTH",
            "HAS_CONTEXT_HEALTH",
        ],
    },
    "security": {
        "labels": [],
        "relationship_types": [
            "OWNS_DOCUMENT",
            "AVAILABLE_TO",
            "ASSIGNED_TO",
            "CAN_USE",
            "USES_TOOL",
            "DEFINES_TOOL",
            "EMITTED_EVENT",
            "OCCURRED_IN",
        ],
    },
}

GRAPH_QUERY_PRESETS = {
    "recent_failures",
    "failed_run_root_cause",
    "workflow_lineage",
    "memory_provenance",
    "stale_context",
    "missing_embeddings",
    "high_cost_runs",
    "tool_failure_hotspots",
    "sub_agent_steering",
    "coding_agent_resume",
    "persona_lineage",
    "persona_capability_map",
    "physical_device_audit",
    "physical_room_context",
}

GRAPH_READ_MAX_OUTPUT_BYTES = 1_000_000


class GraphReadUnavailableError(RuntimeError):
    """Raised when graph projection reads are disabled or unavailable."""


def create_reader_from_settings():
    settings = get_settings()
    if not settings.neo4j_enabled:
        raise GraphReadUnavailableError("Neo4j graph read API is disabled or unavailable")
    driver = create_neo4j_driver(settings)
    return Neo4jGraphReader(driver, config=GraphReadConfig(database=settings.neo4j_database)), True


def resolve_graph_reader(context: Any):
    injected = getattr(context, "graph_read_service", None)
    if injected is not None:
        return injected, False
    return create_reader_from_settings()


async def close_graph_reader_if_needed(reader: Any, close_after: bool) -> None:
    if not close_after:
        return
    close = getattr(reader, "close", None)
    if close is not None:
        await close()


def graph_document_payload(
        document: Any,
        *,
        query_meta: dict | None = None,
        limit: int | None = None,
        max_edges: int | None = None,
        max_output_bytes: int = GRAPH_READ_MAX_OUTPUT_BYTES,
) -> dict:
    if query_meta:
        document.meta.update(query_meta)
    # Graph read routes serve the persisted Agency Graph projection, so callers
    # should not need to infer whether a payload came from Neo4j.
    document.meta.setdefault("projection_mode", "neo4j")
    document.meta.setdefault("projection_available", True)
    _apply_graph_provenance_meta(document)
    original_node_count = len(document.nodes)
    original_edge_count = len(document.edges)
    if max_edges is not None and len(document.edges) > max_edges:
        kept_edges = document.edges[:max_edges]
        referenced_node_ids = {edge.source for edge in kept_edges} | {edge.target for edge in kept_edges}
        document.edges = kept_edges
        document.nodes = [node for node in document.nodes if
                          node.id in referenced_node_ids or len(referenced_node_ids) < 2]
        document.meta["edge_truncated"] = True
    document.meta["node_count"] = len(document.nodes)
    document.meta["edge_count"] = len(document.edges)
    document.meta["total_node_count"] = original_node_count
    document.meta["total_edge_count"] = original_edge_count
    if limit is not None:
        document.meta["limit"] = limit
        document.meta["truncated"] = (
                len(document.nodes) >= limit
                or len(document.edges) >= limit
                or original_node_count > len(document.nodes)
                or original_edge_count > len(document.edges)
        )
    payload = document.to_dict()
    _set_output_size_meta(payload, max_output_bytes=max_output_bytes)
    if _payload_size(payload) > max_output_bytes:
        payload = _trim_payload_to_bytes(payload, max_output_bytes=max_output_bytes)
        _set_output_size_meta(payload, max_output_bytes=max_output_bytes)
    while _payload_size(payload) > max_output_bytes and (payload.get("edges") or payload.get("nodes")):
        payload = _trim_payload_to_bytes(payload, max_output_bytes=max_output_bytes)
        _set_output_size_meta(payload, max_output_bytes=max_output_bytes)
    return payload


def _apply_graph_provenance_meta(document: Any) -> None:
    projection_node_count = len(document.nodes)
    projection_edge_count = len(document.edges)
    operational_nodes = list(getattr(document, "operational_nodes", []) or [])
    operational_edges = list(getattr(document, "operational_edges", []) or [])
    operational_node_count = len(operational_nodes)
    operational_edge_count = len(operational_edges)

    data_sources: list[str] = []
    if document.meta.get("projection_available") is not False:
        data_sources.append("projection")
    if operational_node_count > 0 or operational_edge_count > 0:
        data_sources.append("operational_coverage")

    graph_source_kind = "projection"
    if data_sources == ["operational_coverage"]:
        graph_source_kind = "operational_coverage"
    elif len(data_sources) > 1:
        graph_source_kind = "projection_with_operational_coverage"

    # Make mixed projection/operational payloads explicit so callers do not
    # have to reverse-engineer provenance from optional sections.
    document.meta.update(
        {
            "data_sources": data_sources,
            "graph_source_kind": graph_source_kind,
            "operational_edge_count": operational_edge_count,
            "operational_node_count": operational_node_count,
            "projection_edge_count": projection_edge_count,
            "projection_node_count": projection_node_count,
        }
    )


def graph_neighbors_payload(
        document: Any,
        *,
        center_id: str,
        query_meta: dict | None = None,
        limit: int | None = None,
        max_edges: int | None = None,
        max_output_bytes: int = GRAPH_READ_MAX_OUTPUT_BYTES,
) -> dict:
    payload = graph_document_payload(
        document,
        query_meta=query_meta,
        limit=limit,
        max_edges=max_edges,
        max_output_bytes=max_output_bytes,
    )
    nodes = list(payload.get("nodes") or [])
    edges = list(payload.get("edges") or [])
    node_by_id = {node.get("id"): node for node in nodes}
    center = node_by_id.get(center_id)
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    group_node_ids: dict[tuple[str, str, str], set[str]] = {}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source == center_id and target == center_id:
            direction = "self"
            neighbor_id = target
        elif source == center_id:
            direction = "outgoing"
            neighbor_id = target
        elif target == center_id:
            direction = "incoming"
            neighbor_id = source
        else:
            continue
        neighbor = node_by_id.get(neighbor_id)
        neighbor_type = str((neighbor or {}).get("type") or "Unknown")
        relationship_type = str(edge.get("type") or "UNKNOWN")
        key = (relationship_type, direction, neighbor_type)
        group = grouped.setdefault(
            key,
            {
                "relationship_type": relationship_type,
                "direction": direction,
                "node_type": neighbor_type,
                "count": 0,
                "nodes": [],
                "edges": [],
            },
        )
        group_node_ids.setdefault(key, set())
        group["count"] += 1
        group["edges"].append(edge)
        if neighbor is not None and neighbor_id not in group_node_ids[key]:
            group["nodes"].append(neighbor)
            group_node_ids[key].add(neighbor_id)
    groups = sorted(
        grouped.values(),
        key=lambda item: (item["relationship_type"], item["direction"], item["node_type"]),
    )
    payload["center"] = center
    payload["groups"] = groups
    payload["meta"]["neighbor_group_count"] = len(groups)
    _set_output_size_meta(payload, max_output_bytes=max_output_bytes)
    return payload


def _trim_payload_to_bytes(payload: dict, *, max_output_bytes: int) -> dict:
    trimmed = {
        "nodes": list(payload.get("nodes") or []),
        "edges": list(payload.get("edges") or []),
        "meta": dict(payload.get("meta") or {}),
    }
    trimmed["meta"]["max_output_bytes"] = max_output_bytes
    while trimmed["edges"] and _payload_size(trimmed) > max_output_bytes:
        trimmed["edges"].pop()
        trimmed["meta"]["edge_truncated"] = True
    referenced_node_ids = {edge.get("source") for edge in trimmed["edges"]} | {edge.get("target") for edge in
                                                                               trimmed["edges"]}
    referenced_node_ids.discard(None)
    if referenced_node_ids:
        trimmed["nodes"] = [node for node in trimmed["nodes"] if node.get("id") in referenced_node_ids]
    while trimmed["nodes"] and _payload_size(trimmed) > max_output_bytes:
        trimmed["nodes"].pop()
        trimmed["meta"]["node_truncated"] = True
    trimmed["meta"]["node_count"] = len(trimmed["nodes"])
    trimmed["meta"]["edge_count"] = len(trimmed["edges"])
    trimmed["meta"]["truncated"] = True
    trimmed["meta"]["output_truncated"] = True
    return trimmed


def _payload_size(payload: dict) -> int:
    return len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))


def _set_output_size_meta(payload: dict, *, max_output_bytes: int) -> None:
    payload.setdefault("meta", {})
    payload["meta"]["max_output_bytes"] = max_output_bytes
    previous_size = -1
    while True:
        output_bytes = _payload_size(payload)
        payload["meta"]["output_bytes"] = output_bytes
        if output_bytes == previous_size:
            return
        previous_size = output_bytes


__all__ = [
    "GRAPH_NEIGHBORHOOD_MODES",
    "GRAPH_NEIGHBORHOOD_PRESETS",
    "GRAPH_READ_MAX_OUTPUT_BYTES",
    "GRAPH_QUERY_PRESETS",
    "GraphReadUnavailableError",
    "close_graph_reader_if_needed",
    "create_reader_from_settings",
    "graph_document_payload",
    "graph_neighbors_payload",
    "resolve_graph_reader",
]
