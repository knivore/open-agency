"""Graph projection event to visualization delta mapping."""

from __future__ import annotations

from typing import Any

from app.domain import GraphProjectionEvent
from app.modules.registry import append_optional_module_graph_deltas

SIGMA_GRAPH_DELTA_SCHEMA_VERSION = "sigma.graph.delta.v1"


def graph_projection_event_to_delta(event: GraphProjectionEvent) -> dict[str, Any]:
    """Convert a projected outbox event into a Sigma-compatible graph delta.

    This is intentionally a visualization contract. It does not replace Neo4j
    projection logic and should stay a lossy, safe summary of projected graph
    structure for realtime UI patching.
    """

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    removals: dict[str, list[str]] = {}
    payload = event.payload if isinstance(event.payload, dict) else {}
    event_type = event.event_type

    if event_type.startswith("workflow."):
        workflow_id = _string(payload.get("workflow_id") or event.aggregate_id)
        deleted = event_type.endswith(".deleted")
        nodes.append(
            _node(
                workflow_id,
                "Workflow",
                _string(payload.get("name") or workflow_id),
                event,
                {**payload, "deleted": deleted},
            )
        )
        if not deleted:
            _append_workflow_definition_delta(workflow_id, payload, event, nodes=nodes, edges=edges)

    elif event.aggregate_type == "step_run" or event_type in {"task.started", "agent.step.completed",
                                                              "agent.step.failed"}:
        step_run_id = _string(event.aggregate_id)
        execution_id = _string(payload.get("execution_id"))
        label = _string(payload.get("task_id") or step_run_id)
        nodes.append(
            _node(
                step_run_id,
                "StepRun",
                label,
                event,
                {**payload, "status": _status_from_event(event)},
            )
        )
        if execution_id:
            nodes.append(_node(execution_id, "WorkflowRun", execution_id, event, {"id": execution_id}))
            edges.append(_edge(execution_id, step_run_id, "HAS_STEP_RUN", event))

    elif event_type.startswith("execution."):
        execution_id = _string(payload.get("execution_id") or event.aggregate_id)
        workflow_id = _string(payload.get("workflow_id"))
        deleted = event_type.endswith(".deleted")
        nodes.append(
            _node(
                execution_id,
                "WorkflowRun",
                execution_id,
                event,
                {**payload, "status": "deleted" if deleted else _status_from_event(event), "deleted": deleted},
            )
        )
        if workflow_id and not deleted:
            nodes.append(_node(workflow_id, "Workflow", workflow_id, event, {"id": workflow_id}))
            edges.append(_edge(workflow_id, execution_id, "HAS_RUN", event))
            edges.append(_edge(workflow_id, execution_id, "STARTED", event))

    elif _is_execution_detail_event(event_type):
        execution_id = _string(payload.get("execution_id") or event.aggregate_id)
        event_id = _string(
            event.source_event_id or f"{execution_id}:{event_type}:{payload.get('sequence') or event.event_id}")
        event_node_type = "ContainerEvent" if event_type.startswith("container.") else "ExecutionEvent"
        if execution_id:
            nodes.append(
                _node(execution_id, "WorkflowRun", execution_id, event, {"id": execution_id, "canonical_type": "Run"}))
        nodes.append(
            _node(
                event_id,
                event_node_type,
                f"{payload.get('sequence') or ''} {event_type}".strip(),
                event,
                {
                    "id": event_id,
                    "event_type": event_type,
                    "sequence": payload.get("sequence"),
                    "status": payload.get("status") or _status_from_event(event),
                    "agent_id": payload.get("agent_id"),
                    "task_id": payload.get("task_id"),
                    "source_event_id": event.source_event_id,
                },
            )
        )
        if execution_id:
            edges.append(_edge(execution_id, event_id, "EMITTED_EVENT", event))
        parent_event_id = _string(payload.get("parent_event_id"))
        if parent_event_id:
            parent_id = parent_event_id
            nodes.append(_node(parent_id, "ExecutionEvent", parent_id, event, {"id": parent_id}))
            edges.append(_edge(parent_id, event_id, "PARENT_OF", event))
        agent_id = _string(payload.get("agent_id"))
        if agent_id and execution_id:
            nodes.append(_node(agent_id, "Agent", agent_id, event, {"id": agent_id}))
            edges.append(_edge(agent_id, execution_id, "PARTICIPATED_IN", event))
            edges.append(_edge(agent_id, event_id, "EMITTED_EVENT", event))
        task_id = _string(payload.get("task_id"))
        if task_id and execution_id:
            nodes.append(_node(task_id, "Task", task_id, event, {"id": task_id}))
            edges.append(_edge(task_id, execution_id, "OCCURRED_IN", event))
        tool_call_id = _string(
            payload.get("tool_call_id") or _payload_get(payload, "tool_call_id") or _payload_get(payload, "toolCallId"))
        if tool_call_id and execution_id:
            nodes.append(_node(tool_call_id, "ToolCall", _payload_get(payload, "tool_name") or tool_call_id, event,
                               {"id": tool_call_id}))
            edges.append(_edge(tool_call_id, execution_id, "OCCURRED_IN", event))
        model_request_id = _string(
            payload.get("model_request_id") or _payload_get(payload, "model_request_id") or _payload_get(payload,
                                                                                                         "modelRequestId"))
        if model_request_id and execution_id:
            nodes.append(_node(model_request_id, "ModelRequest", model_request_id, event, {"id": model_request_id}))
            edges.append(_edge(model_request_id, execution_id, "OCCURRED_IN", event))
            model_provider = _payload_get(payload, "provider") or _payload_get(payload, "model_provider")
            model_name = _payload_get(payload, "model") or _payload_get(payload, "model_name")
            model_id = _model_id(model_provider, model_name)
            if model_id:
                nodes.append(_node(model_id, "Model", model_name or model_id, event,
                                   {"id": model_id, "provider": model_provider, "model": model_name}))
                edges.append(_edge(execution_id, model_id, "USED_MODEL", event))
                edges.append(_edge(model_request_id, model_id, "USED_MODEL", event))
            if model_provider:
                nodes.append(_node(model_provider, "ModelProvider", model_provider, event, {"id": model_provider}))
                if model_id:
                    edges.append(_edge(model_id, model_provider, "USED_PROVIDER", event))
        _append_observability_delta(execution_id, event_id, payload, event, nodes=nodes, edges=edges)

    elif event_type.startswith("memory."):
        memory_id = _string(payload.get("memory_id") or event.aggregate_id)
        deleted = event_type.endswith(".deleted")
        nodes.append(
            _node(
                memory_id,
                "Memory",
                _string(payload.get("summary") or memory_id),
                event,
                {**payload, "deleted": deleted},
            )
        )
        provenance = payload.get("graph_provenance") if isinstance(payload.get("graph_provenance"), dict) else {}
        if provenance and not deleted:
            working_set_id = _string(provenance.get("working_set_id") or payload.get("graph_working_set_id"))
            if working_set_id:
                nodes.append(
                    _node(
                        working_set_id,
                        "GraphWorkingSet",
                        working_set_id,
                        event,
                        {"id": working_set_id, "source_memory_id": memory_id},
                    )
                )
                edges.append(_edge(memory_id, working_set_id, "DERIVED_FROM_WORKING_SET", event))
            for graph_node in _dict_list(provenance.get("visited_nodes")) + _dict_list(
                    provenance.get("selected_nodes")):
                graph_node_id = _string(graph_node.get("id"))
                if not graph_node_id:
                    continue
                graph_node_type = _graph_provenance_node_type(graph_node)
                nodes.append(
                    _node(
                        graph_node_id,
                        graph_node_type,
                        graph_node_id,
                        event,
                        {
                            "id": graph_node_id,
                            "source_record_type": graph_node.get("source_record_type"),
                            "source_record_id": graph_node.get("source_record_id"),
                        },
                    )
                )
                edges.append(_edge(memory_id, graph_node_id, "DERIVED_FROM_GRAPH_NODE", event))
            for anchor in _dict_list(provenance.get("anchors")):
                anchor_id = _string(anchor.get("id"))
                if not anchor_id:
                    continue
                anchor_type = _string(anchor.get("type")) or "GraphAnchor"
                nodes.append(
                    _node(anchor_id, _graph_projection_label(anchor_type), anchor_id, event, {"id": anchor_id}))
                edges.append(_edge(memory_id, anchor_id, "DERIVED_FROM_GRAPH_ANCHOR", event))

    elif event_type.startswith("document_memory_collection."):
        document_id = _string(payload.get("document_id") or event.aggregate_id)
        deleted = event_type.endswith(".deleted")
        nodes.append(
            _node(
                document_id,
                "Document",
                _string(payload.get("filename") or document_id),
                event,
                {**payload, "deleted": deleted},
            )
        )
        for memory_id in _string_list(payload.get("memory_ids")):
            nodes.append(_node(memory_id, "Memory", memory_id, event, {"id": memory_id, "deleted": deleted}))
            edges.append(_edge(document_id, memory_id, "HAS_CHUNK", event))
            edges.append(_edge(memory_id, document_id, "PART_OF_DOCUMENT", event))

    elif event_type.startswith("workflow_memory_link."):
        workflow_id = _string(payload.get("workflow_id"))
        link = payload.get("link") if isinstance(payload.get("link"), dict) else {}
        link_id = _string(link.get("id") or event.aggregate_id)
        deleted = event_type.endswith(".deleted")
        if workflow_id:
            nodes.append(_node(workflow_id, "Workflow", workflow_id, event, {"id": workflow_id}))
        for memory_id in _string_list(link.get("memoryIds")):
            nodes.append(_node(memory_id, "Memory", memory_id, event, {"id": memory_id}))
            edge_id = _relationship_id(workflow_id, memory_id, "LINKS_MEMORY", link_id)
            if not workflow_id:
                continue
            if deleted:
                removals.setdefault("removeEdgeIds", []).append(edge_id)
            else:
                edges.append(
                    _edge(
                        workflow_id,
                        memory_id,
                        "LINKS_MEMORY",
                        event,
                        edge_id=edge_id,
                        data={
                            "link_id": link_id,
                            "target_type": link.get("targetType"),
                            "target_id": link.get("targetId"),
                            "ref_type": link.get("refType"),
                            "ref_id": link.get("refId"),
                            "access_mode": link.get("accessMode"),
                            "label": link.get("label"),
                        },
                    )
                )

    elif event_type.startswith("physical.device."):
        append_optional_module_graph_deltas(payload, event, nodes=nodes, edges=edges)

    elif _is_persona_event(event):
        _append_persona_delta(payload, event, nodes=nodes, edges=edges)

    return {
        "upsertNodes": nodes,
        "upsertEdges": edges,
        **removals,
        "metadata": {
            "schemaVersion": SIGMA_GRAPH_DELTA_SCHEMA_VERSION,
            "source": "graph_projection",
            "eventId": event.event_id,
            "eventType": event.event_type,
            "aggregateType": event.aggregate_type,
            "aggregateId": event.aggregate_id,
            "occurredAt": event.occurred_at.isoformat(),
            "projectedAt": event.projected_at.isoformat() if event.projected_at else None,
        },
    }


def _node(node_id: str, node_type: str, label: str, event: GraphProjectionEvent, data: dict[str, Any]) -> dict[
    str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "label": label or node_id,
        "clusterId": node_type,
        "startedAt": event.occurred_at.isoformat(),
        "data": data,
    }


def _edge(
        source: str,
        target: str,
        relationship_type: str,
        event: GraphProjectionEvent,
        *,
        edge_id: str | None = None,
        data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": edge_id or _relationship_id(source, target, relationship_type),
        "source": source,
        "target": target,
        "type": relationship_type,
        "label": relationship_type,
        "startedAt": event.occurred_at.isoformat(),
        "data": data or {},
    }


def _relationship_id(source: str, target: str, relationship_type: str, qualifier: str | None = None) -> str:
    parts = [source, relationship_type, target]
    if qualifier:
        parts.append(qualifier)
    return ":".join(parts)


def _graph_provenance_node_type(node: dict[str, Any]) -> str:
    return _graph_projection_label(_string(node.get("type") or node.get("source_record_type") or "GraphNode"))


def _graph_projection_label(value: str) -> str:
    cleaned = "".join(character for character in value.title() if character.isalnum())
    return cleaned or "GraphNode"


def _append_workflow_definition_delta(
        workflow_id: str,
        payload: dict[str, Any],
        event: GraphProjectionEvent,
        *,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
) -> None:
    agents = _dict_list(payload.get("agents"))
    tasks = _dict_list(payload.get("tasks"))
    tools = _dict_list(payload.get("tools"))

    for tool in tools:
        tool_id = _string(tool.get("id"))
        if not tool_id:
            continue
        nodes.append(
            _node(
                tool_id,
                "Tool",
                _string(tool.get("display_name") or tool.get("name") or tool_id),
                event,
                tool,
            )
        )
        if workflow_id:
            edges.append(_edge(workflow_id, tool_id, "DEFINES_TOOL", event))

    for agent in agents:
        agent_id = _string(agent.get("id"))
        if not agent_id:
            continue
        nodes.append(
            _node(
                agent_id,
                "Agent",
                _string(agent.get("display_name") or agent.get("name") or agent_id),
                event,
                agent,
            )
        )
        if workflow_id:
            edges.append(_edge(workflow_id, agent_id, "DEFINES_AGENT", event))
        for tool_id in _string_list(agent.get("tool_ids")):
            nodes.append(_node(tool_id, "Tool", tool_id, event, {"id": tool_id}))
            edges.append(_edge(agent_id, tool_id, "CAN_USE", event,
                               edge_id=_relationship_id(agent_id, tool_id, "CAN_USE", workflow_id)))
        for handoff_agent_id in _string_list(agent.get("handoff_agent_ids")):
            nodes.append(_node(handoff_agent_id, "Agent", handoff_agent_id, event, {"id": handoff_agent_id}))
            edges.append(
                _edge(
                    agent_id,
                    handoff_agent_id,
                    "CAN_HANDOFF_TO",
                    event,
                    edge_id=_relationship_id(agent_id, handoff_agent_id, "CAN_HANDOFF_TO", workflow_id),
                )
            )
        model_profile_id = _string(agent.get("model_profile_id"))
        if model_profile_id:
            nodes.append(_node(model_profile_id, "Model", model_profile_id, event, {"id": model_profile_id}))
            edges.append(_edge(agent_id, model_profile_id, "USED_MODEL", event,
                               edge_id=_relationship_id(agent_id, model_profile_id, "USED_MODEL", workflow_id)))
            edges.append(
                _edge(
                    agent_id,
                    model_profile_id,
                    "USES_MODEL_PROFILE",
                    event,
                    edge_id=_relationship_id(agent_id, model_profile_id, "USES_MODEL_PROFILE", workflow_id),
                )
            )

    for task in tasks:
        task_id = _string(task.get("id"))
        if not task_id:
            continue
        nodes.append(_node(task_id, "Task", _string(task.get("name") or task_id), event, task))
        if workflow_id:
            edges.append(_edge(workflow_id, task_id, "DEFINES_TASK", event))
        agent_id = _string(task.get("agent_id"))
        if agent_id:
            nodes.append(_node(agent_id, "Agent", agent_id, event, {"id": agent_id}))
            edges.append(_edge(task_id, agent_id, "ASSIGNED_TO", event,
                               edge_id=_relationship_id(task_id, agent_id, "ASSIGNED_TO", workflow_id)))
        for tool_id in _string_list(task.get("tool_ids")):
            nodes.append(_node(tool_id, "Tool", tool_id, event, {"id": tool_id}))
            edges.append(_edge(task_id, tool_id, "USES_TOOL", event,
                               edge_id=_relationship_id(task_id, tool_id, "USES_TOOL", workflow_id)))
        for dependency_id in _string_list(task.get("depends_on_task_ids")):
            nodes.append(_node(dependency_id, "Task", dependency_id, event, {"id": dependency_id}))
            edges.append(_edge(task_id, dependency_id, "DEPENDS_ON", event,
                               edge_id=_relationship_id(task_id, dependency_id, "DEPENDS_ON", workflow_id)))


def _append_observability_delta(
        execution_id: str,
        event_id: str,
        payload: dict[str, Any],
        event: GraphProjectionEvent,
        *,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
) -> None:
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    event_type = event.event_type

    if event_type == "context.health.recorded":
        context_id = f"context_health:{event_id}"
        nodes.append(
            _node(
                context_id,
                "ContextHealth",
                _string(nested.get("status") or metrics.get("context_status") or context_id),
                event,
                {
                    "id": context_id,
                    "status": nested.get("status") or metrics.get("context_status"),
                    "usage_ratio": nested.get("usage_ratio") or metrics.get("context_usage_ratio"),
                    "estimated_total_context_tokens": nested.get("estimated_total_context_tokens"),
                    "context_window": nested.get("context_window"),
                    "after_compaction": bool(nested.get("after_compaction")),
                },
            )
        )
        if execution_id:
            edges.append(_edge(execution_id, context_id, "HAS_CONTEXT_HEALTH", event))
        edges.append(_edge(event_id, context_id, "RECORDED_CONTEXT_HEALTH", event))

    if event_type == "token.usage.recorded":
        usage = nested.get("usage") if isinstance(nested.get("usage"), dict) else {}
        usage_id = f"token_usage:{event_id}"
        nodes.append(
            _node(
                usage_id,
                "TokenUsage",
                _string(usage.get("total_tokens") or metrics.get("total_tokens") or usage_id),
                event,
                {
                    "id": usage_id,
                    "provider": usage.get("provider") or metrics.get("model_provider"),
                    "model": usage.get("model") or metrics.get("model_name"),
                    "total_tokens": usage.get("total_tokens") or metrics.get("total_tokens"),
                    "estimated_cost": usage.get("estimated_cost") or metrics.get("estimated_cost"),
                    "currency": usage.get("currency"),
                },
            )
        )
        if execution_id:
            edges.append(_edge(execution_id, usage_id, "RECORDED_USAGE", event))
        edges.append(_edge(event_id, usage_id, "RECORDED_USAGE", event))

    if event_type.startswith("token.budget."):
        budget = nested.get("budget") if isinstance(nested.get("budget"), dict) else {}
        budget_id = f"token_budget:{event_id}"
        nodes.append(
            _node(
                budget_id,
                "TokenBudget",
                _string(budget.get("status") or event_type.rsplit(".", 1)[-1]),
                event,
                {
                    "id": budget_id,
                    "scope": budget.get("scope"),
                    "status": budget.get("status") or event_type.rsplit(".", 1)[-1],
                    "action": budget.get("action"),
                    "used_tokens": budget.get("used_tokens") or metrics.get("used_tokens"),
                    "budget_tokens": budget.get("budget_tokens") or metrics.get("budget_tokens"),
                    "usage_ratio": budget.get("usage_ratio") or metrics.get("usage_ratio"),
                },
            )
        )
        if execution_id:
            edges.append(_edge(execution_id, budget_id, "HAS_BUDGET_SIGNAL", event))
        edges.append(_edge(event_id, budget_id, "HAS_BUDGET_SIGNAL", event))

    if event_type.startswith("context.compaction."):
        record = nested.get("record") if isinstance(nested.get("record"), dict) else {}
        compaction_id = f"context_compaction:{event_id}"
        nodes.append(
            _node(
                compaction_id,
                "ContextCompaction",
                _string(nested.get("reason") or event_type.rsplit(".", 1)[-1]),
                event,
                {
                    "id": compaction_id,
                    "status": event_type.rsplit(".", 1)[-1],
                    "reason": nested.get("reason") or record.get("reason"),
                    "compacted": bool(record.get("compacted")),
                    "memory_id": record.get("memory_id"),
                    "estimated_tokens_saved": record.get("estimated_tokens_saved"),
                },
            )
        )
        if execution_id:
            edges.append(_edge(execution_id, compaction_id, "HAS_COMPACTION", event))
        edges.append(_edge(event_id, compaction_id, "HAS_COMPACTION", event))
        memory_id = _string(record.get("memory_id"))
        if memory_id:
            nodes.append(_node(memory_id, "Memory", memory_id, event, {"id": memory_id}))
            edges.append(_edge(compaction_id, memory_id, "CREATED_MEMORY", event))

    if event_type == "monitor.finding.created":
        finding_id = f"monitor_finding:{event_id}"
        nodes.append(
            _node(
                finding_id,
                "MonitorFinding",
                _string(nested.get("summary") or nested.get("message") or nested.get("title") or finding_id),
                event,
                {
                    "id": finding_id,
                    "finding_type": nested.get("finding_type") or nested.get("type") or nested.get("category"),
                    "severity": nested.get("severity"),
                    "status": nested.get("status"),
                    "summary": nested.get("summary") or nested.get("message") or nested.get("title"),
                },
            )
        )
        if execution_id:
            edges.append(_edge(execution_id, finding_id, "RAISED_FINDING", event))
        edges.append(_edge(event_id, finding_id, "RAISED_FINDING", event))


def _append_persona_delta(
        payload: dict[str, Any],
        event: GraphProjectionEvent,
        *,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
) -> None:
    # Persona is Agency's canonical name for skill-like packages. The graph
    # keeps the lifecycle visible without exposing legacy /skills endpoints.
    persona_id = _string(payload.get("persona_id") or event.aggregate_id)
    if not persona_id:
        return
    persona_label = _string(payload.get("persona_name") or payload.get("persona_slug") or persona_id)
    nodes.append(
        _node(
            persona_id,
            "Persona",
            persona_label,
            event,
            {
                **payload,
                "id": persona_id,
                "status": payload.get("persona_status") or _status_from_event(event),
                "event_type": event.event_type,
            },
        )
    )

    run_id = _string(payload.get("run_id"))
    if run_id:
        nodes.append(
            _node(run_id, "DistillationRun", run_id, event, {"id": run_id, "persona_id": persona_id, **payload}))
        edges.append(_edge(persona_id, run_id, "PERSONA_HAS_DISTILLATION_RUN", event))

    version_id = _string(payload.get("persona_version_id"))
    if version_id:
        nodes.append(
            _node(
                version_id,
                "PersonaVersion",
                _string(payload.get("version") or version_id),
                event,
                {
                    "id": version_id,
                    "persona_id": persona_id,
                    "version": payload.get("version"),
                    "status": payload.get("version_status") or _status_from_event(event),
                },
            )
        )
        edges.append(_edge(persona_id, version_id, "PERSONA_HAS_VERSION", event))
        if run_id:
            edges.append(_edge(run_id, version_id, "RUN_PRODUCED_VERSION", event))

    item_id = _string(payload.get("item_id"))
    if item_id:
        nodes.append(
            _node(
                item_id,
                "DistillationItem",
                _string(payload.get("title") or payload.get("item_type") or item_id),
                event,
                {
                    "id": item_id,
                    "run_id": run_id,
                    "persona_id": persona_id,
                    "item_type": payload.get("item_type"),
                    "memory_layer": payload.get("memory_layer"),
                    "review_status": payload.get("review_status") or _status_from_event(event),
                    "needs_review": payload.get("needs_review"),
                },
            )
        )
        if run_id:
            edges.append(_edge(run_id, item_id, "RUN_EXTRACTED_ITEM", event))
        source_memory_id = _string(payload.get("source_memory_id"))
        if source_memory_id:
            source_node_id = _source_memory_node_id(source_memory_id)
            nodes.append(
                _node(
                    source_node_id,
                    "SourceMemory",
                    source_memory_id,
                    event,
                    {"id": source_node_id, "source_memory_id": source_memory_id},
                )
            )
            edges.append(_edge(item_id, source_node_id, "ITEM_DERIVED_FROM_MEMORY", event))

    for source_memory_id in _string_list(payload.get("source_memory_ids")):
        source_node_id = _source_memory_node_id(source_memory_id)
        nodes.append(_node(source_node_id, "SourceMemory", source_memory_id, event,
                           {"id": source_node_id, "source_memory_id": source_memory_id}))
        if run_id:
            edges.append(_edge(run_id, source_node_id, "RUN_USED_SOURCE_MEMORY", event))

    for memory_id in _string_list(payload.get("memory_ids")):
        nodes.append(_node(memory_id, "Memory", memory_id, event, {"id": memory_id, "persona_id": persona_id}))
        edges.append(_edge(persona_id, memory_id, "PERSONA_PUBLISHED_MEMORY", event))

    for tool in _dict_list(payload.get("tools")):
        tool_id = _string(tool.get("id") or tool.get("tool_id") or tool.get("name"))
        if not tool_id:
            continue
        nodes.append(_node(tool_id, "Tool", _string(tool.get("name") or tool_id), event, tool))
        edges.append(_edge(persona_id, tool_id, "PERSONA_USES_TOOL", event))

    for workflow in _dict_list(payload.get("workflows")):
        workflow_id = _string(workflow.get("id") or workflow.get("workflow_id") or workflow.get("name"))
        if not workflow_id:
            continue
        nodes.append(_node(workflow_id, "Workflow", _string(workflow.get("name") or workflow_id), event, workflow))
        edges.append(_edge(persona_id, workflow_id, "PERSONA_FOLLOWS_WORKFLOW", event))

    for artifact in _dict_list(payload.get("artifacts")):
        artifact_id = _string(artifact.get("id") or artifact.get("artifact_id") or artifact.get("name"))
        if not artifact_id:
            continue
        nodes.append(_node(artifact_id, "Artifact", _string(artifact.get("name") or artifact_id), event, artifact))
        edges.append(_edge(persona_id, artifact_id, "PERSONA_PRODUCES_ARTIFACT", event))

    agent_id = _string(payload.get("agent_id"))
    if agent_id:
        nodes.append(_node(agent_id, "Agent", agent_id, event, {"id": agent_id, "persona_id": persona_id}))
        edges.append(_edge(persona_id, agent_id, "PERSONA_MATERIALIZED_AS_AGENT", event))

    conversation_id = _string(payload.get("conversation_id"))
    if conversation_id:
        nodes.append(_node(conversation_id, "Conversation", conversation_id, event, {"id": conversation_id}))
        edges.append(_edge(persona_id, conversation_id, "PERSONA_INVOKED_IN_CONVERSATION", event))


def _dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string(value: object) -> str:
    return str(value or "").strip()


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_string(item) for item in value if _string(item)]


def _status_from_event(event: GraphProjectionEvent) -> str:
    explicit = event.payload.get("status") if isinstance(event.payload, dict) else None
    if isinstance(explicit, str) and explicit:
        return explicit
    suffix = event.event_type.rsplit(".", 1)[-1]
    return suffix if suffix else "unknown"


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


def _is_persona_event(event: GraphProjectionEvent) -> bool:
    return event.aggregate_type == "persona" or event.event_type.startswith("persona.")


def _source_memory_node_id(memory_id: str) -> str:
    return f"source_memory:{memory_id}"


def _payload_get(payload: dict[str, Any], key: str) -> str:
    nested = payload.get("payload")
    if not isinstance(nested, dict):
        return ""
    return _string(nested.get(key))


def _model_id(provider: str, model: str) -> str:
    if not model:
        return ""
    return f"{provider}:{model}" if provider else model
