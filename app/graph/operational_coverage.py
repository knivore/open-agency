"""Execution-store backed operational coverage for graph read responses."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from app.domain.executions import Execution, ExecutionStatus
from app.graph.neo4j_read import GraphReadDocument, GraphReadEdge, GraphReadNode

FAILED_STATUSES = {ExecutionStatus.FAILED.value, "error"}
ACTIVE_STATUSES = {
    ExecutionStatus.CREATED.value,
    ExecutionStatus.QUEUED.value,
    ExecutionStatus.RUNNING.value,
    ExecutionStatus.WAITING_FOR_APPROVAL.value,
    ExecutionStatus.PAUSED.value,
    ExecutionStatus.CANCELLING.value,
}


@dataclass(slots=True)
class OperationalCoverageProjection:
    nodes: list[GraphReadNode] = field(default_factory=list)
    edges: list[GraphReadEdge] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)


async def enrich_with_operational_coverage(
        document: GraphReadDocument,
        *,
        execution_store: Any,
        root_type: str,
        root_id: str,
        include: bool,
        recent_run_limit: int,
        workflow_run_limit: int,
        incident_limit: int,
) -> GraphReadDocument:
    if not include:
        return document
    executions = await _load_executions(execution_store, root_type=root_type, root_id=root_id)
    projection = build_operational_coverage(
        executions,
        root_type=root_type,
        root_id=root_id,
        recent_run_limit=recent_run_limit,
        workflow_run_limit=workflow_run_limit,
        incident_limit=incident_limit,
    )
    document.operational_nodes = projection.nodes
    document.operational_edges = projection.edges
    document.operational_coverage = projection.coverage
    return document


def build_operational_coverage(
        executions: Iterable[Execution],
        *,
        root_type: str,
        root_id: str,
        recent_run_limit: int,
        workflow_run_limit: int,
        incident_limit: int,
) -> OperationalCoverageProjection:
    ordered = sorted(executions, key=_execution_sort_time, reverse=True)
    selected = _select_high_signal_executions(
        ordered,
        root_type=root_type,
        recent_run_limit=recent_run_limit,
        workflow_run_limit=workflow_run_limit,
    )
    nodes_by_id: dict[str, GraphReadNode] = {}
    edges_by_id: dict[str, GraphReadEdge] = {}

    for execution in selected:
        workflow_id = execution.workflow_id
        nodes_by_id.setdefault(workflow_id, _workflow_node(workflow_id))
        nodes_by_id[execution.id] = _run_node(execution)
        _put_edge(edges_by_id, workflow_id, execution.id, "HAS_RUN")
        _put_edge(edges_by_id, workflow_id, execution.id, "STARTED")
        if _is_failed(execution):
            error_id = f"error:{execution.id}"
            nodes_by_id[error_id] = _error_node(error_id, execution)
            _put_edge(edges_by_id, execution.id, error_id, "FAILED_WITH")

    incidents = _incident_clusters(ordered, limit=incident_limit)
    for incident in incidents:
        nodes_by_id[incident["id"]] = GraphReadNode(
            id=incident["id"],
            type="IncidentCluster",
            labels=["IncidentCluster"],
            properties=incident["properties"],
        )
        _put_edge(edges_by_id, incident["workflow_id"], incident["id"], "HAS_INCIDENT")

    coverage = {
        "root_type": root_type,
        "root_id": root_id,
        "source": "execution_store",
        "recent_run_count": len(selected),
        "total_run_count": len(ordered),
        "workflow_count": len({execution.workflow_id for execution in ordered}),
        "failed_count": sum(1 for execution in ordered if _is_failed(execution)),
        "active_count": sum(1 for execution in ordered if _status_value(execution) in ACTIVE_STATUSES),
        "completed_count": sum(
            1 for execution in ordered if _status_value(execution) == ExecutionStatus.COMPLETED.value
        ),
        "incident_count": len(incidents),
    }
    return OperationalCoverageProjection(
        nodes=list(nodes_by_id.values()),
        edges=list(edges_by_id.values()),
        coverage=coverage,
    )


async def _load_executions(execution_store: Any, *, root_type: str, root_id: str) -> list[Execution]:
    if root_type == "workflow":
        return list(await execution_store.list_executions_by_workflow(root_id))
    if root_type == "workflow_run":
        execution = await execution_store.get_execution(root_id)
        return [] if execution is None else [execution]
    if root_type == "agent":
        list_by_agent = getattr(execution_store, "list_executions_by_agent", None)
        if list_by_agent is not None:
            return list(await list_by_agent(root_id))
    return list(await execution_store.list_executions())


def _select_high_signal_executions(
        ordered: list[Execution],
        *,
        root_type: str,
        recent_run_limit: int,
        workflow_run_limit: int,
) -> list[Execution]:
    limit = workflow_run_limit if root_type == "workflow" else recent_run_limit
    selected: dict[str, Execution] = {}

    # Keep one recent run per workflow first so one noisy workflow does not hide
    # the broader operational surface in the all-graph view.
    if root_type != "workflow":
        for execution in ordered:
            if len(selected) >= limit:
                break
            if not any(item.workflow_id == execution.workflow_id for item in selected.values()):
                selected[execution.id] = execution

    for execution in ordered:
        if len(selected) >= limit:
            break
        if _is_failed(execution) or _status_value(execution) in ACTIVE_STATUSES:
            selected.setdefault(execution.id, execution)

    for execution in ordered:
        if len(selected) >= limit:
            break
        selected.setdefault(execution.id, execution)

    return list(selected.values())


def _incident_clusters(executions: list[Execution], *, limit: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Execution]] = {}
    for execution in executions:
        if not _is_failed(execution):
            continue
        signature = _incident_signature(execution)
        grouped.setdefault((execution.workflow_id, signature), []).append(execution)

    incidents: list[dict[str, Any]] = []
    for (workflow_id, signature), items in grouped.items():
        ordered = sorted(items, key=_execution_sort_time, reverse=True)
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:10]
        incident_id = f"incident-cluster:{workflow_id}:{digest}"
        first_seen_item = min(ordered, key=_execution_sort_timestamp, default=None)
        last_seen_item = max(ordered, key=_execution_sort_timestamp, default=None)
        incidents.append(
            {
                "id": incident_id,
                "workflow_id": workflow_id,
                "last_seen_sort": _execution_sort_timestamp(ordered[0]),
                "properties": {
                    "workflow_id": workflow_id,
                    "status": "failed",
                    "severity": "error",
                    "incident_signature": signature,
                    "failure_count": len(ordered),
                    "run_ids": [item.id for item in ordered[:12]],
                    "first_seen_at": _iso_or_none(_execution_sort_time(first_seen_item) if first_seen_item else None),
                    "last_seen_at": _iso_or_none(_execution_sort_time(last_seen_item) if last_seen_item else None),
                    "example_error": ordered[0].error,
                },
            }
        )
    incidents.sort(
        key=lambda item: (item["properties"]["failure_count"], item["last_seen_sort"]),
        reverse=True,
    )
    return incidents[:limit]


def _workflow_node(workflow_id: str) -> GraphReadNode:
    return GraphReadNode(
        id=workflow_id,
        type="Workflow",
        labels=["Workflow"],
        properties={"id": workflow_id, "name": workflow_id},
    )


def _run_node(execution: Execution) -> GraphReadNode:
    return GraphReadNode(
        id=execution.id,
        type="Run",
        labels=["WorkflowRun", "Run"],
        properties={
            "id": execution.id,
            "workflow_id": execution.workflow_id,
            "status": _status_value(execution),
            "trigger_type": execution.trigger_type,
            "created_at": _iso_or_none(execution.created_at),
            "started_at": _iso_or_none(execution.started_at),
            "completed_at": _iso_or_none(execution.completed_at),
            "updated_at": _iso_or_none(execution.updated_at),
            "error": execution.error,
        },
    )


def _error_node(error_id: str, execution: Execution) -> GraphReadNode:
    return GraphReadNode(
        id=error_id,
        type="Error",
        labels=["Error"],
        properties={
            "id": error_id,
            "execution_id": execution.id,
            "workflow_id": execution.workflow_id,
            "status": _status_value(execution),
            "message": execution.error or _status_value(execution),
        },
    )


def _put_edge(edges_by_id: dict[str, GraphReadEdge], source: str, target: str, edge_type: str) -> None:
    edge_id = f"{source}:{edge_type}:{target}"
    edges_by_id[edge_id] = GraphReadEdge(id=edge_id, source=source, target=target, type=edge_type)


def _is_failed(execution: Execution) -> bool:
    return bool(execution.error) or _status_value(execution) in FAILED_STATUSES


def _status_value(execution: Execution) -> str:
    status = execution.status
    return status.value if isinstance(status, ExecutionStatus) else str(status)


def _execution_sort_time(execution: Execution) -> datetime:
    return execution.updated_at or execution.completed_at or execution.started_at or execution.created_at or datetime.min


def _execution_sort_timestamp(execution: Execution) -> float:
    value = _execution_sort_time(execution)
    if value == datetime.min:
        return 0.0
    return value.timestamp()


def _incident_signature(execution: Execution) -> str:
    raw = (execution.error or _status_value(execution) or "failed").strip().lower()
    normalized = re.sub(r"\s+", " ", raw)
    normalized = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", normalized)
    normalized = re.sub(r"\b\d+\b", "<num>", normalized)
    return normalized[:160]


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
