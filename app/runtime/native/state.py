"""Execution state stores and event persistence mappers for the native runtime."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from typing import Any, Dict, List, Optional, Protocol
from uuid import uuid4

from app.core.config import get_settings
from app.core.time import utc_now
from app.db.models import ApprovalRequestORM, ExecutionArtifactORM, ExecutionEventORM, ExecutionORM, ToolInvocationORM
from app.db.repositories import (
    InMemoryExecutionArtifactRepository,
    InMemoryExecutionEventRepository,
    InMemoryExecutionRepository,
    MongoExecutionArtifactRepository,
    MongoExecutionEventRepository,
    MongoExecutionRepository,
)
from app.domain import (
    Execution,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionEventType,
    GraphProjectionEvent,
    ModelProfileDefinition,
    WorkflowDefinition,
)
from app.services.execution_activity import apply_activity_to_execution

GRAPH_PROJECTED_EXECUTION_EVENT_TYPES = {
    ExecutionEventType.APPROVAL_GRANTED,
    ExecutionEventType.APPROVAL_REJECTED,
    ExecutionEventType.APPROVAL_REQUESTED,
    ExecutionEventType.ARTIFACT_CREATED,
    ExecutionEventType.CONTAINER_CREATED,
    ExecutionEventType.CONTAINER_FAILED,
    ExecutionEventType.CONTAINER_REPLACED,
    ExecutionEventType.CONTAINER_STARTED,
    ExecutionEventType.CONTAINER_STOPPED,
    ExecutionEventType.CONTEXT_COMPACTION_COMPLETED,
    ExecutionEventType.CONTEXT_COMPACTION_FAILED,
    ExecutionEventType.CONTEXT_COMPACTION_STARTED,
    ExecutionEventType.CONTEXT_HEALTH_RECORDED,
    ExecutionEventType.EXECUTION_STARTED,
    ExecutionEventType.EXECUTION_COMPLETED,
    ExecutionEventType.EXECUTION_FAILED,
    ExecutionEventType.LLM_REQUEST_CREATED,
    ExecutionEventType.LLM_RESPONSE_CREATED,
    ExecutionEventType.MODEL_FALLBACK_FAILED,
    ExecutionEventType.MODEL_FALLBACK_USED,
    ExecutionEventType.MONITOR_FINDING_CREATED,
    ExecutionEventType.RUNTIME_REVISION_RESOLVED,
    ExecutionEventType.TASK_STARTED,
    ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
    ExecutionEventType.TOKEN_BUDGET_WARNING,
    ExecutionEventType.TOKEN_USAGE_RECORDED,
    ExecutionEventType.AGENT_STEP_COMPLETED,
    ExecutionEventType.AGENT_STEP_FAILED,
    ExecutionEventType.TOOL_CALL_COMPLETED,
    ExecutionEventType.TOOL_CALL_FAILED,
    ExecutionEventType.TOOL_CALL_STARTED,
}
logger = logging.getLogger(__name__)

GRAPH_WORKING_SET_TTL_SECONDS = 6 * 60 * 60
GRAPH_WORKING_SET_LIMIT = 10
GRAPH_WORKING_SET_NODE_LIMIT = 100
GRAPH_WORKING_SET_NOTE_LIMIT = 25


@dataclass
class GraphWorkingSet:
    working_set_id: str
    owner_agent_id: str | None
    conversation_id: str | None
    workflow_id: str | None
    run_id: str | None
    execution_id: str | None
    anchors: List[Dict[str, Any]] = field(default_factory=list)
    visited_nodes: List[Dict[str, Any]] = field(default_factory=list)
    selected_nodes: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[Dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    expires_at: datetime = field(
        default_factory=lambda: utc_now() + timedelta(seconds=GRAPH_WORKING_SET_TTL_SECONDS)
    )

    def expired(self, *, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or utc_now())

    def refresh(self, *, now: datetime | None = None, ttl_seconds: int = GRAPH_WORKING_SET_TTL_SECONDS) -> None:
        reference = now or utc_now()
        self.updated_at = reference
        self.expires_at = reference + timedelta(seconds=ttl_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "working_set_id": self.working_set_id,
            "owner_agent_id": self.owner_agent_id,
            "conversation_id": self.conversation_id,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "anchors": list(self.anchors),
            "visited_nodes": list(self.visited_nodes),
            "selected_nodes": list(self.selected_nodes),
            "notes": list(self.notes),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass
class NativeExecutionState:
    execution_id: str
    workflow_id: str
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    paused: bool = False
    cancelled: bool = False
    sequence: int = 0
    node_outputs: Dict[str, Any] = field(default_factory=dict)
    memory_entries: List[Dict[str, Any]] = field(default_factory=list)
    graph_context_entries: List[Dict[str, Any]] = field(default_factory=list)
    graph_working_sets: Dict[str, GraphWorkingSet] = field(default_factory=dict)
    context_compaction: Dict[str, Any] = field(default_factory=dict)
    compacted_context_packs: List[Dict[str, Any]] = field(default_factory=list)
    current_node_id: Optional[str] = None
    current_agent_id: Optional[str] = None
    current_task_id: Optional[str] = None
    planned_node_ids: List[str] = field(default_factory=list)
    terminal_node_ids: List[str] = field(default_factory=list)
    last_event_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence


def prune_expired_graph_working_sets(state: NativeExecutionState, *, now: datetime | None = None) -> None:
    reference = now or utc_now()
    state.graph_working_sets = {
        working_set_id: working_set
        for working_set_id, working_set in state.graph_working_sets.items()
        if not working_set.expired(now=reference)
    }


def create_graph_working_set(
        state: NativeExecutionState,
        *,
        working_set_id: str | None = None,
        owner_agent_id: str | None = None,
        conversation_id: str | None = None,
        workflow_id: str | None = None,
        run_id: str | None = None,
        execution_id: str | None = None,
        anchors: list[dict[str, Any]] | None = None,
        notes: list[dict[str, Any]] | None = None,
        ttl_seconds: int = GRAPH_WORKING_SET_TTL_SECONDS,
) -> GraphWorkingSet:
    now = utc_now()
    prune_expired_graph_working_sets(state, now=now)
    resolved_execution_id = execution_id or state.execution_id
    resolved_workflow_id = workflow_id or state.workflow_id
    resolved_run_id = run_id or resolved_execution_id
    resolved_id = working_set_id or _graph_working_set_id(
        execution_id=resolved_execution_id,
        owner_agent_id=owner_agent_id,
    )
    working_set = GraphWorkingSet(
        working_set_id=resolved_id,
        owner_agent_id=owner_agent_id,
        conversation_id=conversation_id,
        workflow_id=resolved_workflow_id,
        run_id=resolved_run_id,
        execution_id=resolved_execution_id,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    _merge_unique_dicts(
        working_set.anchors,
        [_normalize_anchor(anchor) for anchor in anchors or [] if _normalize_anchor(anchor)],
        key_fields=("type", "id"),
        limit=GRAPH_WORKING_SET_NODE_LIMIT,
    )
    for note in notes or []:
        if isinstance(note, dict) and note:
            working_set.notes.append(dict(note))
    working_set.notes = working_set.notes[-GRAPH_WORKING_SET_NOTE_LIMIT:]
    state.graph_working_sets[working_set.working_set_id] = working_set
    if len(state.graph_working_sets) > GRAPH_WORKING_SET_LIMIT:
        ordered_sets = sorted(state.graph_working_sets.values(), key=lambda item: item.updated_at)
        for expired_set in ordered_sets[: len(state.graph_working_sets) - GRAPH_WORKING_SET_LIMIT]:
            state.graph_working_sets.pop(expired_set.working_set_id, None)
    return working_set


def add_graph_working_set_items(
        working_set: GraphWorkingSet,
        *,
        anchors: list[dict[str, Any]] | None = None,
        visited_nodes: list[dict[str, Any]] | None = None,
        selected_nodes: list[dict[str, Any]] | None = None,
        notes: list[dict[str, Any]] | None = None,
        ttl_seconds: int = GRAPH_WORKING_SET_TTL_SECONDS,
) -> GraphWorkingSet:
    working_set.refresh(ttl_seconds=ttl_seconds)
    _merge_unique_dicts(
        working_set.anchors,
        [_normalize_anchor(anchor) for anchor in anchors or [] if _normalize_anchor(anchor)],
        key_fields=("type", "id"),
        limit=GRAPH_WORKING_SET_NODE_LIMIT,
    )
    _merge_unique_dicts(
        working_set.visited_nodes,
        [_working_set_node(node) for node in visited_nodes or [] if isinstance(node, dict) and node.get("id")],
        key_fields=("id",),
        limit=GRAPH_WORKING_SET_NODE_LIMIT,
    )
    _merge_unique_dicts(
        working_set.selected_nodes,
        [_working_set_node(node) for node in selected_nodes or [] if isinstance(node, dict) and node.get("id")],
        key_fields=("id",),
        limit=GRAPH_WORKING_SET_NODE_LIMIT,
    )
    for note in notes or []:
        if isinstance(note, dict) and note:
            working_set.notes.append(dict(note))
    working_set.notes = working_set.notes[-GRAPH_WORKING_SET_NOTE_LIMIT:]
    return working_set


def remove_graph_working_set_items(
        working_set: GraphWorkingSet,
        *,
        anchor_ids: list[str] | None = None,
        visited_node_ids: list[str] | None = None,
        selected_node_ids: list[str] | None = None,
        clear_notes: bool = False,
        ttl_seconds: int = GRAPH_WORKING_SET_TTL_SECONDS,
) -> GraphWorkingSet:
    working_set.refresh(ttl_seconds=ttl_seconds)
    anchor_id_set = {str(item) for item in anchor_ids or [] if str(item)}
    visited_id_set = {str(item) for item in visited_node_ids or [] if str(item)}
    selected_id_set = {str(item) for item in selected_node_ids or [] if str(item)}
    if anchor_id_set:
        working_set.anchors = [item for item in working_set.anchors if str(item.get("id")) not in anchor_id_set]
    if visited_id_set:
        working_set.visited_nodes = [
            item for item in working_set.visited_nodes if str(item.get("id")) not in visited_id_set
        ]
    if selected_id_set:
        working_set.selected_nodes = [
            item for item in working_set.selected_nodes if str(item.get("id")) not in selected_id_set
        ]
    if clear_notes:
        working_set.notes = []
    return working_set


def record_graph_context_working_set_entry(
        state: NativeExecutionState,
        entry: dict[str, Any],
        *,
        owner_agent_id: str | None,
        workflow_id: str | None,
        run_id: str | None = None,
        execution_id: str | None = None,
        conversation_id: str | None = None,
        ttl_seconds: int = GRAPH_WORKING_SET_TTL_SECONDS,
) -> GraphWorkingSet:
    """Track graph nodes touched by runtime graph context without persisting them."""
    now = utc_now()
    prune_expired_graph_working_sets(state, now=now)
    resolved_execution_id = execution_id or state.execution_id
    resolved_workflow_id = workflow_id or state.workflow_id
    resolved_run_id = run_id or resolved_execution_id
    working_set_id = _graph_working_set_id(
        execution_id=resolved_execution_id,
        owner_agent_id=owner_agent_id,
    )
    working_set = state.graph_working_sets.get(working_set_id)
    if working_set is None:
        working_set = GraphWorkingSet(
            working_set_id=working_set_id,
            owner_agent_id=owner_agent_id,
            conversation_id=conversation_id,
            workflow_id=resolved_workflow_id,
            run_id=resolved_run_id,
            execution_id=resolved_execution_id,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        state.graph_working_sets[working_set_id] = working_set
    else:
        if conversation_id and not working_set.conversation_id:
            working_set.conversation_id = conversation_id
        if resolved_workflow_id and not working_set.workflow_id:
            working_set.workflow_id = resolved_workflow_id
        if resolved_run_id and not working_set.run_id:
            working_set.run_id = resolved_run_id
        if resolved_execution_id and not working_set.execution_id:
            working_set.execution_id = resolved_execution_id
        working_set.refresh(now=now, ttl_seconds=ttl_seconds)

    context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
    query_meta = context.get("query_meta") if isinstance(context.get("query_meta"), dict) else {}
    anchor_type = query_meta.get("anchor_type") or entry.get("anchor_type")
    anchor_id = query_meta.get("anchor_id") or entry.get("anchor_id")
    if anchor_type and anchor_id:
        _merge_unique_dicts(
            working_set.anchors,
            [{"type": str(anchor_type), "id": str(anchor_id)}],
            key_fields=("type", "id"),
            limit=GRAPH_WORKING_SET_NODE_LIMIT,
        )

    provenance = context.get("provenance") if isinstance(context.get("provenance"), dict) else {}
    visited_nodes = [
        _working_set_node(node)
        for node in provenance.get("nodes") or []
        if isinstance(node, dict) and node.get("id")
    ]
    _merge_unique_dicts(
        working_set.visited_nodes,
        visited_nodes,
        key_fields=("id",),
        limit=GRAPH_WORKING_SET_NODE_LIMIT,
    )
    _merge_unique_dicts(
        working_set.selected_nodes,
        _selected_graph_nodes(entry),
        key_fields=("id",),
        limit=GRAPH_WORKING_SET_NODE_LIMIT,
    )
    note = _graph_working_set_note(entry)
    if note:
        working_set.notes.append(note)
        working_set.notes = working_set.notes[-GRAPH_WORKING_SET_NOTE_LIMIT:]

    if len(state.graph_working_sets) > GRAPH_WORKING_SET_LIMIT:
        ordered_sets = sorted(state.graph_working_sets.values(), key=lambda item: item.updated_at)
        for expired_set in ordered_sets[: len(state.graph_working_sets) - GRAPH_WORKING_SET_LIMIT]:
            state.graph_working_sets.pop(expired_set.working_set_id, None)
    entry["working_set_id"] = working_set.working_set_id
    return working_set


def _graph_working_set_id(*, execution_id: str | None, owner_agent_id: str | None) -> str:
    execution_part = _stable_identifier_part(execution_id or "execution")
    owner_part = _stable_identifier_part(owner_agent_id or "agent")
    return f"graph-working-set-{execution_part}-{owner_part}"


def _stable_identifier_part(value: str) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in value)


def _merge_unique_dicts(
        target: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
        *,
        key_fields: tuple[str, ...],
        limit: int,
) -> None:
    seen = {
        tuple(str(item.get(field) or "") for field in key_fields)
        for item in target
        if all(item.get(field) for field in key_fields)
    }
    for item in incoming:
        key = tuple(str(item.get(field) or "") for field in key_fields)
        if not all(key) or key in seen:
            continue
        target.append(item)
        seen.add(key)
    if len(target) > limit:
        del target[:-limit]


def _working_set_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "id": node.get("id"),
            "type": node.get("type"),
            "source_record_type": node.get("source_record_type"),
            "source_record_id": node.get("source_record_id"),
            "sensitive": node.get("sensitive"),
            "sensitivity": node.get("sensitivity"),
        }.items()
        if value is not None
    }


def _normalize_anchor(anchor: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(anchor, dict):
        return None
    anchor_type = anchor.get("type") or anchor.get("anchor_type")
    anchor_id = anchor.get("id") or anchor.get("anchor_id")
    if not anchor_type or not anchor_id:
        return None
    return {"type": str(anchor_type), "id": str(anchor_id)}


def _selected_graph_nodes(entry: dict[str, Any]) -> list[dict[str, Any]]:
    context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
    query_meta = context.get("query_meta") if isinstance(context.get("query_meta"), dict) else {}
    selected: list[dict[str, Any]] = []
    anchor_type = query_meta.get("anchor_type") or entry.get("anchor_type")
    anchor_id = query_meta.get("anchor_id") or entry.get("anchor_id")
    if anchor_type and anchor_id:
        selected.append({"id": str(anchor_id), "type": str(anchor_type)})
    for section_name in (
            "related_memories",
            "related_documents",
            "prior_attempts",
            "prior_changes",
            "failures",
            "decisions",
            "constraints",
            "open_questions",
            "next_actions",
    ):
        for item in context.get(section_name) or []:
            if isinstance(item, dict) and item.get("id"):
                selected.append(_working_set_node(item))
    return selected


def _graph_working_set_note(entry: dict[str, Any]) -> dict[str, Any] | None:
    context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
    query_meta = context.get("query_meta") if isinstance(context.get("query_meta"), dict) else {}
    summary = context.get("summary")
    note = {
        "trigger": entry.get("trigger"),
        "reason": entry.get("reason"),
        "intent": query_meta.get("intent") or entry.get("intent"),
        "budget": query_meta.get("budget") or entry.get("budget"),
        "status": context.get("status"),
    }
    if summary:
        note["summary"] = str(summary)[:500]
    note = {key: value for key, value in note.items() if value is not None}
    return note or None


class WorkflowRepository(Protocol):
    async def get_workflow(self, workflow_id: str) -> Optional[WorkflowDefinition]: ...

    async def save_workflow(self, workflow: WorkflowDefinition) -> WorkflowDefinition: ...


class ModelProfileRepository(Protocol):
    async def get_profile(self, profile_id: str) -> Optional[ModelProfileDefinition]: ...

    async def save_profile(self, profile: ModelProfileDefinition) -> ModelProfileDefinition: ...


class ExecutionStore(Protocol):
    async def save_execution(self, execution: Execution) -> Execution: ...

    async def update_execution(self, execution: Execution) -> Execution: ...

    async def get_execution(self, execution_id: str) -> Optional[Execution]: ...

    async def list_executions(self) -> List[Execution]: ...

    async def save_event(self, event: ExecutionEvent) -> ExecutionEvent: ...

    async def list_events(self, execution_id: str) -> List[ExecutionEvent]: ...

    async def list_events_after(self, execution_id: str, after_sequence: int) -> List[ExecutionEvent]: ...

    async def delete_event(self, event_id: str) -> bool: ...

    async def save_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact: ...

    async def list_artifacts(self, execution_id: str) -> List[ExecutionArtifact]: ...

    async def list_active_executions(self) -> List[Execution]: ...

    async def list_executions_by_workflow(self, workflow_id: str) -> List[Execution]: ...

    async def list_executions_by_agent(self, agent_id: str) -> List[Execution]: ...

    async def delete_execution(self, execution_id: str) -> bool: ...

    async def create_approval_request(self, *, execution_id: str, event_id: str | None, tool_id: str, status: str,
                                      payload: dict[str, Any]) -> str: ...

    async def update_approval_request(self, request_id: str, *, status: str, response_payload: dict[str, Any],
                                      responded_by: str | None = None) -> None: ...

    async def list_approval_requests(self, execution_id: str) -> list[dict[str, Any]]: ...

    async def acquire_lock(self, execution_id: str, worker_id: str, stale_after_seconds: int = 30) -> bool: ...

    async def release_lock(self, execution_id: str, worker_id: str) -> bool: ...

    async def heartbeat(self, execution_id: str, worker_id: str) -> Optional[Execution]: ...


def _serialize_execution_trigger(trigger_payload: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {"payload": trigger_payload, "metadata": metadata}


def _deserialize_execution_payload(payload: Dict[str, Any] | None) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if isinstance(payload, dict) and "payload" in payload and "metadata" in payload:
        return dict(payload.get("payload") or {}), dict(payload.get("metadata") or {})
    return dict(payload or {}), {}


def _execution_to_orm(execution: Execution) -> ExecutionORM:
    return ExecutionORM(
        id=execution.id,
        workflow_id=execution.workflow_id,
        goal_id=execution.goal_id,
        workflow_version_id=execution.workflow_version_id,
        status=execution.status.value,
        runtime_adapter=execution.runtime_adapter_id,
        runtime_revision_id=execution.runtime_revision_id,
        runtime_fingerprint=execution.runtime_fingerprint,
        trigger_type=execution.trigger_type,
        trigger_payload_json=_serialize_execution_trigger(execution.trigger_payload, execution.metadata),
        input_json=execution.input_payload,
        output_json=execution.output_payload,
        error_json=execution.error_json,
        metadata_json=execution.metadata,
        created_by=execution.created_by,
        worker_id=execution.worker_id,
        created_at=execution.created_at,
        started_at=execution.started_at,
        ended_at=execution.completed_at,
        updated_at=execution.updated_at,
        last_heartbeat_at=execution.last_heartbeat_at,
        container_id=execution.container_id,
        container_name=execution.container_name,
        container_image=execution.container_image,
        container_status=execution.container_status,
        container_started_at=execution.container_started_at,
        container_ended_at=execution.container_ended_at,
        container_exit_code=execution.container_exit_code,
        replacement_of_execution_id=execution.replacement_of_execution_id,
        restart_reason=execution.restart_reason,
    )


def _execution_from_orm(orm: ExecutionORM) -> Execution:
    trigger_payload, metadata = _deserialize_execution_payload(orm.trigger_payload_json)
    metadata.update(dict(orm.metadata_json or {}))
    return Execution.model_validate(
        {
            "id": orm.id,
            "workflow_id": orm.workflow_id,
            "goal_id": orm.goal_id,
            "workflow_version_id": orm.workflow_version_id,
            "runtime_adapter": orm.runtime_adapter,
            "runtime_revision_id": orm.runtime_revision_id,
            "runtime_fingerprint": orm.runtime_fingerprint,
            "status": orm.status,
            "trigger_type": orm.trigger_type,
            "trigger_payload": trigger_payload,
            "input_json": orm.input_json,
            "output_json": orm.output_json,
            "error": (
                orm.error_json.get("message") if isinstance(orm.error_json, dict) else orm.error_json
            ),
            "created_by": orm.created_by,
            "worker_id": orm.worker_id,
            "created_at": orm.created_at,
            "started_at": orm.started_at,
            "ended_at": orm.ended_at,
            "updated_at": orm.updated_at,
            "last_heartbeat_at": orm.last_heartbeat_at,
            "container_id": orm.container_id,
            "container_name": orm.container_name,
            "container_image": orm.container_image,
            "container_status": orm.container_status,
            "container_started_at": orm.container_started_at,
            "container_ended_at": orm.container_ended_at,
            "container_exit_code": orm.container_exit_code,
            "replacement_of_execution_id": orm.replacement_of_execution_id,
            "restart_reason": orm.restart_reason,
            "metadata": metadata,
        }
    )


def _datetime_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _execution_projection_payload(execution: Execution | None) -> dict[str, Any]:
    if execution is None:
        return {}
    return {
        "workflow_version_id": execution.workflow_version_id,
        "goal_id": execution.goal_id,
        "runtime_adapter_id": execution.runtime_adapter_id,
        "runtime_revision_id": execution.runtime_revision_id,
        "runtime_fingerprint": execution.runtime_fingerprint,
        "trigger_type": execution.trigger_type,
        "trigger_payload": execution.trigger_payload,
        "execution_error": execution.error,
        "created_at": _datetime_iso(execution.created_at),
        "started_at": _datetime_iso(execution.started_at),
        "completed_at": _datetime_iso(execution.completed_at),
        "updated_at": _datetime_iso(execution.updated_at),
        "container_id": execution.container_id,
        "container_name": execution.container_name,
        "container_image": execution.container_image,
        "container_status": execution.container_status,
        "container_started_at": _datetime_iso(execution.container_started_at),
        "container_ended_at": _datetime_iso(execution.container_ended_at),
        "container_exit_code": execution.container_exit_code,
        "replacement_of_execution_id": execution.replacement_of_execution_id,
        "restart_reason": execution.restart_reason,
    }


def _event_to_orm(event: ExecutionEvent) -> ExecutionEventORM:
    payload_json = {
        "payload": event.payload,
        "metrics": event.metrics,
        "metadata": event.metadata,
        "workflow_id": event.workflow_id,
        "agent_id": event.agent_id,
        "task_id": event.task_id,
        "tool_call_id": event.tool_call_id,
        "model_request_id": event.model_request_id,
        "redacted_fields": event.redacted_fields,
        "source": event.source,
        "status": event.status,
        "payload_sha256": event.payload_sha256,
    }
    return ExecutionEventORM(
        id=event.id,
        execution_id=event.execution_id,
        sequence=event.sequence,
        event_type=event.event_type.value,
        timestamp=event.timestamp,
        actor_type=event.actor_type,
        actor_id=event.actor,
        payload_json=payload_json,
        parent_event_id=event.parent_event_id,
        trace_id=event.trace_id,
        span_id=event.span_id,
    )


def _event_from_orm(orm: ExecutionEventORM) -> ExecutionEvent:
    payload = dict(orm.payload_json or {})
    return ExecutionEvent.model_validate(
        {
            "id": orm.id,
            "execution_id": orm.execution_id,
            "workflow_id": payload.get("workflow_id"),
            "agent_id": payload.get("agent_id"),
            "task_id": payload.get("task_id"),
            "tool_call_id": payload.get("tool_call_id"),
            "model_request_id": payload.get("model_request_id"),
            "parent_event_id": orm.parent_event_id,
            "trace_id": orm.trace_id,
            "span_id": orm.span_id,
            "event_type": orm.event_type,
            "timestamp": orm.timestamp,
            "sequence": orm.sequence,
            "actor_type": orm.actor_type,
            "actor_id": orm.actor_id,
            "source": payload.get("source"),
            "status": payload.get("status"),
            "payload_json": payload.get("payload", {}),
            "payload_sha256": payload.get("payload_sha256"),
            "metrics": payload.get("metrics", {}),
            "metadata": payload.get("metadata", {}),
            "redacted_fields": payload.get("redacted_fields", []),
        }
    )


def _artifact_to_orm(artifact: ExecutionArtifact) -> ExecutionArtifactORM:
    metadata = dict(artifact.metadata)
    if artifact.size_bytes is not None:
        metadata["size_bytes"] = artifact.size_bytes
    return ExecutionArtifactORM(
        id=artifact.id,
        execution_id=artifact.execution_id,
        event_id=artifact.event_id,
        artifact_type=artifact.artifact_type,
        name=artifact.name,
        content_json=artifact.content_json,
        content_text=artifact.content_text,
        file_path=artifact.uri,
        mime_type=artifact.media_type,
        metadata_json=metadata,
        created_at=artifact.created_at,
    )


def _artifact_from_orm(orm: ExecutionArtifactORM) -> ExecutionArtifact:
    metadata = dict(orm.metadata_json or {})
    return ExecutionArtifact.model_validate(
        {
            "id": orm.id,
            "execution_id": orm.execution_id,
            "event_id": orm.event_id,
            "artifact_type": orm.artifact_type,
            "name": orm.name,
            "content_json": orm.content_json,
            "content_text": orm.content_text,
            "file_path": orm.file_path,
            "mime_type": orm.mime_type,
            "created_at": orm.created_at,
            "metadata_json": {k: v for k, v in metadata.items() if k != "size_bytes"},
            "size_bytes": metadata.get("size_bytes"),
        }
    )


def _approval_request_from_orm(orm: ApprovalRequestORM) -> dict[str, Any]:
    return {
        "id": orm.id,
        "execution_id": orm.execution_id,
        "event_id": orm.event_id,
        "tool_id": orm.tool_id,
        "status": orm.status,
        "request_payload": dict(orm.request_payload_json or {}),
        "response_payload": dict(orm.response_payload_json or {}),
        "requested_at": orm.requested_at.isoformat() if orm.requested_at else None,
        "responded_at": orm.responded_at.isoformat() if orm.responded_at else None,
        "responded_by": orm.responded_by,
    }


class InMemoryWorkflowRepository:
    def __init__(self):
        self._workflows: Dict[str, WorkflowDefinition] = {}

    async def get_workflow(self, workflow_id: str) -> Optional[WorkflowDefinition]:
        return self._workflows.get(workflow_id)

    async def save_workflow(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        self._workflows[workflow.id] = workflow
        return workflow


class InMemoryModelProfileRepository:
    def __init__(self):
        self._profiles: Dict[str, ModelProfileDefinition] = {}

    async def get_profile(self, profile_id: str) -> Optional[ModelProfileDefinition]:
        return self._profiles.get(profile_id)

    async def save_profile(self, profile: ModelProfileDefinition) -> ModelProfileDefinition:
        self._profiles[profile.id] = profile
        return profile


class InMemoryExecutionStore:
    def __init__(self):
        self.execution_repository = InMemoryExecutionRepository()
        self.event_repository = InMemoryExecutionEventRepository()
        self.artifact_repository = InMemoryExecutionArtifactRepository()
        self._executions = self.execution_repository._items
        self._events = self.event_repository._items
        self._artifacts = self.artifact_repository._items
        self._approval_requests: Dict[str, dict[str, Any]] = {}

    async def save_execution(self, execution: Execution) -> Execution:
        existing = await self.execution_repository.get_execution(execution.id)
        if existing is None:
            return await self.execution_repository.create_execution(execution)
        return await self.execution_repository.save_execution(execution)

    async def update_execution(self, execution: Execution) -> Execution:
        return await self.execution_repository.save_execution(execution)

    async def get_execution(self, execution_id: str) -> Optional[Execution]:
        return await self.execution_repository.get_execution(execution_id)

    async def list_executions(self) -> List[Execution]:
        return await self.execution_repository.list_executions()

    async def save_event(self, event: ExecutionEvent) -> ExecutionEvent:
        saved = await self.event_repository.append_event(event)
        execution = await self.execution_repository.get_execution(saved.execution_id)
        if execution is not None and apply_activity_to_execution(execution, saved):
            await self.execution_repository.save_execution(execution)
        return saved

    async def list_events(self, execution_id: str) -> List[ExecutionEvent]:
        return await self.event_repository.list_events(execution_id)

    async def list_events_after(self, execution_id: str, after_sequence: int) -> List[ExecutionEvent]:
        return await self.event_repository.list_events_after_sequence(execution_id, after_sequence)

    async def delete_event(self, event_id: str) -> bool:
        return await self.event_repository.delete_event(event_id)

    async def save_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact:
        return await self.artifact_repository.create_artifact(artifact)

    async def list_artifacts(self, execution_id: str) -> List[ExecutionArtifact]:
        return await self.artifact_repository.list_artifacts(execution_id)

    async def list_active_executions(self) -> List[Execution]:
        active = {"queued", "running", "waiting_for_approval", "paused", "cancelling"}
        return await self.execution_repository.list_executions(filters={"status_in": active})

    async def list_executions_by_workflow(self, workflow_id: str) -> List[Execution]:
        return await self.execution_repository.list_executions(filters={"workflow_id": workflow_id})

    async def list_executions_by_agent(self, agent_id: str) -> List[Execution]:
        return [
            execution
            for execution in await self.execution_repository.list_executions()
            if agent_id in (execution.metadata.get("agent_ids") or [])
        ]

    async def delete_execution(self, execution_id: str) -> bool:
        deleted = await self.execution_repository.delete_execution(execution_id)
        self._events.pop(execution_id, None)
        self._artifacts.pop(execution_id, None)
        self._approval_requests = {
            request_id: request
            for request_id, request in self._approval_requests.items()
            if request.get("execution_id") != execution_id
        }
        return deleted

    async def create_approval_request(self, *, execution_id: str, event_id: str | None, tool_id: str, status: str,
                                      payload: dict[str, Any]) -> str:
        request_id = f"{execution_id}:{tool_id}"[:64]
        self._approval_requests[request_id] = {
            "id": request_id,
            "execution_id": execution_id,
            "event_id": event_id,
            "tool_id": tool_id,
            "status": status,
            "request_payload": dict(payload or {}),
            "response_payload": {},
            "requested_at": utc_now().isoformat(),
            "responded_at": None,
            "responded_by": None,
        }
        return request_id

    async def update_approval_request(self, request_id: str, *, status: str, response_payload: dict[str, Any],
                                      responded_by: str | None = None) -> None:
        current = self._approval_requests.get(request_id)
        if current is None:
            return
        self._approval_requests[request_id] = {
            **current,
            "status": status,
            "response_payload": dict(response_payload or {}),
            "responded_at": utc_now().isoformat(),
            "responded_by": responded_by,
        }

    async def list_approval_requests(self, execution_id: str) -> list[dict[str, Any]]:
        items = [
            dict(item)
            for item in self._approval_requests.values()
            if item.get("execution_id") == execution_id
        ]
        return sorted(items, key=lambda item: (item.get("requested_at") or "", item.get("id") or ""))

    async def acquire_lock(self, execution_id: str, worker_id: str, stale_after_seconds: int = 30) -> bool:
        execution = self._executions.get(execution_id)
        if execution is None:
            return False
        now = utc_now()
        last_heartbeat = execution.last_heartbeat_at
        is_stale = last_heartbeat is None or (now - last_heartbeat).total_seconds() > stale_after_seconds
        if execution.worker_id is None or execution.worker_id == worker_id or is_stale:
            execution.worker_id = worker_id
            execution.last_heartbeat_at = now
            self._executions[execution_id] = execution
            return True
        return False

    async def release_lock(self, execution_id: str, worker_id: str) -> bool:
        execution = self._executions.get(execution_id)
        if execution is None or execution.worker_id != worker_id:
            return False
        execution.worker_id = None
        self._executions[execution_id] = execution
        return True

    async def heartbeat(self, execution_id: str, worker_id: str) -> Optional[Execution]:
        execution = self._executions.get(execution_id)
        if execution is None:
            return None
        if execution.worker_id not in {None, worker_id}:
            return execution
        execution.worker_id = worker_id
        execution.last_heartbeat_at = utc_now()
        self._executions[execution_id] = execution
        return execution


class MongoExecutionStore:
    def __init__(self):
        self.execution_repository = MongoExecutionRepository()
        self.event_repository = MongoExecutionEventRepository(self.execution_repository)
        self.artifact_repository = MongoExecutionArtifactRepository()
        self._db = self.execution_repository.collection.database
        self._executions = self.execution_repository.collection
        self._events = self.event_repository.collection
        self._artifacts = self.artifact_repository.collection

    async def save_execution(self, execution: Execution) -> Execution:
        existing = await self.execution_repository.get_execution(execution.id)
        if existing is None:
            return await self.execution_repository.create_execution(execution)
        return await self.execution_repository.save_execution(execution)

    async def update_execution(self, execution: Execution) -> Execution:
        return await self.execution_repository.save_execution(execution)

    async def get_execution(self, execution_id: str) -> Optional[Execution]:
        return await self.execution_repository.get_execution(execution_id)

    async def list_executions(self) -> List[Execution]:
        return await self.execution_repository.list_executions()

    async def save_event(self, event: ExecutionEvent) -> ExecutionEvent:
        return await self.event_repository.append_event(event)

    async def list_events(self, execution_id: str) -> List[ExecutionEvent]:
        return await self.event_repository.list_events(execution_id)

    async def list_events_after(self, execution_id: str, after_sequence: int) -> List[ExecutionEvent]:
        return await self.event_repository.list_events_after_sequence(execution_id, after_sequence)

    async def delete_event(self, event_id: str) -> bool:
        return await self.event_repository.delete_event(event_id)

    async def save_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact:
        return await self.artifact_repository.create_artifact(artifact)

    async def list_artifacts(self, execution_id: str) -> List[ExecutionArtifact]:
        return await self.artifact_repository.list_artifacts(execution_id)

    async def list_active_executions(self) -> List[Execution]:
        active = ["queued", "running", "waiting_for_approval", "paused", "cancelling"]
        return await self.execution_repository.list_executions(filters={"status_in": active})

    async def list_executions_by_workflow(self, workflow_id: str) -> List[Execution]:
        return await self.execution_repository.list_executions(filters={"workflow_id": workflow_id})

    async def list_executions_by_agent(self, agent_id: str) -> List[Execution]:
        cursor = self._executions.find({"metadata.agent_ids": agent_id})
        items: List[Execution] = []
        async for record in cursor:
            record.pop("_id", None)
            record.pop("_next_event_sequence", None)
            items.append(Execution.model_validate(record))
        return items

    async def delete_execution(self, execution_id: str) -> bool:
        deleted = await self.execution_repository.delete_execution(execution_id)
        await self._events.delete_many({"execution_id": execution_id})
        await self._artifacts.delete_many({"execution_id": execution_id})
        return deleted

    async def acquire_lock(self, execution_id: str, worker_id: str, stale_after_seconds: int = 30) -> bool:
        now = utc_now()
        current = await self.get_execution(execution_id)
        if current is None:
            return False
        is_stale = current.last_heartbeat_at is None or (
                now - current.last_heartbeat_at).total_seconds() > stale_after_seconds
        if current.worker_id not in {None, worker_id} and not is_stale:
            return False
        current.worker_id = worker_id
        current.last_heartbeat_at = now
        await self.update_execution(current)
        return True

    async def release_lock(self, execution_id: str, worker_id: str) -> bool:
        current = await self.get_execution(execution_id)
        if current is None or current.worker_id != worker_id:
            return False
        current.worker_id = None
        await self.update_execution(current)
        return True

    async def heartbeat(self, execution_id: str, worker_id: str) -> Optional[Execution]:
        current = await self.get_execution(execution_id)
        if current is None:
            return None
        if current.worker_id not in {None, worker_id}:
            return current
        current.worker_id = worker_id
        current.last_heartbeat_at = utc_now()
        await self.update_execution(current)
        return current


class SQLExecutionStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, graph_projection_event_repo=None):
        self.session_factory = session_factory
        self.graph_projection_event_repo = graph_projection_event_repo

    async def _append_execution_projection_event(self, event: ExecutionEvent, *,
                                                 execution: Execution | None = None) -> None:
        if not get_settings().graph_projection_enabled:
            return
        if self.graph_projection_event_repo is None or event.event_type not in GRAPH_PROJECTED_EXECUTION_EVENT_TYPES:
            return
        aggregate_type = "step_run" if event.task_id else "workflow_run"
        aggregate_id = f"{event.execution_id}:{event.task_id}" if event.task_id else event.execution_id
        execution_payload = _execution_projection_payload(execution) if execution is not None else {}
        try:
            await self.graph_projection_event_repo.append(
                GraphProjectionEvent(
                    event_type=event.event_type.value,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    occurred_at=event.timestamp,
                    payload={
                        **execution_payload,
                        "execution_id": event.execution_id,
                        "workflow_id": event.workflow_id,
                        "agent_id": event.agent_id,
                        "task_id": event.task_id,
                        "tool_call_id": event.tool_call_id,
                        "model_request_id": event.model_request_id,
                        "parent_event_id": event.parent_event_id,
                        "sequence": event.sequence,
                        "trace_id": event.trace_id,
                        "span_id": event.span_id,
                        "actor_type": event.actor_type,
                        "actor": event.actor,
                        "source": event.source,
                        "status": event.status,
                        "payload": event.payload,
                        "metrics": event.metrics,
                        "metadata": event.metadata,
                    },
                    source="execution_events",
                    source_event_id=event.id,
                )
            )
        except Exception:
            logger.exception("Failed to append execution graph projection event")

    async def save_execution(self, execution: Execution) -> Execution:
        async with self.session_factory() as session:
            existing = await session.get(ExecutionORM, execution.id)
            if existing is None:
                entity = _execution_to_orm(execution)
                session.add(entity)
            else:
                entity = existing
                source = _execution_to_orm(execution)
                for field_name in (
                        "workflow_id",
                        "goal_id",
                        "workflow_version_id",
                        "status",
                        "runtime_adapter",
                        "runtime_revision_id",
                        "runtime_fingerprint",
                        "trigger_type",
                        "trigger_payload_json",
                        "input_json",
                        "output_json",
                        "error_json",
                        "created_by",
                        "worker_id",
                        "created_at",
                        "started_at",
                        "ended_at",
                        "updated_at",
                        "last_heartbeat_at",
                        "container_id",
                        "container_name",
                        "container_image",
                        "container_status",
                        "container_started_at",
                        "container_ended_at",
                        "container_exit_code",
                        "replacement_of_execution_id",
                        "restart_reason",
                        "metadata_json",
                ):
                    setattr(entity, field_name, getattr(source, field_name))
            await session.commit()
            return _execution_from_orm(entity)

    async def update_execution(self, execution: Execution) -> Execution:
        return await self.save_execution(execution)

    async def get_execution(self, execution_id: str) -> Optional[Execution]:
        async with self.session_factory() as session:
            entity = await session.get(ExecutionORM, execution_id)
            return None if entity is None else _execution_from_orm(entity)

    async def list_executions(self) -> List[Execution]:
        async with self.session_factory() as session:
            result = await session.execute(select(ExecutionORM).order_by(ExecutionORM.created_at.desc()))
            return [_execution_from_orm(item) for item in result.scalars().all()]

    async def save_event(self, event: ExecutionEvent) -> ExecutionEvent:
        async with self.session_factory() as session:
            async with session.begin():
                execution = await session.get(ExecutionORM, event.execution_id, with_for_update=True)
                if execution is None:
                    raise ValueError(f"Execution '{event.execution_id}' was not found for event append")

                result = await session.execute(
                    select(func.max(ExecutionEventORM.sequence)).where(
                        ExecutionEventORM.execution_id == event.execution_id
                    )
                )
                event.sequence = int(result.scalar_one_or_none() or 0) + 1

                entity = _event_to_orm(event)
                session.add(entity)
                await session.flush()
                saved = _event_from_orm(entity)
                activity_execution = _execution_from_orm(execution)
                if apply_activity_to_execution(activity_execution, saved):
                    execution.trigger_payload_json = _serialize_execution_trigger(
                        activity_execution.trigger_payload,
                        activity_execution.metadata,
                    )
                    execution.metadata_json = activity_execution.metadata
                    execution.updated_at = activity_execution.updated_at
            await self._append_execution_projection_event(saved, execution=_execution_from_orm(execution))
            return saved

    async def list_events(self, execution_id: str) -> List[ExecutionEvent]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ExecutionEventORM)
                .where(ExecutionEventORM.execution_id == execution_id)
                .order_by(ExecutionEventORM.sequence.asc())
            )
            return [_event_from_orm(item) for item in result.scalars().all()]

    async def list_events_after(self, execution_id: str, after_sequence: int) -> List[ExecutionEvent]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ExecutionEventORM)
                .where(
                    ExecutionEventORM.execution_id == execution_id,
                    ExecutionEventORM.sequence > after_sequence,
                )
                .order_by(ExecutionEventORM.sequence.asc())
            )
            return [_event_from_orm(item) for item in result.scalars().all()]

    async def delete_event(self, event_id: str) -> bool:
        async with self.session_factory() as session:
            entity = await session.get(ExecutionEventORM, event_id)
            if entity is None:
                return False
            await session.delete(entity)
            await session.commit()
            return True

    async def save_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact:
        async with self.session_factory() as session:
            entity = _artifact_to_orm(artifact)
            session.add(entity)
            await session.commit()
            return _artifact_from_orm(entity)

    async def list_artifacts(self, execution_id: str) -> List[ExecutionArtifact]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ExecutionArtifactORM)
                .where(ExecutionArtifactORM.execution_id == execution_id)
                .order_by(ExecutionArtifactORM.created_at.asc())
            )
            return [_artifact_from_orm(item) for item in result.scalars().all()]

    async def list_active_executions(self) -> List[Execution]:
        active = {"queued", "running", "waiting_for_approval", "paused", "cancelling"}
        async with self.session_factory() as session:
            result = await session.execute(
                select(ExecutionORM).where(ExecutionORM.status.in_(active)).order_by(ExecutionORM.created_at.desc())
            )
            return [_execution_from_orm(item) for item in result.scalars().all()]

    async def list_executions_by_workflow(self, workflow_id: str) -> List[Execution]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ExecutionORM).where(ExecutionORM.workflow_id == workflow_id).order_by(
                    ExecutionORM.created_at.desc())
            )
            return [_execution_from_orm(item) for item in result.scalars().all()]

    async def list_executions_by_agent(self, agent_id: str) -> List[Execution]:
        executions = await self.list_executions()
        return [execution for execution in executions if agent_id in (execution.metadata.get("agent_ids") or [])]

    async def delete_execution(self, execution_id: str) -> bool:
        deleted_execution: Execution | None = None
        async with self.session_factory() as session:
            entity = await session.get(ExecutionORM, execution_id)
            if entity is None:
                return False
            deleted_execution = _execution_from_orm(entity)
            await session.delete(entity)
            await session.commit()
        await self._append_execution_deleted_projection_event(deleted_execution)
        return True

    async def _append_execution_deleted_projection_event(self, execution: Execution | None) -> None:
        if execution is None:
            return
        if not get_settings().graph_projection_enabled or self.graph_projection_event_repo is None:
            return
        try:
            await self.graph_projection_event_repo.append(
                GraphProjectionEvent(
                    event_type="execution.deleted",
                    aggregate_type="workflow_run",
                    aggregate_id=execution.id,
                    payload={
                        **_execution_projection_payload(execution),
                        "execution_id": execution.id,
                        "workflow_id": execution.workflow_id,
                        "status": "deleted",
                    },
                    source="execution_retention",
                    source_event_id=f"execution:{execution.id}:deleted",
                )
            )
        except Exception:
            logger.exception("Failed to append execution deletion graph projection event")

    async def acquire_lock(self, execution_id: str, worker_id: str, stale_after_seconds: int = 30) -> bool:
        current = await self.get_execution(execution_id)
        if current is None:
            return False
        now = utc_now()
        is_stale = current.last_heartbeat_at is None or (
                now - current.last_heartbeat_at).total_seconds() > stale_after_seconds
        if current.worker_id not in {None, worker_id} and not is_stale:
            return False
        current.worker_id = worker_id
        current.last_heartbeat_at = now
        await self.update_execution(current)
        return True

    async def release_lock(self, execution_id: str, worker_id: str) -> bool:
        current = await self.get_execution(execution_id)
        if current is None or current.worker_id != worker_id:
            return False
        current.worker_id = None
        await self.update_execution(current)
        return True

    async def heartbeat(self, execution_id: str, worker_id: str) -> Optional[Execution]:
        current = await self.get_execution(execution_id)
        if current is None:
            return None
        if current.worker_id not in {None, worker_id}:
            return current
        current.worker_id = worker_id
        current.last_heartbeat_at = utc_now()
        await self.update_execution(current)
        return current

    async def create_approval_request(self, *, execution_id: str, event_id: str | None, tool_id: str, status: str,
                                      payload: dict[str, Any]) -> str:
        request_id = f"{execution_id}:{tool_id}"
        async with self.session_factory() as session:
            entity = ApprovalRequestORM(
                id=request_id[:64],
                execution_id=execution_id,
                event_id=event_id,
                tool_id=tool_id,
                status=status,
                request_payload_json=payload,
            )
            session.merge(entity)
            await session.commit()
        return request_id[:64]

    async def update_approval_request(self, request_id: str, *, status: str, response_payload: dict[str, Any],
                                      responded_by: str | None = None) -> None:
        async with self.session_factory() as session:
            entity = await session.get(ApprovalRequestORM, request_id)
            if entity is None:
                return
            entity.status = status
            entity.response_payload_json = response_payload
            entity.responded_by = responded_by
            entity.responded_at = utc_now()
            await session.commit()

    async def list_approval_requests(self, execution_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ApprovalRequestORM)
                .where(ApprovalRequestORM.execution_id == execution_id)
                .order_by(ApprovalRequestORM.requested_at.asc(), ApprovalRequestORM.id.asc())
            )
            return [_approval_request_from_orm(item) for item in result.scalars().all()]

    async def create_tool_invocation(self, *, invocation_id: str, execution_id: str, tool_id: str, event_id: str | None,
                                     input_json: dict[str, Any]) -> None:
        async with self.session_factory() as session:
            entity = ToolInvocationORM(
                id=invocation_id[:64],
                execution_id=execution_id,
                tool_id=tool_id,
                event_id=event_id,
                status="running",
                input_json=input_json,
                started_at=utc_now(),
            )
            session.add(entity)
            await session.commit()

    async def update_tool_invocation(
            self,
            invocation_id: str,
            *,
            status: str,
            output_json: dict[str, Any] | None = None,
            error_json: dict[str, Any] | None = None,
            latency_ms: int | None = None,
    ) -> None:
        async with self.session_factory() as session:
            entity = await session.get(ToolInvocationORM, invocation_id[:64])
            if entity is None:
                return
            entity.status = status
            entity.output_json = output_json
            entity.error_json = error_json
            entity.ended_at = utc_now()
            entity.latency_ms = latency_ms
            await session.commit()

    async def list_all_executions(self) -> List[Execution]:
        return await self.list_executions()

    async def list_all_events(self) -> List[ExecutionEvent]:
        async with self.session_factory() as session:
            result = await session.execute(select(ExecutionEventORM).order_by(ExecutionEventORM.timestamp.asc()))
            return [_event_from_orm(item) for item in result.scalars().all()]
