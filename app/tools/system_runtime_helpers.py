"""Shared schema helpers for runtime-coupled system-tool families.

Memory and graph tools still have the most backend-specific schemas in the
system surface. Keeping those helper builders here reduces the size of
`app.services.agent_tools` without forcing the remaining families into a less
readable fully-static format.
"""

from __future__ import annotations

from typing import Any

MEMORY_SCOPE_SCHEMA = {
    "type": "string",
    "enum": ["user", "workspace", "conversation", "workflow", "global"],
}
MEMORY_LINK_TARGET_SCHEMA = {
    "type": "string",
    "enum": ["workflow", "agent", "task"],
}
MEMORY_LINK_REF_SCHEMA = {
    "type": "string",
    "enum": ["memory", "memory_collection"],
}
MEMORY_LINK_ACCESS_SCHEMA = {
    "type": "string",
    "enum": ["read", "read_write"],
}


def graph_working_set_reference_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "execution_id": {"type": "string", "description": "Runtime execution id that owns the working set."},
            "working_set_id": {"type": "string", "description": "Graph working set id."},
        },
        "required": ["execution_id", "working_set_id"],
        "additionalProperties": False,
    }


def graph_working_set_persist_context_pack_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "execution_id": {"type": "string", "description": "Runtime execution id that owns the working set."},
            "working_set_id": {"type": "string", "description": "Graph working set id."},
            "scope": {
                "type": "string",
                "enum": ["workflow", "conversation", "workspace", "user"],
                "default": "workflow",
                "description": "Durable memory scope for the context pack.",
            },
            "summary": {
                "type": ["string", "null"],
                "description": "Optional context-pack summary. A compact default is generated when omitted.",
            },
            "content": {
                "type": ["string", "null"],
                "description": "Optional context-pack body. A deterministic body is generated when omitted.",
            },
            "created_by_user_id": {"type": ["string", "null"], "description": "User owner for user-scoped memory."},
            "workspace_id": {"type": ["string", "null"], "description": "Workspace id for workspace-scoped memory."},
            "conversation_id": {"type": ["string", "null"], "description": "Conversation id for conversation scope."},
            "workflow_id": {"type": ["string", "null"], "description": "Workflow id for workflow scope."},
            "importance": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "default": 45,
                "description": "Memory importance score used when ranking retained context packs.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "maxItems": 25,
                "description": "Optional tags to attach to the persisted context-pack memory.",
            },
            "confirmed": {
                "type": "boolean",
                "default": False,
                "description": "Required when the generated or supplied content is sensitive.",
            },
        },
        "required": ["execution_id", "working_set_id"],
        "additionalProperties": False,
    }


def graph_working_set_create_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "execution_id": {"type": "string", "description": "Runtime execution id that owns the working set."},
            "working_set_id": {
                "type": ["string", "null"],
                "description": "Optional stable working set id. If omitted, one is derived from execution and agent.",
            },
            "owner_agent_id": {"type": ["string", "null"], "description": "Agent that owns the working set."},
            "conversation_id": {"type": ["string", "null"], "description": "Conversation/thread id if known."},
            "workflow_id": {"type": ["string", "null"], "description": "Workflow id if known."},
            "run_id": {"type": ["string", "null"], "description": "Run id if distinct from execution_id."},
            "anchors": {
                "type": "array",
                "items": graph_working_set_anchor_schema(),
                "default": [],
                "maxItems": 25,
                "description": "Initial graph anchors.",
            },
            "notes": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "default": [],
                "maxItems": 25,
                "description": "Initial working-set notes.",
            },
            "ttl_seconds": {
                "type": "integer",
                "minimum": 60,
                "maximum": 86400,
                "default": 21600,
                "description": "Ephemeral lifetime before the runtime prunes this working set.",
            },
        },
        "required": ["execution_id"],
        "additionalProperties": False,
    }


def graph_working_set_add_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "execution_id": {"type": "string", "description": "Runtime execution id that owns the working set."},
            "working_set_id": {"type": "string", "description": "Graph working set id."},
            "anchors": {
                "type": "array",
                "items": graph_working_set_anchor_schema(),
                "default": [],
                "maxItems": 25,
                "description": "Anchor nodes or entities to add to the active graph working set.",
            },
            "visited_nodes": {
                "type": "array",
                "items": graph_working_set_node_schema(),
                "default": [],
                "maxItems": 100,
                "description": "Graph nodes already visited during traversal.",
            },
            "selected_nodes": {
                "type": "array",
                "items": graph_working_set_node_schema(),
                "default": [],
                "maxItems": 100,
                "description": "Graph nodes selected as relevant evidence for the current task.",
            },
            "notes": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "default": [],
                "maxItems": 25,
                "description": "Structured notes to append to the working set.",
            },
            "ttl_seconds": {
                "type": "integer",
                "minimum": 60,
                "maximum": 86400,
                "default": 21600,
                "description": "Ephemeral lifetime before the runtime prunes this working set.",
            },
        },
        "required": ["execution_id", "working_set_id"],
        "additionalProperties": False,
    }


def graph_working_set_remove_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "execution_id": {"type": "string", "description": "Runtime execution id that owns the working set."},
            "working_set_id": {"type": "string", "description": "Graph working set id."},
            "anchor_ids": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "maxItems": 100,
                "description": "Anchor ids to remove from the working set.",
            },
            "visited_node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "maxItems": 100,
                "description": "Visited graph node ids to remove from the working set.",
            },
            "selected_node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "maxItems": 100,
                "description": "Selected evidence node ids to remove from the working set.",
            },
            "clear_notes": {"type": "boolean", "default": False, "description": "Whether to remove all notes."},
            "ttl_seconds": {
                "type": "integer",
                "minimum": 60,
                "maximum": 86400,
                "default": 21600,
                "description": "Ephemeral lifetime before the runtime prunes this working set.",
            },
        },
        "required": ["execution_id", "working_set_id"],
        "additionalProperties": False,
    }


def graph_working_set_anchor_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string"},
            "id": {"type": "string"},
        },
        "required": ["type", "id"],
        "additionalProperties": False,
    }


def graph_working_set_node_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "type": {"type": ["string", "null"]},
            "source_record_type": {"type": ["string", "null"]},
            "source_record_id": {"type": ["string", "null"]},
            "sensitive": {"type": ["boolean", "null"]},
            "sensitivity": {"type": ["string", "null"]},
        },
        "required": ["id"],
        "additionalProperties": False,
    }
