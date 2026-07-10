"""Graph projection parity diagnostics between the outbox and Neo4j."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.domain import GraphProjectionEvent
from app.graph.neo4j_projection import NEO4J_PROJECTED_LABELS, Neo4jProjectionConfig

OUTBOX_AGGREGATE_LABELS = {
    "workflow": "Workflow",
    "workflow_run": "WorkflowRun",
    "step_run": "StepRun",
    "memory": "Memory",
    "document_memory_collection": "Document",
    "persona": "Persona",
}

OUTBOX_RELATIONSHIP_TYPES = {
    "workflow_memory_link": "LINKS_MEMORY",
}

PROJECTED_RELATIONSHIP_TYPES = (
    "ASSIGNED_TO",
    "APPROVES",
    "AVAILABLE_TO",
    "CALLED_TOOL",
    "CAN_HANDOFF_TO",
    "CAN_USE",
    "CREATED_CONTAINER",
    "CREATED_MEMORY",
    "DEFINES_AGENT",
    "DEFINES_TASK",
    "DEFINES_TOOL",
    "DERIVED_FROM",
    "DEPENDS_ON",
    "EMITTED_EVENT",
    "ESCALATES_TO",
    "FAILED_WITH",
    "FOLLOWS",
    "FOLLOWED_BY",
    "HAS_BUDGET_SIGNAL",
    "HAS_CHUNK",
    "HAS_COMPACTION",
    "HAS_CONTEXT_HEALTH",
    "HAS_CONTEXT_PACK",
    "HAS_MEMORY_LINK",
    "HAS_RUN",
    "HAS_STEP_RUN",
    "HAS_VERSION",
    "LINKS_MEMORY",
    "KNOWS",
    "MENTIONS",
    "OCCURRED_IN",
    "PARENT_OF",
    "PARTICIPATED_IN",
    "PARTICIPATES_IN",
    "PART_OF_DOCUMENT",
    "ITEM_DERIVED_FROM_MEMORY",
    "PERSONA_FOLLOWS_WORKFLOW",
    "PERSONA_HAS_DISTILLATION_RUN",
    "PERSONA_HAS_VERSION",
    "PERSONA_INVOKED_IN_CONVERSATION",
    "PERSONA_MATERIALIZED_AS_AGENT",
    "PERSONA_PRODUCES_ARTIFACT",
    "PERSONA_PUBLISHED_MEMORY",
    "PERSONA_USES_TOOL",
    "PRODUCED_ARTIFACT",
    "PRODUCES",
    "RAISED_FINDING",
    "RECORDED_CONTEXT_HEALTH",
    "RECORDED_USAGE",
    "RUN_EXTRACTED_ITEM",
    "RUN_PRODUCED_VERSION",
    "RUN_USED_SOURCE_MEMORY",
    "SOURCE_CONVERSATION",
    "SOURCE_DOCUMENT",
    "SOURCE_EXECUTION",
    "STARTED",
    "SUPERSEDES",
    "SUMMARIZES",
    "TRIGGERED",
    "RELATES_TO",
    "REVIEWS",
    "USED_MODEL",
    "USED_PROVIDER",
    "USED_RUNTIME",
    "USED_WORKFLOW_VERSION",
    "USES_MODEL_PROFILE",
    "USES",
    "USES_TOOL",
)

SOURCE_INTELLIGENCE_GRAPH_ENTITY_LABELS = {
    "Person",
    "Knowledge",
    "Tool",
    "Workflow",
    "Artifact",
    "Decision",
    "Event",
    "Organization",
    "Persona",
}

SOURCE_INTELLIGENCE_GRAPH_RELATIONSHIP_TYPES = {
    "KNOWS",
    "USES",
    "FOLLOWS",
    "PRODUCES",
    "REVIEWS",
    "APPROVES",
    "ESCALATES_TO",
    "PARTICIPATES_IN",
    "DERIVED_FROM",
    "RELATES_TO",
}


@dataclass(slots=True)
class GraphParityItem:
    kind: str
    name: str
    expected: int
    actual: int

    @property
    def delta(self) -> int:
        return self.actual - self.expected

    @property
    def ok(self) -> bool:
        return self.delta == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "expected": self.expected,
            "actual": self.actual,
            "delta": self.delta,
            "ok": self.ok,
        }


@dataclass(slots=True)
class GraphParityResult:
    ok: bool
    checked_events: int
    truncated: bool
    outbox_status: dict[str, Any]
    node_counts_by_type: dict[str, int] = field(default_factory=dict)
    edge_counts_by_type: dict[str, int] = field(default_factory=dict)
    items: list[GraphParityItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_events": self.checked_events,
            "truncated": self.truncated,
            "outbox_status": self.outbox_status,
            "node_counts_by_type": self.node_counts_by_type,
            "edge_counts_by_type": self.edge_counts_by_type,
            "items": [item.to_dict() for item in self.items],
            "errors": self.errors,
        }


class Neo4jGraphParityChecker:
    """Compare expected projected graph counts against Neo4j active counts."""

    def __init__(self, driver, *, config: Neo4jProjectionConfig | None = None):
        self.driver = driver
        self.config = config or Neo4jProjectionConfig()

    async def close(self) -> None:
        close = getattr(self.driver, "close", None)
        if close is not None:
            await close()

    async def check(self, event_repository, *, event_limit: int = 10000) -> GraphParityResult:
        expected, checked_events, truncated = await _expected_counts_from_outbox(event_repository, limit=event_limit)
        actual = await self._actual_counts()
        node_counts, edge_counts = await self.projected_graph_counts()
        names = sorted(set(expected) | set(actual), key=lambda item: (item[0], item[1]))
        items = [
            GraphParityItem(
                kind=kind,
                name=name,
                expected=expected.get((kind, name), 0),
                actual=actual.get((kind, name), 0),
            )
            for kind, name in names
        ]
        status_summary = await event_repository.status_summary()
        errors: list[str] = []
        if truncated:
            errors.append(f"Outbox scan reached event_limit={event_limit}; parity may be incomplete.")
        if int(status_summary.get("failed_count", 0) or 0) > 0:
            errors.append("Outbox has failed projection events.")
        if int(status_summary.get("pending_count", 0) or 0) > 0:
            errors.append("Outbox has pending projection events.")
        return GraphParityResult(
            ok=all(item.ok for item in items) and not errors,
            checked_events=checked_events,
            truncated=truncated,
            outbox_status=status_summary,
            node_counts_by_type=node_counts,
            edge_counts_by_type=edge_counts,
            items=items,
            errors=errors,
        )

    async def projected_graph_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        node_counts: dict[str, int] = {}
        edge_counts: dict[str, int] = {}
        for label in sorted(set(NEO4J_PROJECTED_LABELS)):
            node_counts[label] = await self._count_node_label(label)
        for relationship_type in sorted(set(PROJECTED_RELATIONSHIP_TYPES)):
            edge_counts[relationship_type] = await self._count_relationship_type(relationship_type)
        return node_counts, edge_counts

    async def _actual_counts(self) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        for label in sorted(set(NEO4J_PROJECTED_LABELS)):
            counts[("node", label)] = await self._count_node_label(label)
        for relationship_type in sorted(set(PROJECTED_RELATIONSHIP_TYPES)):
            counts[("relationship", relationship_type)] = await self._count_relationship_type(relationship_type)
        return counts

    async def _count_node_label(self, label: str) -> int:
        return await self._count(
            f"""
            MATCH (n:`{_safe_identifier(label)}`)
            WHERE coalesce(n.deleted, false) = false
            RETURN count(n) AS count
            """,
            {},
        )

    async def _count_relationship_type(self, relationship_type: str) -> int:
        return await self._count(
            f"""
            MATCH ()-[r:`{_safe_identifier(relationship_type)}`]->()
            WHERE coalesce(r.deleted, false) = false
            RETURN count(r) AS count
            """,
            {},
        )

    async def _count(self, cypher: str, params: dict[str, Any]) -> int:
        session_kwargs = {}
        if self.config.database:
            session_kwargs["database"] = self.config.database
        async with self.driver.session(**session_kwargs) as session:
            result = await session.run(cypher, **params)
            record = await _first_record(result)
            if record is None:
                return 0
            value = _record_get(record, "count")
            return int(value or 0)


async def _expected_counts_from_outbox(event_repository, *, limit: int) -> tuple[dict[tuple[str, str], int], int, bool]:
    events = await event_repository.list_events(status="projected", limit=limit + 1)
    truncated = len(events) > limit
    active_nodes: dict[tuple[str, str], bool] = {}
    active_relationships: dict[tuple[str, str], bool] = {}
    active_event_sequences: dict[tuple[str, int], str] = {}
    for event in events[:limit]:
        _apply_event_to_expected_counts(event, active_nodes=active_nodes, active_relationships=active_relationships)
        _apply_event_sequence_expected(event, active_event_sequences=active_event_sequences)
    _apply_followed_by_expected(active_event_sequences, active_relationships)
    counts: dict[tuple[str, str], int] = {}
    for (label, _aggregate_id), active in active_nodes.items():
        if active:
            counts[("node", label)] = counts.get(("node", label), 0) + 1
    for (relationship_type, _aggregate_id), active in active_relationships.items():
        if active:
            counts[("relationship", relationship_type)] = counts.get(("relationship", relationship_type), 0) + 1
    return counts, min(len(events), limit), truncated


def _apply_event_to_expected_counts(
        event: GraphProjectionEvent,
        *,
        active_nodes: dict[tuple[str, str], bool],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    active = not event.event_type.endswith(".deleted")
    is_step_run_projection = event.event_type in {"task.started", "agent.step.completed", "agent.step.failed"} or (
            event.aggregate_type == "step_run"
            and event.event_type in {"execution.started", "execution.completed", "execution.failed"}
    )
    if event.aggregate_type in OUTBOX_AGGREGATE_LABELS and (
            event.aggregate_type != "step_run" or is_step_run_projection
    ):
        _set_node(active_nodes, OUTBOX_AGGREGATE_LABELS[event.aggregate_type], event.aggregate_id, active=active)
    if event.aggregate_type in OUTBOX_RELATIONSHIP_TYPES:
        _set_relationship(
            active_relationships,
            OUTBOX_RELATIONSHIP_TYPES[event.aggregate_type],
            event.aggregate_id,
            active=active,
        )
    if not active:
        if event.event_type.startswith("workflow."):
            _deactivate_workflow_relationships(
                active_relationships,
                _string(payload.get("workflow_id") or event.aggregate_id),
            )
        if event.event_type.startswith("memory."):
            _deactivate_memory_relationships(
                active_relationships,
                _string(payload.get("memory_id") or event.aggregate_id),
            )
        return
    if event.event_type.startswith("workflow."):
        _deactivate_workflow_relationships(
            active_relationships,
            _string(payload.get("workflow_id") or event.aggregate_id),
            preserve_run_relationships=True,
        )
        _apply_workflow_definition_expected(event, payload, active_nodes, active_relationships)
    if event.event_type.startswith("memory."):
        _apply_memory_expected(event, payload, active_nodes, active_relationships)
    if event.event_type.startswith("document_memory_collection."):
        _apply_document_expected(event, payload, active_nodes, active_relationships)
    if event.event_type.startswith("workflow_memory_link."):
        _apply_workflow_memory_link_expected(event, payload, active_nodes, active_relationships)
    if event.aggregate_type == "persona" or event.event_type.startswith("persona."):
        _apply_persona_expected(event, payload, active_nodes, active_relationships)
    if is_step_run_projection:
        _apply_step_run_expected(event, payload, active_nodes, active_relationships)
        return
    if event.event_type.startswith("execution."):
        _apply_workflow_run_expected(event, payload, active_nodes, active_relationships)
    if _is_execution_detail_event(event.event_type):
        _apply_execution_detail_expected(event, payload, active_nodes, active_relationships)


def _apply_workflow_definition_expected(
        event: GraphProjectionEvent,
        payload: dict[str, Any],
        active_nodes: dict[tuple[str, str], bool],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    workflow_id = _string(payload.get("workflow_id") or event.aggregate_id)
    if not workflow_id:
        return
    _set_node(active_nodes, "Workflow", workflow_id)
    workflow_version_id = _workflow_version_id(payload, workflow_id)
    if workflow_version_id:
        _set_node(active_nodes, "WorkflowVersion", workflow_version_id)
        _set_edge(active_relationships, "HAS_VERSION", workflow_id, workflow_version_id)
    for tool in _dict_list(payload.get("tools")):
        tool_id = _string(tool.get("id"))
        if not tool_id:
            continue
        _set_node(active_nodes, "Tool", tool_id)
        _set_edge(active_relationships, "DEFINES_TOOL", workflow_id, tool_id)
    for agent in _dict_list(payload.get("agents")):
        agent_id = _string(agent.get("id"))
        if not agent_id:
            continue
        _set_node(active_nodes, "Agent", agent_id)
        _set_edge(active_relationships, "DEFINES_AGENT", workflow_id, agent_id)
        for tool_id in _string_list(agent.get("tool_ids")):
            _set_node(active_nodes, "Tool", tool_id)
            _set_edge(active_relationships, "CAN_USE", agent_id, tool_id, workflow_id)
        for handoff_agent_id in _string_list(agent.get("handoff_agent_ids")):
            _set_node(active_nodes, "Agent", handoff_agent_id)
            _set_edge(active_relationships, "CAN_HANDOFF_TO", agent_id, handoff_agent_id, workflow_id)
        model_profile_id = _string(agent.get("model_profile_id"))
        if model_profile_id:
            _set_node(active_nodes, "Model", model_profile_id)
            _set_edge(active_relationships, "USED_MODEL", agent_id, model_profile_id, workflow_id)
            _set_edge(active_relationships, "USES_MODEL_PROFILE", agent_id, model_profile_id, workflow_id)
    for task in _dict_list(payload.get("tasks")):
        task_id = _string(task.get("id"))
        if not task_id:
            continue
        _set_node(active_nodes, "Task", task_id)
        _set_edge(active_relationships, "DEFINES_TASK", workflow_id, task_id)
        agent_id = _string(task.get("agent_id"))
        if agent_id:
            _set_node(active_nodes, "Agent", agent_id)
            _set_edge(active_relationships, "ASSIGNED_TO", task_id, agent_id, workflow_id)
        for tool_id in _string_list(task.get("tool_ids")):
            _set_node(active_nodes, "Tool", tool_id)
            _set_edge(active_relationships, "USES_TOOL", task_id, tool_id, workflow_id)
        for dependency_id in _string_list(task.get("depends_on_task_ids")):
            _set_node(active_nodes, "Task", dependency_id)
            _set_edge(active_relationships, "DEPENDS_ON", task_id, dependency_id, workflow_id)


def _apply_persona_expected(
        event: GraphProjectionEvent,
        payload: dict[str, Any],
        active_nodes: dict[tuple[str, str], bool],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    persona_id = _string(payload.get("persona_id") or event.aggregate_id)
    if not persona_id:
        return
    _set_node(active_nodes, "Persona", persona_id)
    run_id = _string(payload.get("run_id"))
    if run_id:
        _set_node(active_nodes, "DistillationRun", run_id)
        _set_edge(active_relationships, "PERSONA_HAS_DISTILLATION_RUN", persona_id, run_id)
    version_id = _string(payload.get("persona_version_id"))
    if version_id:
        _set_node(active_nodes, "PersonaVersion", version_id)
        _set_edge(active_relationships, "PERSONA_HAS_VERSION", persona_id, version_id)
        if run_id:
            _set_edge(active_relationships, "RUN_PRODUCED_VERSION", run_id, version_id)
    item_id = _string(payload.get("item_id"))
    if item_id:
        _set_node(active_nodes, "DistillationItem", item_id)
        if run_id:
            _set_edge(active_relationships, "RUN_EXTRACTED_ITEM", run_id, item_id)
        source_memory_id = _string(payload.get("source_memory_id"))
        if source_memory_id:
            _set_node(active_nodes, "SourceMemory", source_memory_id)
            _set_edge(active_relationships, "ITEM_DERIVED_FROM_MEMORY", item_id, source_memory_id)
    for source_memory_id in _string_list(payload.get("source_memory_ids")):
        _set_node(active_nodes, "SourceMemory", source_memory_id)
        if run_id:
            _set_edge(active_relationships, "RUN_USED_SOURCE_MEMORY", run_id, source_memory_id)
    for memory_id in _string_list(payload.get("memory_ids")):
        _set_node(active_nodes, "Memory", memory_id)
        _set_edge(active_relationships, "PERSONA_PUBLISHED_MEMORY", persona_id, memory_id)
    for tool in _dict_list(payload.get("tools")):
        tool_id = _first_string(tool.get("id"), tool.get("tool_id"), tool.get("name"))
        if tool_id:
            _set_node(active_nodes, "Tool", tool_id)
            _set_edge(active_relationships, "PERSONA_USES_TOOL", persona_id, tool_id)
    for workflow in _dict_list(payload.get("workflows")):
        workflow_id = _first_string(workflow.get("id"), workflow.get("workflow_id"), workflow.get("name"))
        if workflow_id:
            _set_node(active_nodes, "Workflow", workflow_id)
            _set_edge(active_relationships, "PERSONA_FOLLOWS_WORKFLOW", persona_id, workflow_id)
    for artifact in _dict_list(payload.get("artifacts")):
        artifact_id = _first_string(artifact.get("id"), artifact.get("artifact_id"), artifact.get("name"))
        if artifact_id:
            _set_node(active_nodes, "Artifact", artifact_id)
            _set_edge(active_relationships, "PERSONA_PRODUCES_ARTIFACT", persona_id, artifact_id)
    agent_id = _string(payload.get("agent_id"))
    if agent_id:
        _set_node(active_nodes, "Agent", agent_id)
        _set_edge(active_relationships, "PERSONA_MATERIALIZED_AS_AGENT", persona_id, agent_id)
    conversation_id = _string(payload.get("conversation_id"))
    if conversation_id:
        _set_node(active_nodes, "Conversation", conversation_id)
        _set_edge(active_relationships, "PERSONA_INVOKED_IN_CONVERSATION", persona_id, conversation_id)


def _apply_workflow_run_expected(
        event: GraphProjectionEvent,
        payload: dict[str, Any],
        active_nodes: dict[tuple[str, str], bool],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    execution_id = _string(payload.get("execution_id") or event.aggregate_id)
    workflow_id = _string(payload.get("workflow_id"))
    if execution_id:
        _set_node(active_nodes, "WorkflowRun", execution_id)
    if workflow_id and execution_id:
        _set_node(active_nodes, "Workflow", workflow_id)
        _set_edge(active_relationships, "HAS_RUN", workflow_id, execution_id)
        _set_edge(active_relationships, "STARTED", workflow_id, execution_id)
    workflow_version_id = _string(payload.get("workflow_version_id"))
    if execution_id and workflow_version_id:
        _set_node(active_nodes, "WorkflowVersion", workflow_version_id)
        if workflow_id:
            _set_edge(active_relationships, "HAS_VERSION", workflow_id, workflow_version_id)
        _set_edge(active_relationships, "USED_WORKFLOW_VERSION", execution_id, workflow_version_id)
    schedule_id = _string(
        _nested(payload, "trigger_payload").get("schedule_id") or _nested(payload, "trigger_payload").get("scheduleId"))
    if schedule_id and execution_id:
        _set_node(active_nodes, "Schedule", schedule_id)
        _set_edge(active_relationships, "TRIGGERED", schedule_id, execution_id)
    runtime_revision_id = _string(
        payload.get("runtime_revision_id") or _nested(payload, "payload").get("runtime_revision_id"))
    if runtime_revision_id and execution_id:
        _set_node(active_nodes, "RuntimeRevision", runtime_revision_id)
        _set_edge(active_relationships, "USED_RUNTIME", execution_id, runtime_revision_id)
    container_id = _container_source_id(event, payload)
    if container_id and execution_id:
        _set_node(active_nodes, "RuntimeContainer", container_id)
        _set_edge(active_relationships, "CREATED_CONTAINER", execution_id, container_id)
    if _error_message(event, payload) and execution_id:
        error_id = f"error:{_event_node_id(event, execution_id, payload)}"
        _set_node(active_nodes, "Error", error_id)
        _set_edge(active_relationships, "FAILED_WITH", execution_id, error_id)


def _apply_step_run_expected(
        event: GraphProjectionEvent,
        payload: dict[str, Any],
        active_nodes: dict[tuple[str, str], bool],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    step_run_id = _string(event.aggregate_id)
    execution_id = _string(payload.get("execution_id"))
    task_id = _string(payload.get("task_id") or step_run_id)
    agent_id = _string(payload.get("agent_id"))
    if step_run_id:
        _set_node(active_nodes, "StepRun", step_run_id)
    if execution_id and step_run_id:
        _set_node(active_nodes, "WorkflowRun", execution_id)
        _set_edge(active_relationships, "HAS_STEP_RUN", execution_id, step_run_id)
    if task_id and execution_id:
        _set_node(active_nodes, "Task", task_id)
        _set_edge(active_relationships, "OCCURRED_IN", task_id, execution_id)
    if agent_id and execution_id:
        _set_node(active_nodes, "Agent", agent_id)
        _set_edge(active_relationships, "PARTICIPATED_IN", agent_id, execution_id)
        if step_run_id:
            _set_edge(active_relationships, "ASSIGNED_TO", step_run_id, agent_id)
    if _error_message(event, payload) and step_run_id:
        error_id = f"error:{_event_node_id(event, execution_id or step_run_id, payload)}"
        _set_node(active_nodes, "Error", error_id)
        _set_edge(active_relationships, "FAILED_WITH", step_run_id, error_id)


def _apply_execution_detail_expected(
        event: GraphProjectionEvent,
        payload: dict[str, Any],
        active_nodes: dict[tuple[str, str], bool],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    execution_id = _string(payload.get("execution_id") or event.aggregate_id)
    event_id = _event_node_id(event, execution_id, payload)
    event_label = "ContainerEvent" if event.event_type.startswith("container.") else "ExecutionEvent"
    if execution_id:
        _set_node(active_nodes, "WorkflowRun", execution_id)
    if event_id:
        _set_node(active_nodes, event_label, event_id)
    if execution_id and event_id:
        _set_edge(active_relationships, "EMITTED_EVENT", execution_id, event_id)
    parent_event_id = _string(payload.get("parent_event_id"))
    if parent_event_id and event_id:
        _set_node(active_nodes, "ExecutionEvent", parent_event_id)
        _set_edge(active_relationships, "PARENT_OF", parent_event_id, event_id)
    agent_id = _string(payload.get("agent_id"))
    if agent_id and execution_id:
        _set_node(active_nodes, "Agent", agent_id)
        _set_edge(active_relationships, "PARTICIPATED_IN", agent_id, execution_id)
        if event_id:
            _set_edge(active_relationships, "EMITTED_EVENT", agent_id, event_id)
    task_id = _string(payload.get("task_id"))
    if task_id and execution_id:
        _set_node(active_nodes, "Task", task_id)
        _set_edge(active_relationships, "OCCURRED_IN", task_id, execution_id)
    event_payload = _nested(payload, "payload")
    tool_call_id = _string(
        payload.get("tool_call_id") or event_payload.get("tool_call_id") or event_payload.get("toolCallId"))
    if tool_call_id and execution_id:
        _set_node(active_nodes, "ToolCall", tool_call_id)
        _set_edge(active_relationships, "OCCURRED_IN", tool_call_id, execution_id)
        if event_id:
            _set_edge(active_relationships, "CALLED_TOOL", event_id, tool_call_id)
    model_request_id = _string(
        payload.get("model_request_id") or event_payload.get("model_request_id") or event_payload.get("modelRequestId")
    )
    if model_request_id and execution_id:
        _set_node(active_nodes, "ModelRequest", model_request_id)
        _set_edge(active_relationships, "OCCURRED_IN", model_request_id, execution_id)
    provider = _string(event_payload.get("provider") or event_payload.get("model_provider"))
    model_name = _string(event_payload.get("model") or event_payload.get("model_name"))
    model_id = _model_id(provider, model_name)
    if model_id:
        _set_node(active_nodes, "Model", model_id)
        if execution_id:
            _set_edge(active_relationships, "USED_MODEL", execution_id, model_id)
        if model_request_id:
            _set_edge(active_relationships, "USED_MODEL", model_request_id, model_id)
    if provider:
        _set_node(active_nodes, "ModelProvider", provider)
        if model_id:
            _set_edge(active_relationships, "USED_PROVIDER", model_id, provider)
    artifact_id = _string(
        event_payload.get("artifact_id")
        or event_payload.get("artifactId")
        or (event_payload.get("id") if "artifact" in event.event_type else None)
    )
    if artifact_id and execution_id:
        _set_node(active_nodes, "Artifact", artifact_id)
        _set_edge(active_relationships, "PRODUCED_ARTIFACT", execution_id, artifact_id)
    runtime_revision_id = _string(payload.get("runtime_revision_id") or event_payload.get("runtime_revision_id"))
    if runtime_revision_id and execution_id:
        _set_node(active_nodes, "RuntimeRevision", runtime_revision_id)
        _set_edge(active_relationships, "USED_RUNTIME", execution_id, runtime_revision_id)
    container_id = _container_source_id(event, payload)
    if container_id and execution_id:
        _set_node(active_nodes, "RuntimeContainer", container_id)
        _set_edge(active_relationships, "CREATED_CONTAINER", execution_id, container_id)
    if _error_message(event, payload) and event_id:
        error_id = f"error:{event_id}"
        _set_node(active_nodes, "Error", error_id)
        _set_edge(active_relationships, "FAILED_WITH", event_id, error_id)
        if execution_id:
            _set_edge(active_relationships, "FAILED_WITH", execution_id, error_id)
    _apply_observability_expected(event, payload, event_id, execution_id, active_nodes, active_relationships)


def _apply_observability_expected(
        event: GraphProjectionEvent,
        payload: dict[str, Any],
        event_id: str,
        execution_id: str,
        active_nodes: dict[tuple[str, str], bool],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    event_payload = _nested(payload, "payload")
    if event.event_type == "context.health.recorded":
        context_id = f"context_health:{event_id}"
        _set_node(active_nodes, "ContextHealth", context_id)
        if execution_id:
            _set_edge(active_relationships, "HAS_CONTEXT_HEALTH", execution_id, context_id)
        _set_edge(active_relationships, "RECORDED_CONTEXT_HEALTH", event_id, context_id)
    if event.event_type == "token.usage.recorded":
        usage_id = f"token_usage:{event_id}"
        _set_node(active_nodes, "TokenUsage", usage_id)
        if execution_id:
            _set_edge(active_relationships, "RECORDED_USAGE", execution_id, usage_id)
        _set_edge(active_relationships, "RECORDED_USAGE", event_id, usage_id)
        model_request_id = _string(payload.get("model_request_id") or event_payload.get("model_request_id"))
        if model_request_id:
            _set_node(active_nodes, "ModelRequest", model_request_id)
            _set_edge(active_relationships, "RECORDED_USAGE", model_request_id, usage_id)
    if event.event_type.startswith("token.budget."):
        budget_id = f"token_budget:{event_id}"
        _set_node(active_nodes, "TokenBudget", budget_id)
        if execution_id:
            _set_edge(active_relationships, "HAS_BUDGET_SIGNAL", execution_id, budget_id)
        _set_edge(active_relationships, "HAS_BUDGET_SIGNAL", event_id, budget_id)
    if event.event_type.startswith("context.compaction."):
        compaction_id = f"context_compaction:{event_id}"
        _set_node(active_nodes, "ContextCompaction", compaction_id)
        if execution_id:
            _set_edge(active_relationships, "HAS_COMPACTION", execution_id, compaction_id)
        _set_edge(active_relationships, "HAS_COMPACTION", event_id, compaction_id)
        memory_id = _string(_nested(event_payload, "record").get("memory_id"))
        if memory_id:
            _set_node(active_nodes, "Memory", memory_id)
            _set_edge(active_relationships, "CREATED_MEMORY", compaction_id, memory_id)
    if event.event_type == "monitor.finding.created":
        finding_id = f"monitor_finding:{event_id}"
        _set_node(active_nodes, "MonitorFinding", finding_id)
        if execution_id:
            _set_edge(active_relationships, "RAISED_FINDING", execution_id, finding_id)
        _set_edge(active_relationships, "RAISED_FINDING", event_id, finding_id)


def _apply_memory_expected(
        event: GraphProjectionEvent,
        payload: dict[str, Any],
        active_nodes: dict[tuple[str, str], bool],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    memory_id = _string(payload.get("memory_id") or event.aggregate_id)
    if not memory_id:
        return
    _set_node(active_nodes, "Memory", memory_id)
    if event.event_type == "memory.source_intelligence.graph_hints.approved":
        _apply_source_intelligence_graph_hints_expected(payload, memory_id, active_nodes, active_relationships)
        return
    created_by_user_id = _string(payload.get("created_by_user_id"))
    if created_by_user_id:
        _set_node(active_nodes, "User", created_by_user_id)
        _set_edge(active_relationships, "CREATED_MEMORY", created_by_user_id, memory_id)
    for target_label, target_id in (
            ("Workflow", _string(payload.get("workflow_id"))),
            ("Agent", _string(payload.get("agent_id"))),
            ("Conversation", _string(payload.get("conversation_id"))),
    ):
        if target_id:
            _set_node(active_nodes, target_label, target_id)
            _set_edge(active_relationships, "AVAILABLE_TO", memory_id, target_id, target_label.lower())
    source_execution_id = _string(payload.get("source_execution_id"))
    if source_execution_id:
        _set_node(active_nodes, "WorkflowRun", source_execution_id)
        _set_edge(active_relationships, "SOURCE_EXECUTION", memory_id, source_execution_id)
    source_conversation_id = _string(payload.get("source_conversation_id"))
    if source_conversation_id:
        _set_node(active_nodes, "Conversation", source_conversation_id)
        _set_edge(active_relationships, "SOURCE_CONVERSATION", memory_id, source_conversation_id)
    supersedes_memory_id = _string(payload.get("supersedes_memory_id"))
    if supersedes_memory_id:
        _set_node(active_nodes, "Memory", supersedes_memory_id)
        _set_edge(active_relationships, "SUPERSEDES", memory_id, supersedes_memory_id)
    for entity in _dict_list(payload.get("entities")):
        entity_id = _string(entity.get("id"))
        if entity_id:
            _set_node(active_nodes, "Entity", entity_id)
            _set_edge(active_relationships, "MENTIONS", memory_id, entity_id, entity.get("extractor_version"))
            document_id = _string(payload.get("document_id"))
            if document_id:
                _set_node(active_nodes, "Document", document_id)
                _set_edge(active_relationships, "MENTIONS", document_id, entity_id, entity.get("extractor_version"))
    if payload.get("memory_type") == "context_pack":
        _set_node(active_nodes, "ContextPack", memory_id)
        _set_edge(active_relationships, "HAS_CONTEXT_PACK", memory_id, memory_id)
        for label, key, prefix in (
                ("Decision", "decisions", "decision"),
                ("Constraint", "constraints", "constraint"),
                ("OpenQuestion", "open_questions", "open-question"),
                ("NextAction", "next_actions", "next-action"),
        ):
            for item in _context_pack_items(payload, key, prefix=prefix):
                item_id = item["id"]
                _set_node(active_nodes, label, item_id)
                _set_edge(active_relationships, "SUMMARIZES", memory_id, item_id)
                _set_edge(active_relationships, "MENTIONS", memory_id, item_id)


def _apply_source_intelligence_graph_hints_expected(
        payload: dict[str, Any],
        memory_id: str,
        active_nodes: dict[tuple[str, str], bool],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    graph_hints = _nested(payload, "graph_hints")
    entities = _source_graph_entities_for_expected(payload)
    for entity in entities:
        entity_id = entity["id"]
        _set_node(active_nodes, "Entity", entity_id)
        if entity["label"] != "Entity":
            _set_node(active_nodes, entity["label"], entity_id)
        _set_edge(active_relationships, "MENTIONS", memory_id, entity_id, "source_intelligence")
        document_id = _string(payload.get("document_id"))
        if document_id:
            _set_node(active_nodes, "Document", document_id)
            _set_edge(active_relationships, "SOURCE_DOCUMENT", memory_id, document_id)
            _set_edge(active_relationships, "MENTIONS", document_id, entity_id, "source_intelligence")
    labels_by_name = {entity["normalized_name"]: entity["label"] for entity in entities}
    raw_relationships = payload.get("relationships")
    if raw_relationships is None:
        raw_relationships = graph_hints.get("relationships")
    for relationship in _dict_list(raw_relationships):
        relationship_type = _source_graph_relationship_type(relationship.get("relationship_type"))
        source_name = _string(relationship.get("source_name"))
        target_name = _string(relationship.get("target_name"))
        if not relationship_type or not source_name or not target_name:
            continue
        source_label = labels_by_name.get(_normalized_source_graph_name(source_name), "Entity")
        target_label = labels_by_name.get(_normalized_source_graph_name(target_name), "Entity")
        source_id = _source_graph_node_id(source_label, source_name)
        target_id = _source_graph_node_id(target_label, target_name)
        _set_node(active_nodes, "Entity", source_id)
        _set_node(active_nodes, "Entity", target_id)
        if source_label != "Entity":
            _set_node(active_nodes, source_label, source_id)
        if target_label != "Entity":
            _set_node(active_nodes, target_label, target_id)
        _set_edge(active_relationships, relationship_type, source_id, target_id, memory_id)


def _apply_document_expected(
        event: GraphProjectionEvent,
        payload: dict[str, Any],
        active_nodes: dict[tuple[str, str], bool],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    document_id = _string(payload.get("document_id") or event.aggregate_id)
    if not document_id:
        return
    _set_node(active_nodes, "Document", document_id)
    for index, memory_id in enumerate(_string_list(payload.get("memory_ids"))):
        chunk_id = f"{document_id}:chunk:{index}"
        _set_node(active_nodes, "DocumentChunk", chunk_id)
        _set_node(active_nodes, "Memory", memory_id)
        _set_edge(active_relationships, "HAS_CHUNK", document_id, chunk_id)
        _set_edge(active_relationships, "PART_OF_DOCUMENT", chunk_id, document_id)
        _set_edge(active_relationships, "PART_OF_DOCUMENT", memory_id, document_id)
        _set_edge(active_relationships, "SOURCE_DOCUMENT", memory_id, document_id)
    for target_label, target_id in (
            ("Workflow", _string(payload.get("workflow_id"))),
            ("Agent", _string(payload.get("agent_id"))),
            ("Conversation", _string(payload.get("conversation_id"))),
    ):
        if target_id:
            _set_node(active_nodes, target_label, target_id)
            _set_edge(active_relationships, "AVAILABLE_TO", document_id, target_id, target_label.lower())


def _apply_workflow_memory_link_expected(
        event: GraphProjectionEvent,
        payload: dict[str, Any],
        active_nodes: dict[tuple[str, str], bool],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    workflow_id = _string(payload.get("workflow_id"))
    link = _nested(payload, "link")
    link_id = _string(link.get("id") or event.aggregate_id)
    if workflow_id:
        _set_node(active_nodes, "Workflow", workflow_id)
    for memory_id in _string_list(link.get("memoryIds")):
        _set_node(active_nodes, "Memory", memory_id)
        if workflow_id:
            _set_edge(active_relationships, "LINKS_MEMORY", workflow_id, memory_id, link_id)
    target_type = _string(link.get("targetType"))
    target_id = _string(link.get("targetId"))
    if target_type == "agent" and target_id:
        _set_node(active_nodes, "Agent", target_id)
        _set_edge(active_relationships, "DEFINES_AGENT", workflow_id, target_id)
        for memory_id in _string_list(link.get("memoryIds")):
            _set_edge(active_relationships, "HAS_MEMORY_LINK", target_id, memory_id, link_id)
    elif target_type == "task" and target_id:
        _set_node(active_nodes, "Task", target_id)
        _set_edge(active_relationships, "DEFINES_TASK", workflow_id, target_id)
        for memory_id in _string_list(link.get("memoryIds")):
            _set_edge(active_relationships, "HAS_MEMORY_LINK", target_id, memory_id, link_id)
    elif target_type == "workflow":
        for memory_id in _string_list(link.get("memoryIds")):
            _set_edge(active_relationships, "HAS_MEMORY_LINK", workflow_id, memory_id, link_id)


def _set_node(
        active_nodes: dict[tuple[str, str], bool],
        label: str,
        node_id: Any,
        *,
        active: bool = True,
) -> None:
    clean_id = _string(node_id)
    if clean_id:
        active_nodes[(label, clean_id)] = active


def _set_relationship(
        active_relationships: dict[tuple[str, str], bool],
        relationship_type: str,
        relationship_id: Any,
        *,
        active: bool = True,
) -> None:
    clean_id = _string(relationship_id)
    if clean_id:
        active_relationships[(relationship_type, clean_id)] = active


def _set_edge(
        active_relationships: dict[tuple[str, str], bool],
        relationship_type: str,
        source: Any,
        target: Any,
        qualifier: Any = None,
) -> None:
    clean_source = _string(source)
    clean_target = _string(target)
    if not clean_source or not clean_target:
        return
    relationship_id = f"{clean_source}:{relationship_type}:{clean_target}"
    clean_qualifier = _string(qualifier)
    if clean_qualifier:
        relationship_id = f"{relationship_id}:{clean_qualifier}"
    _set_relationship(active_relationships, relationship_type, relationship_id)


def _apply_event_sequence_expected(
        event: GraphProjectionEvent,
        *,
        active_event_sequences: dict[tuple[str, int], str],
) -> None:
    if not _is_execution_detail_event(event.event_type):
        return
    payload = event.payload if isinstance(event.payload, dict) else {}
    active = not event.event_type.endswith(".deleted")
    execution_id = _string(payload.get("execution_id") or event.aggregate_id)
    sequence = _sequence_int(payload.get("sequence"))
    event_id = _event_node_id(event, execution_id, payload)
    if not execution_id or sequence is None or not event_id:
        return
    key = (execution_id, sequence)
    if active:
        active_event_sequences[key] = event_id
    else:
        active_event_sequences.pop(key, None)


def _apply_followed_by_expected(
        active_event_sequences: dict[tuple[str, int], str],
        active_relationships: dict[tuple[str, str], bool],
) -> None:
    for (execution_id, sequence), event_id in active_event_sequences.items():
        if sequence <= 1:
            continue
        previous_event_id = active_event_sequences.get((execution_id, sequence - 1))
        if previous_event_id:
            _set_edge(active_relationships, "FOLLOWED_BY", previous_event_id, event_id)


def _deactivate_workflow_relationships(
        active_relationships: dict[tuple[str, str], bool],
        workflow_id: str,
        *,
        preserve_run_relationships: bool = False,
) -> None:
    if not workflow_id:
        return
    workflow_relationship_types = {
        "DEFINES_AGENT",
        "DEFINES_TASK",
        "DEFINES_TOOL",
        "HAS_MEMORY_LINK",
        "LINKS_MEMORY",
    }
    if not preserve_run_relationships:
        workflow_relationship_types.update({"HAS_RUN", "STARTED"})
    scoped_relationship_types = {
        "ASSIGNED_TO",
        "CAN_HANDOFF_TO",
        "CAN_USE",
        "DEPENDS_ON",
        "USED_MODEL",
        "USES_MODEL_PROFILE",
        "USES_TOOL",
    }
    for relationship_key in list(active_relationships):
        relationship_type, relationship_id = relationship_key
        starts_from_workflow = relationship_id.startswith(f"{workflow_id}:{relationship_type}:")
        scoped_to_workflow = relationship_id.endswith(f":{workflow_id}")
        if relationship_type in workflow_relationship_types and starts_from_workflow:
            active_relationships[relationship_key] = False
        if relationship_type in scoped_relationship_types and scoped_to_workflow:
            active_relationships[relationship_key] = False


def _deactivate_memory_relationships(
        active_relationships: dict[tuple[str, str], bool],
        memory_id: str,
) -> None:
    if not memory_id:
        return
    memory_relationship_types = {
        "AVAILABLE_TO",
        "CREATED_MEMORY",
        "HAS_CONTEXT_PACK",
        "MENTIONS",
        "SOURCE_CONVERSATION",
        "SOURCE_DOCUMENT",
        "SOURCE_EXECUTION",
        "SUMMARIZES",
        "SUPERSEDES",
    }
    for relationship_key in list(active_relationships):
        relationship_type, relationship_id = relationship_key
        if relationship_type not in memory_relationship_types:
            continue
        source, target = _relationship_endpoints(relationship_id, relationship_type)
        if source == memory_id or target == memory_id:
            active_relationships[relationship_key] = False


def _relationship_endpoints(relationship_id: str, relationship_type: str) -> tuple[str, str]:
    delimiter = f":{relationship_type}:"
    if delimiter not in relationship_id:
        return "", ""
    source, target_and_qualifier = relationship_id.split(delimiter, 1)
    target = target_and_qualifier.split(":", 1)[0]
    return source, target


def _event_node_id(event: GraphProjectionEvent, execution_id: str, payload: dict[str, Any]) -> str:
    return _string(
        event.source_event_id or f"{execution_id}:{event.event_type}:{payload.get('sequence') or event.event_id}")


def _source_graph_entities_for_expected(payload: dict[str, Any]) -> list[dict[str, str]]:
    graph_hints = _nested(payload, "graph_hints")
    raw_entities = payload.get("entities")
    if raw_entities is None:
        raw_entities = graph_hints.get("entities")
    entities_by_name: dict[str, dict[str, str]] = {}
    for item in _dict_list(raw_entities):
        name = _string(item.get("name"))
        if not name:
            continue
        label = _source_graph_label(item.get("label"))
        normalized_name = _normalized_source_graph_name(name)
        entities_by_name[normalized_name] = {
            "id": _source_graph_node_id(label, name),
            "label": label,
            "normalized_name": normalized_name,
        }
    raw_relationships = payload.get("relationships")
    if raw_relationships is None:
        raw_relationships = graph_hints.get("relationships")
    for relationship in _dict_list(raw_relationships):
        for key in ("source_name", "target_name"):
            name = _string(relationship.get(key))
            normalized_name = _normalized_source_graph_name(name)
            if name and normalized_name not in entities_by_name:
                entities_by_name[normalized_name] = {
                    "id": _source_graph_node_id("Entity", name),
                    "label": "Entity",
                    "normalized_name": normalized_name,
                }
    return list(entities_by_name.values())


def _source_graph_node_id(label: str, name: str) -> str:
    normalized_label = _source_graph_label(label).lower()
    normalized_name = _normalized_source_graph_name(name)
    digest = hashlib.sha1(f"{normalized_label}:{normalized_name}".encode("utf-8")).hexdigest()[:16]
    return f"source-intelligence:{normalized_label}:{digest}"


def _source_graph_label(value: Any) -> str:
    label = _string(value)
    if label in SOURCE_INTELLIGENCE_GRAPH_ENTITY_LABELS:
        return label
    return "Entity"


def _source_graph_relationship_type(value: Any) -> str:
    relationship_type = _string(value)
    return relationship_type if relationship_type in SOURCE_INTELLIGENCE_GRAPH_RELATIONSHIP_TYPES else ""


def _normalized_source_graph_name(value: Any) -> str:
    return " ".join(_string(value).lower().split())


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_string(item) for item in value if _string(item)]


def _nested(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _first_string(*values: Any) -> str:
    for value in values:
        text = _string(value)
        if text:
            return text
    return ""


def _container_source_id(event: GraphProjectionEvent, payload: dict[str, Any]) -> str:
    event_payload = _nested(payload, "payload")
    container_id = _first_string(
        payload.get("container_id"),
        event_payload.get("container_id"),
        event_payload.get("containerId"),
        event_payload.get("id") if event.event_type.startswith("container.") else None,
    )
    container_name = _first_string(
        payload.get("container_name"),
        event_payload.get("container_name"),
        event_payload.get("containerName"),
        event_payload.get("name") if event.event_type.startswith("container.") else None,
    )
    return _first_string(container_id, container_name)


def _sequence_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and value.strip():
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None


def _context_pack_items(payload: dict[str, Any], key: str, *, prefix: str) -> list[dict[str, str]]:
    memory_id = _string(payload.get("memory_id"))
    metadata = _nested(payload, "metadata")
    raw_items = metadata.get(key)
    if not memory_id or not isinstance(raw_items, list):
        return []
    items: list[dict[str, str]] = []
    for index, item in enumerate(raw_items):
        if isinstance(item, str):
            summary = item.strip()
            item_id = f"{memory_id}:{prefix}:{index}"
        elif isinstance(item, dict):
            summary = _string(item.get("summary") or item.get("text") or item.get("title") or item.get("name"))
            item_id = _string(item.get("id") or item.get("node_id")) or f"{memory_id}:{prefix}:{index}"
        else:
            continue
        if summary:
            items.append({"id": item_id, "summary": summary[:500]})
    return items[:50]


def _workflow_version_id(payload: dict[str, Any], workflow_id: Any) -> str:
    explicit = _string(payload.get("workflow_version_id") or payload.get("workflow_version"))
    if explicit:
        return explicit
    clean_workflow_id = _string(workflow_id)
    revision = payload.get("revision")
    if clean_workflow_id and revision is not None:
        return f"{clean_workflow_id}:v{revision}"
    return ""


def _model_id(provider: str, model_name: str) -> str:
    if not model_name:
        return ""
    return f"{provider}:{model_name}" if provider else model_name


def _error_message(event: GraphProjectionEvent, payload: dict[str, Any]) -> str:
    event_payload = _nested(payload, "payload")
    for value in (
            payload.get("execution_error"),
            payload.get("error"),
            event_payload.get("error"),
            event_payload.get("message"),
            event_payload.get("reason"),
    ):
        clean = _string(value)
        if clean:
            return clean
    return event.event_type if "failed" in event.event_type or "error" in event.event_type else ""


def _is_execution_detail_event(event_type: str) -> bool:
    return (
            event_type.startswith("approval.")
            or event_type.startswith("artifact.")
            or event_type.startswith("container.")
            or event_type.startswith("context.")
            or event_type.startswith("llm.")
            or event_type.startswith("monitor.")
            or event_type.startswith("runtime.")
            or event_type.startswith("token.")
            or event_type.startswith("tool.call.")
    )


async def _first_record(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, list):
        return result[0] if result else None
    single = getattr(result, "single", None)
    if single is not None:
        value = single()
        if hasattr(value, "__await__"):
            value = await value
        return value
    if hasattr(result, "__aiter__"):
        async for record in result:
            return record
        return None
    try:
        return next(iter(result))
    except StopIteration:
        return None


def _record_get(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        return record.get(key)
    get = getattr(record, "get", None)
    if get is not None:
        return get(key)
    try:
        return record[key]
    except (KeyError, IndexError, TypeError):
        return None


def _safe_identifier(value: str) -> str:
    normalized = "".join(character for character in value if character.isalnum() or character == "_")
    if normalized != value or not normalized:
        raise ValueError(f"Invalid Neo4j identifier: {value}")
    return normalized


__all__ = ["GraphParityItem", "GraphParityResult", "Neo4jGraphParityChecker"]
