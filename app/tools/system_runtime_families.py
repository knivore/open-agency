"""Concrete runtime-heavy system-tool families.

Memory and graph tools still need Python-owned schema builders, but the tool
identity, descriptions, tags, security, and implementation routing now come
from the shared YAML registry. That keeps the readable declaration layer in one
place while preserving code-owned schema assembly for the complex cases.
"""

from __future__ import annotations

from typing import Any, Callable

from app.domain import SecuritySettings, ToolDefinition, ToolImplementationReference, ToolType
from app.tools.registry_config import load_system_runtime_tool_spec_config
from app.tools.system_runtime_helpers import (
    MEMORY_LINK_ACCESS_SCHEMA,
    MEMORY_LINK_REF_SCHEMA,
    MEMORY_LINK_TARGET_SCHEMA,
    MEMORY_SCOPE_SCHEMA,
    graph_working_set_add_input_schema,
    graph_working_set_create_input_schema,
    graph_working_set_persist_context_pack_input_schema,
    graph_working_set_reference_input_schema,
    graph_working_set_remove_input_schema,
)


def _memory_list_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "scope": {**MEMORY_SCOPE_SCHEMA, "description": "Optional memory scope filter."},
            "query": {"type": ["string", "null"], "description": "Optional text filter."},
            "limit": {
                "type": "integer",
                "minimum": 0,
                "maximum": 50,
                "default": 20,
                "description": "Maximum number of memories to return.",
            },
        },
        "additionalProperties": False,
    }


def _memory_catalog_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "scope": {**MEMORY_SCOPE_SCHEMA, "description": "Optional memory scope filter."},
            "workflow_id": {"type": ["string", "null"], "description": "Optional workflow scope id."},
            "agent_id": {"type": ["string", "null"], "description": "Optional agent scope id."},
            "conversation_id": {"type": ["string", "null"], "description": "Optional conversation scope id."},
            "target_type": {**MEMORY_LINK_TARGET_SCHEMA, "description": "Optional link target type."},
            "target_id": {"type": ["string", "null"], "description": "Optional link target id."},
            "query": {"type": ["string", "null"], "description": "Optional text filter."},
            "include_sensitive": {
                "type": "boolean",
                "default": False,
                "description": "Include sensitive memories when the actor can read them.",
            },
            "status": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Optional memory status filters.",
            },
            "limit_per_group": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "default": 20,
                "description": "Maximum number of memory catalog entries to return per resource group.",
            },
        },
        "additionalProperties": False,
    }


def _memory_remember_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "scope": {**MEMORY_SCOPE_SCHEMA, "description": "Memory scope. Prefer user for personal preferences."},
            "content": {"type": "string", "description": "The exact fact or preference to remember."},
            "summary": {"type": ["string", "null"], "description": "Short display summary."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for retrieval and grouping.",
            },
            "sensitive": {
                "type": "boolean",
                "default": False,
                "description": "Whether the memory contains sensitive personal or business information.",
            },
            "confirmed": {
                "type": "boolean",
                "default": False,
                "description": "Set true only after explicit user confirmation, required for sensitive memory.",
            },
            "workspace_id": {"type": ["string", "null"], "description": "Optional workspace scope id."},
            "conversation_id": {"type": ["string", "null"], "description": "Optional conversation scope id."},
            "workflow_id": {"type": ["string", "null"], "description": "Optional workflow scope id."},
        },
        "required": ["scope", "content"],
        "additionalProperties": False,
    }


def _memory_update_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of the durable memory to update."},
            "content": {"type": ["string", "null"], "description": "Replacement memory content, if changing."},
            "summary": {"type": ["string", "null"], "description": "Replacement short display summary."},
            "tags": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Replacement tag list, if changing tags.",
            },
            "sensitive": {"type": ["boolean", "null"], "description": "Whether the memory is sensitive."},
            "confirmed": {
                "type": "boolean",
                "default": False,
                "description": "Set true only after explicit user confirmation for sensitive updates.",
            },
            "workflow_id": {
                "type": ["string", "null"],
                "description": "Optional workflow id when updating through a workflow memory link.",
            },
            "target_type": {
                **MEMORY_LINK_TARGET_SCHEMA,
                "description": "Optional linked target type. Requires a read_write link when provided.",
            },
            "target_id": {
                "type": ["string", "null"],
                "description": "Optional linked agent/task id. Required for agent and task link targets.",
            },
        },
        "required": ["memory_id"],
        "additionalProperties": False,
    }


def _memory_delete_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "ID of the durable memory to delete."},
            "workflow_id": {
                "type": ["string", "null"],
                "description": "Optional workflow id when deleting through a workflow memory link.",
            },
            "target_type": {
                **MEMORY_LINK_TARGET_SCHEMA,
                "description": "Optional linked target type. Requires a read_write link when provided.",
            },
            "target_id": {
                "type": ["string", "null"],
                "description": "Optional linked agent/task id. Required for agent and task link targets.",
            },
        },
        "required": ["memory_id"],
        "additionalProperties": False,
    }


def _memory_exclusions_list_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "memory_id": {"type": ["string", "null"], "description": "Optional memory id filter."},
            "target_type": {**MEMORY_LINK_TARGET_SCHEMA, "description": "Optional target type filter."},
            "target_id": {"type": ["string", "null"], "description": "Optional target id filter."},
        },
        "additionalProperties": False,
    }


def _memory_exclusions_add_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory id to exclude."},
            "target_type": {
                "type": "string",
                "enum": ["workflow", "agent", "task", "conversation", "global"],
                "description": "Retrieval target that should ignore this memory.",
            },
            "target_id": {"type": ["string", "null"], "description": "Target id, except for global."},
            "reason": {"type": ["string", "null"], "description": "Optional exclusion reason."},
        },
        "required": ["memory_id", "target_type"],
        "additionalProperties": False,
    }


def _memory_exclusions_delete_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory id that owns the exclusion."},
            "exclusion_id": {"type": "string", "description": "Exclusion id to remove."},
        },
        "required": ["memory_id", "exclusion_id"],
        "additionalProperties": False,
    }


def _workflow_memory_links_list_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"workflow_id": {"type": "string", "description": "Workflow id."}},
        "required": ["workflow_id"],
        "additionalProperties": False,
    }


def _workflow_memory_links_add_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string", "description": "Workflow id."},
            "target_type": {
                **MEMORY_LINK_TARGET_SCHEMA,
                "description": "Workflow graph level where the memory should be available.",
            },
            "target_id": {"type": ["string", "null"], "description": "Agent/task id when target is not workflow."},
            "ref_type": {
                **MEMORY_LINK_REF_SCHEMA,
                "description": "Whether the linked reference is a single memory or a memory collection.",
            },
            "ref_id": {"type": "string", "description": "Memory id or document id."},
            "access_mode": {
                **MEMORY_LINK_ACCESS_SCHEMA,
                "default": "read",
                "description": "Use read_write only when the linked runtime may update/delete this memory.",
            },
            "label": {"type": ["string", "null"], "description": "Optional display label."},
        },
        "required": ["workflow_id", "target_type", "ref_type", "ref_id"],
        "additionalProperties": False,
    }


def _workflow_memory_links_delete_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string", "description": "Workflow id."},
            "link_id": {"type": "string", "description": "Workflow memory link id."},
        },
        "required": ["workflow_id", "link_id"],
        "additionalProperties": False,
    }


def _graph_context_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {"type": ["string", "null"],
                      "description": "Natural-language graph search query when no anchor is provided."},
            "intent": {
                "type": "string",
                "enum": ["resume", "debug", "steer", "plan", "audit", "learn", "handoff", "root_cause"],
                "default": "resume",
                "description": "Why context is being requested so the response can prioritize useful sections.",
            },
            "anchor_type": {
                "type": ["string", "null"],
                "enum": [
                    "workflow", "run", "execution", "agent", "task", "step_run", "tool", "model_request",
                    "memory", "context_pack", "conversation", "message", "document", "entity", "error",
                    "approval_request", None,
                ],
                "description": "Graph entity type to anchor the context around.",
            },
            "anchor_id": {"type": ["string", "null"], "description": "Graph entity id matching anchor_type."},
            "scope": {
                "type": ["object", "null"],
                "description": "Optional graph scope. Runtime ids can act as implicit anchors when no query or anchor is provided.",
                "additionalProperties": True,
            },
            "mode": {
                "type": ["string", "null"],
                "enum": ["operational", "knowledge", "lineage", "health", "cost", "security", None],
                "description": "Optional emphasis for future context shaping.",
            },
            "include_memories": {"type": "boolean", "default": True,
                                 "description": "Whether memory/context-pack nodes should be surfaced in related_memories."},
            "include_events": {"type": "boolean", "default": False,
                               "description": "Whether execution/container events should be surfaced in recent_events."},
            "include_raw_graph": {"type": "boolean", "default": False,
                                  "description": "Whether to include bounded raw graph nodes and edges in the graph field."},
            "budget": {"type": "string", "enum": ["brief", "balanced", "full", "raw_graph"], "default": "balanced",
                       "description": "Response size budget."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50,
                      "description": "Maximum graph records to request before budget trimming."},
        },
        "additionalProperties": False,
    }


def _graph_search_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {"type": ["string", "null"],
                      "description": "Optional natural-language or identifier text to search."},
            "labels": {"type": "array", "items": {"type": "string"}, "default": [],
                       "description": "Optional graph label allow-list."},
            "node_types": {"type": "array", "items": {"type": "string"}, "default": [],
                           "description": "Optional canonical node type allow-list."},
            "workflow_id": {"type": ["string", "null"], "description": "Optional workflow scope."},
            "agent_id": {"type": ["string", "null"], "description": "Optional agent scope."},
            "tool_id": {"type": ["string", "null"], "description": "Optional tool scope."},
            "document_id": {"type": ["string", "null"], "description": "Optional document scope."},
            "entity_id": {"type": ["string", "null"], "description": "Optional entity scope."},
            "error_text": {"type": ["string", "null"], "description": "Optional error/status text filter."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50,
                      "description": "Maximum graph nodes to return."},
        },
        "additionalProperties": False,
    }


def _graph_expand_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "minLength": 1, "description": "Graph node id to expand."},
            "preset": {"type": ["string", "null"],
                       "enum": ["workflow", "workflow_run", "agent", "tool", "memory", "entity", "task", None],
                       "description": "Optional canonical neighborhood preset."},
            "mode": {"type": ["string", "null"],
                     "enum": ["operational", "knowledge", "lineage", "health", "cost", "security", None],
                     "description": "Optional graph neighborhood mode."},
            "labels": {"type": "array", "items": {"type": "string"}, "default": [],
                       "description": "Optional center-node label allow-list. Overrides preset/mode labels."},
            "relationship_types": {"type": "array", "items": {"type": "string"}, "default": [],
                                   "description": "Optional relationship type allow-list. Overrides preset/mode relationships."},
            "depth": {"type": "integer", "minimum": 1, "maximum": 2, "default": 1, "description": "Expansion depth."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50,
                      "description": "Maximum path records before payload trimming."},
            "include_deleted": {"type": "boolean", "default": False,
                                "description": "Whether deleted graph records may be included."},
        },
        "required": ["node_id"],
        "additionalProperties": False,
    }


def _graph_neighbors_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "minLength": 1, "description": "Graph node id to inspect."},
            "preset": {"type": ["string", "null"],
                       "enum": ["workflow", "workflow_run", "agent", "tool", "memory", "entity", "task", None],
                       "description": "Optional canonical neighborhood preset."},
            "mode": {"type": ["string", "null"],
                     "enum": ["operational", "knowledge", "lineage", "health", "cost", "security", None],
                     "description": "Optional graph neighborhood mode."},
            "labels": {"type": "array", "items": {"type": "string"}, "default": [],
                       "description": "Optional center-node label allow-list. Overrides preset/mode labels."},
            "relationship_types": {"type": "array", "items": {"type": "string"}, "default": [],
                                   "description": "Optional relationship type allow-list. Overrides preset/mode relationships."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50,
                      "description": "Maximum one-hop path records before payload trimming."},
            "include_deleted": {"type": "boolean", "default": False,
                                "description": "Whether deleted graph records may be included."},
        },
        "required": ["node_id"],
        "additionalProperties": False,
    }


def _graph_path_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "path_type": {"type": "string",
                          "enum": ["shortest", "memory_source_run", "failed_run_root_cause", "influence",
                                   "agent_prior_runs"], "description": "Approved path template to run."},
            "source_id": {"type": ["string", "null"], "description": "Source node id for shortest path."},
            "target_id": {"type": ["string", "null"], "description": "Target node id for shortest path."},
            "relationship_types": {"type": "array", "items": {"type": "string"}, "default": [],
                                   "description": "Optional relationship type allow-list for shortest path."},
            "memory_id": {"type": ["string", "null"], "description": "Memory id for memory provenance paths."},
            "run_id": {"type": ["string", "null"],
                       "description": "Workflow run id for run-root-cause, memory-source-run, or agent-prior-runs paths."},
            "anchor_type": {"type": ["string", "null"], "enum": ["document", "entity", None],
                            "description": "Influence path anchor type."},
            "anchor_id": {"type": ["string", "null"], "description": "Influence path anchor id."},
            "workflow_id": {"type": ["string", "null"], "description": "Optional workflow target for influence paths."},
            "agent_id": {"type": ["string", "null"], "description": "Agent id for prior-run paths."},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 4, "default": 4,
                          "description": "Maximum traversal depth."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25,
                      "description": "Maximum path records before payload trimming."},
        },
        "required": ["path_type"],
        "additionalProperties": False,
    }


def _graph_summarize_subgraph_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "nodes": {"type": "array", "items": {"type": "object"}, "maxItems": 100,
                      "description": "Normalized graph nodes to summarize."},
            "edges": {"type": "array", "items": {"type": "object"}, "maxItems": 100, "default": [],
                      "description": "Normalized graph edges to summarize."},
            "meta": {"type": "object", "default": {},
                     "description": "Optional graph metadata to carry into provenance.", "additionalProperties": True},
            "query": {"type": ["string", "null"], "description": "Optional label for the selected subgraph."},
            "intent": {"type": "string",
                       "enum": ["resume", "debug", "steer", "plan", "audit", "learn", "handoff", "root_cause"],
                       "default": "learn", "description": "Why the agent is compacting this subgraph."},
            "anchor_type": {"type": ["string", "null"],
                            "enum": ["workflow", "run", "execution", "agent", "task", "step_run", "tool",
                                     "model_request", "memory", "context_pack", "conversation", "message", "document",
                                     "entity", "error", "approval_request", None],
                            "description": "Optional anchor type represented by the selected subgraph."},
            "anchor_id": {"type": ["string", "null"],
                          "description": "Optional anchor id represented by the selected subgraph."},
            "scope": {"type": ["object", "null"],
                      "description": "Optional scope used for memory visibility and provenance context.",
                      "additionalProperties": True},
            "mode": {"type": ["string", "null"],
                     "enum": ["operational", "knowledge", "lineage", "health", "cost", "security", None],
                     "description": "Optional emphasis for the compact context."},
            "include_memories": {"type": "boolean", "default": True,
                                 "description": "Whether memory/context-pack nodes should be surfaced in related_memories."},
            "include_events": {"type": "boolean", "default": False,
                               "description": "Whether event nodes should be surfaced in recent_events."},
            "include_raw_graph": {"type": "boolean", "default": False,
                                  "description": "Whether to include a budgeted raw graph excerpt in the graph field."},
            "budget": {"type": "string", "enum": ["brief", "balanced", "full", "raw_graph"], "default": "balanced",
                       "description": "Response size budget."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50,
                      "description": "Maximum graph records to consider before budget trimming."},
        },
        "required": ["nodes"],
        "additionalProperties": False,
    }


INPUT_SCHEMA_BUILDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "memory_list_input_schema": _memory_list_input_schema,
    "memory_catalog_input_schema": _memory_catalog_input_schema,
    "memory_remember_input_schema": _memory_remember_input_schema,
    "memory_update_input_schema": _memory_update_input_schema,
    "memory_delete_input_schema": _memory_delete_input_schema,
    "memory_exclusions_list_input_schema": _memory_exclusions_list_input_schema,
    "memory_exclusions_add_input_schema": _memory_exclusions_add_input_schema,
    "memory_exclusions_delete_input_schema": _memory_exclusions_delete_input_schema,
    "workflow_memory_links_list_input_schema": _workflow_memory_links_list_input_schema,
    "workflow_memory_links_add_input_schema": _workflow_memory_links_add_input_schema,
    "workflow_memory_links_delete_input_schema": _workflow_memory_links_delete_input_schema,
    "graph_context_input_schema": _graph_context_input_schema,
    "graph_search_input_schema": _graph_search_input_schema,
    "graph_expand_input_schema": _graph_expand_input_schema,
    "graph_neighbors_input_schema": _graph_neighbors_input_schema,
    "graph_path_input_schema": _graph_path_input_schema,
    "graph_summarize_subgraph_input_schema": _graph_summarize_subgraph_input_schema,
    "graph_working_set_create_input_schema": graph_working_set_create_input_schema,
    "graph_working_set_add_input_schema": graph_working_set_add_input_schema,
    "graph_working_set_remove_input_schema": graph_working_set_remove_input_schema,
    "graph_working_set_reference_input_schema": graph_working_set_reference_input_schema,
    "graph_working_set_persist_context_pack_input_schema": graph_working_set_persist_context_pack_input_schema,
}


def _runtime_tool_specs_for(family: str) -> list[dict[str, Any]]:
    specs = load_system_runtime_tool_spec_config()
    family_specs = specs.get(family) or []
    return list(family_specs if isinstance(family_specs, list) else [])


def _materialize_runtime_tool_specs(
        specs: list[dict[str, Any]],
        *,
        output_schemas: dict[str, dict[str, Any]],
) -> list[ToolDefinition]:
    tools: list[ToolDefinition] = []
    for spec in specs:
        input_schema_ref = str(spec["input_schema_ref"])
        input_schema_builder = INPUT_SCHEMA_BUILDERS[input_schema_ref]
        tools.append(
            ToolDefinition(
                id=spec["id"],
                name=spec["name"],
                display_name=spec["display_name"],
                description=spec["description"],
                tool_type=ToolType[spec["tool_type"]],
                input_schema=input_schema_builder(),
                output_schema=output_schemas[spec["output_schema_name"]],
                implementation=ToolImplementationReference.model_validate(spec["implementation"]),
                security=SecuritySettings(**spec["security"]),
                tags=list(spec["tags"]),
            )
        )
    return tools


def memory_runtime_tool_definitions(
        *,
        items_output_schema: dict[str, Any],
        result_output_schema: dict[str, Any],
) -> list[ToolDefinition]:
    """Build the memory tool family from YAML metadata plus Python schema builders."""
    return _materialize_runtime_tool_specs(
        _runtime_tool_specs_for("memory"),
        output_schemas={
            "ITEMS_OUTPUT_SCHEMA": items_output_schema,
            "RESULT_OUTPUT_SCHEMA": result_output_schema,
        },
    )


def graph_runtime_tool_definitions(
        *,
        result_output_schema: dict[str, Any],
        graph_context_output_schema: dict[str, Any],
        graph_document_output_schema: dict[str, Any],
        graph_neighbors_output_schema: dict[str, Any],
) -> list[ToolDefinition]:
    """Build the graph tool family from YAML metadata plus Python schema builders."""
    return _materialize_runtime_tool_specs(
        _runtime_tool_specs_for("graph"),
        output_schemas={
            "RESULT_OUTPUT_SCHEMA": result_output_schema,
            "GRAPH_CONTEXT_OUTPUT_SCHEMA": graph_context_output_schema,
            "GRAPH_DOCUMENT_OUTPUT_SCHEMA": graph_document_output_schema,
            "GRAPH_NEIGHBORS_OUTPUT_SCHEMA": graph_neighbors_output_schema,
        },
    )
