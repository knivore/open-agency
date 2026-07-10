"""Graph projection outbox backfill from canonical source records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.time import utc_now
from app.domain import Execution, ExecutionEvent, GraphProjectionEvent, MemoryRecord, MemoryStatus
from app.runtime.native.state import (
    GRAPH_PROJECTED_EXECUTION_EVENT_TYPES,
    _execution_projection_payload,
)
from app.services.memory import MemoryEntityExtractor, MemoryService
from app.services.source_intelligence import SourceIntelligenceService

WORKFLOW_MEMORY_LINK_METADATA_KEY = "memory_links"


@dataclass(slots=True)
class GraphProjectionBackfillResult:
    scanned: int = 0
    enqueued: int = 0
    skipped: int = 0
    domains: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def add_domain(self, domain: str, *, scanned: int, enqueued: int, skipped: int = 0) -> None:
        self.scanned += scanned
        self.enqueued += enqueued
        self.skipped += skipped
        self.domains[domain] = {
            "scanned": self.domains.get(domain, {}).get("scanned", 0) + scanned,
            "enqueued": self.domains.get(domain, {}).get("enqueued", 0) + enqueued,
            "skipped": self.domains.get(domain, {}).get("skipped", 0) + skipped,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "enqueued": self.enqueued,
            "skipped": self.skipped,
            "domains": self.domains,
            "errors": self.errors,
        }


class GraphProjectionBackfillService:
    """Regenerate graph projection outbox rows from current source records."""

    def __init__(self, context: Any):
        self.context = context
        self.memory_service = MemoryService(context)

    async def backfill(
            self,
            *,
            domains: list[str] | None = None,
            limit: int = 1000,
    ) -> GraphProjectionBackfillResult:
        selected = _normalize_domains(domains)
        result = GraphProjectionBackfillResult()
        if "workflows" in selected:
            result.add_domain("workflows", **await self.backfill_workflows(limit=limit))
        if "workflow_memory_links" in selected:
            result.add_domain("workflow_memory_links", **await self.backfill_workflow_memory_links(limit=limit))
        if "executions" in selected:
            result.add_domain("executions", **await self.backfill_executions(limit=limit))
        if "memories" in selected:
            result.add_domain("memories", **await self.backfill_memories(limit=limit))
        if "documents" in selected:
            result.add_domain("documents", **await self.backfill_documents(limit=limit))
        if "source_intelligence_graph_hints" in selected:
            result.add_domain(
                "source_intelligence_graph_hints",
                **await self.backfill_source_intelligence_graph_hints(limit=limit),
            )
        return result

    async def backfill_workflows(self, *, limit: int) -> dict[str, int]:
        scanned = enqueued = skipped = 0
        for workflow in (await self.context.workflow_repo.list(include_deleted=True))[: max(limit, 0)]:
            scanned += 1
            payload = {
                "workflow_id": workflow.id,
                "name": workflow.name,
                "description": workflow.description,
                "entrypoint": workflow.entrypoint,
                "revision": workflow.versioning.revision,
                "is_published": workflow.versioning.is_published,
                "version": workflow.versioning.version,
                "labels": workflow.versioning.labels,
                "default_runtime_adapter_id": workflow.default_runtime_adapter_id,
                "allowed_runtime_adapter_ids": workflow.allowed_runtime_adapter_ids,
                "agents": [_agent_payload(agent) for agent in workflow.agent_definitions if agent.id],
                "tasks": [_task_payload(task) for task in workflow.task_definitions if task.id],
                "tools": [_tool_payload(tool) for tool in workflow.tool_definitions if tool.id],
                "nodes": [_workflow_node_payload(node) for node in workflow.nodes if node.id],
                "edges": [_workflow_edge_payload(edge) for edge in workflow.edges if edge.id],
                "backfilled_at": utc_now().isoformat(),
            }
            await self._append(
                GraphProjectionEvent(
                    event_type="workflow.updated",
                    aggregate_type="workflow",
                    aggregate_id=workflow.id,
                    user_id=_metadata_user_id(workflow.metadata),
                    payload=payload,
                    source="graph_backfill_workflows",
                    source_event_id=f"workflow:{workflow.id}:revision:{workflow.versioning.revision}",
                )
            )
            enqueued += 1
        return {"scanned": scanned, "enqueued": enqueued, "skipped": skipped}

    async def backfill_workflow_memory_links(self, *, limit: int) -> dict[str, int]:
        scanned = enqueued = skipped = 0
        workflows = await self.context.workflow_repo.list(include_deleted=True)
        for workflow in workflows:
            for link in _workflow_memory_links(workflow.metadata):
                if scanned >= max(limit, 0):
                    return {"scanned": scanned, "enqueued": enqueued, "skipped": skipped}
                scanned += 1
                link_id = str(link.get("id") or f"{workflow.id}:{link.get('ref_id') or scanned}")
                await self._append(
                    GraphProjectionEvent(
                        event_type="workflow_memory_link.updated",
                        aggregate_type="workflow_memory_link",
                        aggregate_id=link_id,
                        user_id=_metadata_user_id(workflow.metadata),
                        payload={
                            "workflow_id": workflow.id,
                            "link": _serialize_workflow_memory_link(workflow.id, link),
                            "backfilled_at": utc_now().isoformat(),
                        },
                        source="graph_backfill_workflow_memory_links",
                        source_event_id=f"workflow_memory_link:{workflow.id}:{link_id}",
                    )
                )
                enqueued += 1
        return {"scanned": scanned, "enqueued": enqueued, "skipped": skipped}

    async def backfill_executions(self, *, limit: int) -> dict[str, int]:
        scanned = enqueued = skipped = 0
        executions = await self.context.execution_store.list_executions()
        for execution in executions:
            for event in await self.context.execution_store.list_events(execution.id):
                if scanned >= max(limit, 0):
                    return {"scanned": scanned, "enqueued": enqueued, "skipped": skipped}
                scanned += 1
                if event.event_type not in GRAPH_PROJECTED_EXECUTION_EVENT_TYPES:
                    skipped += 1
                    continue
                await self._append(_execution_event(event, execution))
                enqueued += 1
        return {"scanned": scanned, "enqueued": enqueued, "skipped": skipped}

    async def backfill_memories(self, *, limit: int) -> dict[str, int]:
        scanned = enqueued = skipped = 0
        for memory in (await self.context.memory_repo.list(include_deleted=True))[: max(limit, 0)]:
            scanned += 1
            payload = self.memory_service.graph_projection_payload_for_memory(memory)
            event_type = "memory.deleted" if memory.status != MemoryStatus.ACTIVE else "memory.updated"
            projected = await self._append(
                GraphProjectionEvent(
                    event_type=event_type,
                    aggregate_type="memory",
                    aggregate_id=memory.id,
                    user_id=memory.created_by_user_id,
                    payload={**payload, "backfilled_at": utc_now().isoformat()},
                    source="graph_backfill_memories",
                    source_event_id=f"memory:{memory.id}:updated:{memory.updated_at.isoformat()}",
                )
            )
            enqueued += 1
            enqueued += await self._append_memory_entities(memory, payload, projected.event_id)
        return {"scanned": scanned, "enqueued": enqueued, "skipped": skipped}

    async def backfill_documents(self, *, limit: int) -> dict[str, int]:
        memories = await self.context.memory_repo.list(include_deleted=True)
        groups: dict[str, list[MemoryRecord]] = {}
        for memory in memories:
            metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
            document_id = metadata.get("document_id")
            if isinstance(document_id, str) and document_id.strip():
                groups.setdefault(document_id.strip(), []).append(memory)
        scanned = enqueued = skipped = 0
        for document_id, document_memories in list(groups.items())[: max(limit, 0)]:
            scanned += 1
            payload = self._document_payload(document_id, document_memories)
            await self._append(
                GraphProjectionEvent(
                    event_type="document_memory_collection.created",
                    aggregate_type="document_memory_collection",
                    aggregate_id=document_id,
                    user_id=payload.get("created_by_user_id"),
                    payload={**payload, "backfilled_at": utc_now().isoformat()},
                    source="graph_backfill_documents",
                    source_event_id=f"document_memory_collection:{document_id}:{payload.get('chunk_count')}",
                )
            )
            enqueued += 1
        return {"scanned": scanned, "enqueued": enqueued, "skipped": skipped}

    async def backfill_source_intelligence_graph_hints(self, *, limit: int) -> dict[str, int]:
        scanned = enqueued = skipped = 0
        for memory in await self.context.memory_repo.list(include_deleted=True):
            if scanned >= max(limit, 0):
                break
            scanned += 1
            metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
            graph_hints = metadata.get("graph_hints") if isinstance(metadata.get("graph_hints"), dict) else {}
            if graph_hints.get("review_status") != "approved":
                skipped += 1
                continue
            if memory.status != MemoryStatus.ACTIVE:
                skipped += 1
                continue
            entities = graph_hints.get("entities")
            relationships = graph_hints.get("relationships")
            if not isinstance(entities, list):
                entities = []
            if not isinstance(relationships, list):
                relationships = []
            if not entities and not relationships:
                skipped += 1
                continue
            source_intelligence = (
                metadata.get("source_intelligence")
                if isinstance(metadata.get("source_intelligence"), dict)
                else {}
            )
            await self._append(
                GraphProjectionEvent(
                    event_type="memory.source_intelligence.graph_hints.approved",
                    aggregate_type="memory",
                    aggregate_id=memory.id,
                    user_id=memory.created_by_user_id,
                    payload={
                        "memory_id": memory.id,
                        "document_id": metadata.get("document_id"),
                        "filename": metadata.get("filename"),
                        "chunk_index": metadata.get("chunk_index"),
                        "status": memory.status.value,
                        "source_ref": SourceIntelligenceService.source_ref(memory),
                        "source_intelligence": source_intelligence,
                        "graph_hints": graph_hints,
                        "entities": entities,
                        "relationships": relationships,
                        "review": graph_hints.get("review"),
                        "backfilled_at": utc_now().isoformat(),
                    },
                    source="source_intelligence",
                    source_event_id=SourceIntelligenceService._graph_hints_source_event_id(
                        memory.id,
                        entities,
                        relationships,
                    ),
                )
            )
            enqueued += 1
        return {"scanned": scanned, "enqueued": enqueued, "skipped": skipped}

    async def _append(self, event: GraphProjectionEvent) -> GraphProjectionEvent:
        return await self.context.graph_projection_event_repo.append(event)

    async def _append_memory_entities(self, memory: MemoryRecord, payload: dict[str, Any], source_event_id: str) -> int:
        candidates = MemoryEntityExtractor().extract(payload)
        if not candidates:
            return 0
        await self._append(
            GraphProjectionEvent(
                event_type="memory.entities.extracted",
                aggregate_type="memory",
                aggregate_id=memory.id,
                user_id=memory.created_by_user_id,
                payload={
                    "memory_id": memory.id,
                    "document_id": payload.get("metadata", {}).get("document_id") if isinstance(payload.get("metadata"),
                                                                                                dict) else None,
                    "entities": [candidate.to_projection_payload() for candidate in candidates],
                },
                source="graph_backfill_memory_entities",
                source_event_id=source_event_id,
            )
        )
        return 1

    def _document_payload(self, document_id: str, memories: list[MemoryRecord]) -> dict[str, Any]:
        representative = memories[0] if memories else None
        metadata = representative.metadata if representative is not None and isinstance(representative.metadata,
                                                                                        dict) else {}
        memory_ids = [item.id for item in sorted(memories, key=MemoryService._document_chunk_sort_key)]
        projected_memory_ids = MemoryService._bounded_document_projection_ids(
            memory_ids,
            max_chunks=get_settings().graph_document_projection_max_chunks,
        )
        return {
            "document_id": document_id,
            "scope": representative.scope.value if representative is not None else None,
            "created_by_user_id": representative.created_by_user_id if representative is not None else None,
            "workspace_id": representative.workspace_id if representative is not None else None,
            "conversation_id": representative.conversation_id if representative is not None else None,
            "workflow_id": representative.workflow_id if representative is not None else None,
            "agent_id": representative.agent_id if representative is not None else None,
            "filename": metadata.get("filename"),
            "content_type": metadata.get("content_type"),
            "content_sha256": metadata.get("content_sha256"),
            "memory_ids": projected_memory_ids,
            "chunk_count": len(memory_ids),
            "projected_chunk_count": len(projected_memory_ids),
            "omitted_chunk_count": max(len(memory_ids) - len(projected_memory_ids), 0),
            "projection_capped": len(projected_memory_ids) < len(memory_ids),
            "source": "graph_backfill",
        }


def _execution_event(event: ExecutionEvent, execution: Execution) -> GraphProjectionEvent:
    aggregate_type = "step_run" if event.task_id else "workflow_run"
    aggregate_id = f"{event.execution_id}:{event.task_id}" if event.task_id else event.execution_id
    execution_payload = _execution_projection_payload(execution)
    return GraphProjectionEvent(
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
            "backfilled_at": utc_now().isoformat(),
        },
        source="execution_events",
        source_event_id=event.id,
    )


def _normalize_domains(domains: list[str] | None) -> set[str]:
    if not domains or "all" in domains:
        return {
            "workflows",
            "workflow_memory_links",
            "executions",
            "memories",
            "documents",
            "source_intelligence_graph_hints",
        }
    aliases = {
        "workflow": "workflows",
        "workflow-memory-links": "workflow_memory_links",
        "execution": "executions",
        "memory": "memories",
        "document": "documents",
        "source-intelligence-graph-hints": "source_intelligence_graph_hints",
        "graph-hints": "source_intelligence_graph_hints",
        "source_intelligence": "source_intelligence_graph_hints",
    }
    return {aliases.get(domain, domain) for domain in domains}


def _metadata_user_id(metadata: dict[str, Any]) -> str | None:
    user_id = metadata.get("updated_by") or metadata.get("created_by")
    return user_id if isinstance(user_id, str) else None


def _workflow_memory_links(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw_links = metadata.get(WORKFLOW_MEMORY_LINK_METADATA_KEY)
    if not isinstance(raw_links, list):
        return []
    return [dict(item) for item in raw_links if isinstance(item, dict)]


def _serialize_workflow_memory_link(workflow_id: str, link: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(link.get("id") or ""),
        "workflowId": workflow_id,
        "targetType": str(link.get("target_type") or link.get("targetType") or ""),
        "targetId": link.get("target_id") or link.get("targetId"),
        "refType": str(link.get("ref_type") or link.get("refType") or ""),
        "refId": str(link.get("ref_id") or link.get("refId") or ""),
        "memoryIds": link.get("memory_ids") if isinstance(link.get("memory_ids"), list) else link.get(
            "memoryIds") or [],
        "accessMode": str(link.get("access_mode") or link.get("accessMode") or "read"),
        "label": link.get("label"),
        "createdAt": link.get("created_at") or link.get("createdAt"),
        "createdBy": link.get("created_by") or link.get("createdBy"),
        "updatedAt": link.get("updated_at") or link.get("updatedAt"),
        "updatedBy": link.get("updated_by") or link.get("updatedBy"),
    }


def _agent_payload(agent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "name": agent.name,
        "display_name": agent.display_name,
        "description": agent.description,
        "role": agent.role,
        "model_profile_id": agent.model_profile_id,
        "tool_ids": list(agent.tool_ids),
        "handoff_agent_ids": list(agent.handoff_agent_ids),
        "memory_enabled": agent.memory.enabled,
        "memory_scope": agent.memory.scope,
        "memory_strategy": agent.memory.strategy,
        "memory_backend_ref": agent.memory.backend_ref,
    }


def _task_payload(task) -> dict[str, Any]:
    return {
        "id": task.id,
        "name": task.name,
        "description": task.description,
        "agent_id": task.agent_id,
        "tool_ids": list(task.tool_ids),
        "depends_on_task_ids": list(task.depends_on_task_ids),
        "human_approval_required": task.human_approval_required,
        "has_input_schema": bool(task.input_schema),
        "has_output_schema": bool(task.output_schema),
    }


def _tool_payload(tool) -> dict[str, Any]:
    return {
        "id": tool.id,
        "name": tool.name,
        "display_name": tool.display_name,
        "description": tool.description,
        "tool_type": tool.tool_type.value if hasattr(tool.tool_type, "value") else str(tool.tool_type),
        "tags": list(tool.tags),
        "requires_approval": tool.security.requires_approval,
        "sandbox_required": tool.security.sandbox_required,
        "allow_shell": tool.security.allow_shell,
        "allow_browser": tool.security.allow_browser,
        "allow_filesystem": tool.security.allow_filesystem,
        "allow_network": tool.security.allow_network,
        "read_only": tool.security.read_only,
        "read_only_sql": tool.security.read_only_sql,
        "dangerous": tool.security.dangerous,
        "has_input_schema": bool(tool.input_schema),
        "has_output_schema": bool(tool.output_schema),
    }


def _workflow_node_payload(node) -> dict[str, Any]:
    return {
        "id": node.id,
        "name": node.name,
        "node_type": node.node_type.value if hasattr(node.node_type, "value") else str(node.node_type),
        "agent_id": node.agent_id,
        "task_id": node.task_id,
        "tool_id": node.tool_id,
    }


def _workflow_edge_payload(edge) -> dict[str, Any]:
    return {
        "id": edge.id,
        "source_node_id": edge.source_node_id,
        "target_node_id": edge.target_node_id,
        "edge_type": edge.edge_type.value if hasattr(edge.edge_type, "value") else str(edge.edge_type),
        "has_condition": bool(edge.condition),
    }


__all__ = ["GraphProjectionBackfillResult", "GraphProjectionBackfillService"]
