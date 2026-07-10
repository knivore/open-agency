"""Deterministic Agency Graph context synthesis for future agent tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal

from app.core.config import get_settings
from app.domain import ExecutionEvent, MemoryRecord, UserDefinition
from app.graph.neo4j_read import GraphReadDocument, GraphReadEdge, GraphReadNode, Neo4jGraphReadError
from app.graph.service import (
    GRAPH_NEIGHBORHOOD_PRESETS,
    GraphReadUnavailableError,
    close_graph_reader_if_needed,
    resolve_graph_reader,
)
from app.services.memory import MemoryService

AgencyGraphIntent = Literal[
    "resume",
    "debug",
    "steer",
    "plan",
    "audit",
    "learn",
    "handoff",
    "root_cause",
]
AgencyGraphBudget = Literal["brief", "balanced", "full", "raw_graph"]

SUPPORTED_INTENTS: set[str] = {
    "resume",
    "debug",
    "steer",
    "plan",
    "audit",
    "learn",
    "handoff",
    "root_cause",
}

SUPPORTED_ANCHORS: set[str] = {
    "workflow",
    "run",
    "execution",
    "agent",
    "task",
    "step_run",
    "tool",
    "model_request",
    "memory",
    "context_pack",
    "conversation",
    "message",
    "document",
    "entity",
    "error",
    "approval_request",
    "device",
    "device_command",
    "device_event",
    "room",
}

SUPPORTED_BUDGETS: set[str] = {"brief", "balanced", "full", "raw_graph"}

ANCHOR_PRESETS = {
    "workflow": "workflow",
    "run": "workflow_run",
    "execution": "workflow_run",
    "agent": "agent",
    "task": "task",
    "step_run": "task",
    "tool": "tool",
    "memory": "memory",
    "context_pack": "memory",
    "document": "memory",
    "entity": "entity",
    "device": "physical_device",
    "device_command": "physical_audit",
    "device_event": "physical_audit",
    "room": "physical_device",
}

BUDGET_LIMITS = {
    "brief": {"nodes": 8, "edges": 8, "events": 4},
    "balanced": {"nodes": 20, "edges": 20, "events": 8},
    "full": {"nodes": 60, "edges": 60, "events": 20},
    "raw_graph": {"nodes": 100, "edges": 100, "events": 30},
}
BUDGET_TRAVERSAL_FACTORS = {
    "brief": 1,
    "balanced": 2,
    "full": 4,
    "raw_graph": 6,
}

SENSITIVE_PROPERTY_KEY_PARTS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "embedding",
    "password",
    "raw_content",
    "refresh_token",
    "secret",
    "token",
}
SENSITIVE_NODE_DISPLAY_KEYS = {
    "content",
    "error",
    "label",
    "message",
    "name",
    "raw_content",
    "summary",
    "text",
    "title",
}
DOCUMENT_CHUNK_RAW_CONTENT_KEYS = {
    "content",
    "raw_content",
    "raw_text",
    "text",
    "chunk_text",
    "page_content",
}
PROTECTED_CREDENTIAL_NODE_TYPES = {
    "apikey",
    "api_key",
    "authcredential",
    "authsession",
    "connectioncredential",
    "connectoraccount",
    "credential",
    "credentialref",
    "credentialreference",
    "externalaccount",
    "externaltoken",
    "oauthcredential",
    "oauthsession",
    "oauthtoken",
    "refreshtoken",
    "secret",
    "token",
}
PROTECTED_INTEGRATION_NODE_TYPES = {
    "a2aagent",
    "authconfig",
    "connector",
    "connectorhealth",
    "integration",
    "mcpserver",
    "oauthclient",
}
PROTECTED_NODE_PROPERTY_ALLOWLIST = {
    "created_at",
    "deleted",
    "enabled",
    "health",
    "last_seen_at",
    "projection_mode",
    "projection_version",
    "projected_at",
    "severity",
    "source_endpoint",
    "source_record_id",
    "source_record_type",
    "source_system",
    "status",
    "updated_at",
}
MAX_GRAPH_CONTEXT_STRING_LENGTH = 500

SCOPE_ANCHOR_FIELDS: tuple[tuple[str, str], ...] = (
    ("execution_id", "execution"),
    ("run_id", "run"),
    ("workflow_run_id", "run"),
    ("step_run_id", "step_run"),
    ("task_id", "task"),
    ("agent_id", "agent"),
    ("workflow_id", "workflow"),
    ("tool_id", "tool"),
    ("memory_id", "memory"),
    ("context_pack_id", "context_pack"),
    ("conversation_id", "conversation"),
    ("message_id", "message"),
    ("document_id", "document"),
    ("entity_id", "entity"),
    ("error_id", "error"),
    ("approval_request_id", "approval_request"),
)


@dataclass(slots=True)
class AgencyGraphContextRequest:
    query: str | None = None
    intent: str = "resume"
    anchor_type: str | None = None
    anchor_id: str | None = None
    preset: str | None = None
    scope: dict[str, Any] | None = None
    mode: str | None = None
    include_memories: bool = True
    include_events: bool = False
    include_raw_graph: bool = False
    budget: str = "balanced"
    limit: int = 50


@dataclass(slots=True)
class _TraversalBudgetDecision:
    allowed: bool
    actor_key: str
    units: int
    used_units: int
    remaining_units: int
    max_units: int
    window_seconds: float


_GRAPH_CONTEXT_RATE_STATE: dict[str, tuple[float, int]] = {}
_GRAPH_CONTEXT_RATE_LOCK = asyncio.Lock()


class AgencyGraphContextService:
    """Build compact, read-only graph context for agents.

    The first version deliberately avoids LLM summarization. It consumes the
    normalized graph read DTO and emits deterministic sections with provenance.
    """

    def __init__(self, context: Any):
        self.context = context

    async def build_context(self, request: AgencyGraphContextRequest | dict[str, Any]) -> dict[str, Any]:
        request = self._coerce_request(request)
        request = self._with_scope_anchor(request)
        validation_error = self._validate_request(request)
        if validation_error:
            return validation_error
        budget_decision = await _consume_traversal_budget(request)
        if not budget_decision.allowed:
            return self._budget_exceeded_response(request, budget_decision)

        try:
            document = await asyncio.wait_for(
                self._load_graph_document(request),
                timeout=_query_timeout_seconds(),
            )
        except TimeoutError:
            return self._unavailable_response(
                request,
                status="timeout",
                guidance=(
                    "Agency Graph query timed out. Try a narrower anchor, smaller limit, lower budget, durable memory "
                    "search, or execution events."
                ),
            )
        except GraphReadUnavailableError:
            status = "graph_disabled" if self._graph_read_disabled_by_settings() else "graph_unavailable"
            guidance = (
                "Enable NEO4J_ENABLED=true and run projection/backfill, or use durable memory search and execution events."
                if status == "graph_disabled"
                else "Try durable memory search, execution events, or wait for Neo4j projection/backfill."
            )
            return self._unavailable_response(
                request,
                status=status,
                guidance=guidance,
            )
        except Neo4jGraphReadError as exc:
            return self._unavailable_response(request, status="query_failed", guidance=str(exc))

        document, linked_memories = await self._filter_memory_nodes(request, document)
        document = self._filter_protected_nodes(document)
        document = self._filter_scope_restricted_nodes(request, document)
        document = self._sanitize_document_chunk_nodes(request, document)
        runtime_events = await self._runtime_event_fallback(request, document)
        return self._synthesize_context(request, document, linked_memories=linked_memories,
                                        runtime_events=runtime_events)

    async def summarize_subgraph(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._coerce_subgraph_request(payload)
        validation_error = self._validate_request(request)
        if validation_error:
            return validation_error
        document = _graph_document_from_payload(payload)
        document, linked_memories = await self._filter_memory_nodes(request, document)
        document = self._filter_protected_nodes(document)
        document = self._filter_scope_restricted_nodes(request, document)
        document = self._sanitize_document_chunk_nodes(request, document)
        return self._synthesize_context(request, document, linked_memories=linked_memories, runtime_events=[])

    def _coerce_request(self, value: AgencyGraphContextRequest | dict[str, Any]) -> AgencyGraphContextRequest:
        if isinstance(value, AgencyGraphContextRequest):
            return value
        request = AgencyGraphContextRequest(
            query=_string_or_none(value.get("query")),
            intent=str(value.get("intent") or "resume"),
            anchor_type=_string_or_none(value.get("anchor_type")),
            anchor_id=_string_or_none(value.get("anchor_id")),
            preset=_string_or_none(value.get("preset")),
            scope=value.get("scope") if isinstance(value.get("scope"), dict) else None,
            mode=_string_or_none(value.get("mode")),
            include_memories=bool(value.get("include_memories", True)),
            include_events=bool(value.get("include_events", False)),
            include_raw_graph=bool(value.get("include_raw_graph", False)),
            budget=str(value.get("budget") or "balanced"),
            limit=_bounded_int(value.get("limit", 50), minimum=1, maximum=100),
        )
        return request

    def _coerce_subgraph_request(self, value: dict[str, Any]) -> AgencyGraphContextRequest:
        request = AgencyGraphContextRequest(
            query=_string_or_none(value.get("query")) or "selected_subgraph",
            intent=str(value.get("intent") or "learn"),
            anchor_type=_string_or_none(value.get("anchor_type")),
            anchor_id=_string_or_none(value.get("anchor_id")),
            scope=value.get("scope") if isinstance(value.get("scope"), dict) else None,
            mode=_string_or_none(value.get("mode")),
            include_memories=bool(value.get("include_memories", True)),
            include_events=bool(value.get("include_events", False)),
            include_raw_graph=bool(value.get("include_raw_graph", False)),
            budget=str(value.get("budget") or "balanced"),
            limit=_bounded_int(value.get("limit", 50), minimum=1, maximum=100),
        )
        if request.anchor_type or request.anchor_id:
            request.query = _string_or_none(value.get("query"))
        return request

    def _with_scope_anchor(self, request: AgencyGraphContextRequest) -> AgencyGraphContextRequest:
        if request.anchor_type or request.anchor_id or request.query:
            return request
        anchor_type, anchor_id = _anchor_from_scope(request.scope)
        if not anchor_type or not anchor_id:
            return request
        request.anchor_type = anchor_type
        request.anchor_id = anchor_id
        return request

    def _validate_request(self, request: AgencyGraphContextRequest) -> dict[str, Any] | None:
        if request.intent not in SUPPORTED_INTENTS:
            return self._error_response(
                "invalid_intent",
                request,
                f"Unsupported Agency Graph intent: {request.intent}",
                guidance=f"Use one of: {', '.join(sorted(SUPPORTED_INTENTS))}.",
            )
        if request.budget not in SUPPORTED_BUDGETS:
            return self._error_response(
                "invalid_budget",
                request,
                f"Unsupported Agency Graph budget: {request.budget}",
                guidance=f"Use one of: {', '.join(sorted(SUPPORTED_BUDGETS))}.",
            )
        if request.preset and request.preset not in GRAPH_NEIGHBORHOOD_PRESETS:
            return self._error_response(
                "invalid_preset",
                request,
                f"Unsupported Agency Graph preset: {request.preset}",
                guidance=f"Use one of: {', '.join(sorted(GRAPH_NEIGHBORHOOD_PRESETS))}.",
            )
        if request.anchor_type and request.anchor_type not in SUPPORTED_ANCHORS:
            return self._error_response(
                "invalid_anchor",
                request,
                f"Unsupported Agency Graph anchor type: {request.anchor_type}",
                guidance=f"Use one of: {', '.join(sorted(SUPPORTED_ANCHORS))}.",
            )
        if bool(request.anchor_type) != bool(request.anchor_id):
            return self._error_response(
                "invalid_anchor",
                request,
                "Both anchor_type and anchor_id are required when anchoring graph context.",
                guidance="Provide both anchor fields or use a query-only graph search.",
            )
        if not request.anchor_id and not request.query:
            return self._error_response(
                "invalid_request",
                request,
                "Agency Graph context requires an anchor or a query.",
                guidance="Provide an anchor_type plus anchor_id, or provide a natural-language query.",
            )
        return None

    async def _load_graph_document(self, request: AgencyGraphContextRequest) -> GraphReadDocument:
        reader, close_after = resolve_graph_reader(self.context)
        try:
            documents: list[GraphReadDocument] = []
            if request.anchor_type and request.anchor_id:
                preset = request.preset or ANCHOR_PRESETS.get(request.anchor_type)
                if preset:
                    preset_config = GRAPH_NEIGHBORHOOD_PRESETS[preset]
                    documents.append(
                        await reader.get_neighborhood(
                            request.anchor_id,
                            labels=preset_config["labels"],
                            relationship_types=preset_config["relationship_types"],
                            depth=2,
                            limit=request.limit,
                        )
                    )
                else:
                    documents.append(await reader.get_neighborhood(request.anchor_id, depth=1, limit=request.limit))
                documents.extend(await self._load_relevant_path_documents(reader, request))
                return _merge_graph_documents(documents)
            documents.append(
                await reader.search_nodes(
                    request.query or "",
                    labels=_labels_from_scope(request.scope),
                    limit=request.limit,
                )
            )
            return _merge_graph_documents(documents)
        finally:
            await close_graph_reader_if_needed(reader, close_after)

    async def _load_relevant_path_documents(self, reader: Any, request: AgencyGraphContextRequest) -> list[
        GraphReadDocument]:
        if not request.anchor_type or not request.anchor_id:
            return []
        path_limit = min(max(request.limit, 1), BUDGET_LIMITS[request.budget]["edges"])
        path_documents: list[GraphReadDocument] = []
        if request.anchor_type in {"run", "execution"} and request.intent in {"debug", "root_cause", "resume", "steer"}:
            document = await _maybe_call_graph_reader(
                reader,
                "get_failed_run_root_cause_path",
                run_id=request.anchor_id,
                max_depth=3,
                limit=path_limit,
            )
            if document is not None:
                path_documents.append(document)
        if request.anchor_type in {"memory", "context_pack"}:
            document = await _maybe_call_graph_reader(
                reader,
                "get_memory_source_run_path",
                memory_id=request.anchor_id,
                max_depth=4,
                limit=path_limit,
            )
            if document is not None:
                path_documents.append(document)
        if request.anchor_type == "agent" and request.intent in {"debug", "handoff", "resume", "steer"}:
            document = await _maybe_call_graph_reader(
                reader,
                "get_agent_prior_runs_path",
                agent_id=request.anchor_id,
                max_depth=3,
                limit=path_limit,
            )
            if document is not None:
                path_documents.append(document)
        if request.anchor_type in {"document", "entity"}:
            document = await _maybe_call_graph_reader(
                reader,
                "get_influence_path",
                anchor_type=request.anchor_type,
                anchor_id=request.anchor_id,
                max_depth=4,
                limit=path_limit,
            )
            if document is not None:
                path_documents.append(document)
        return path_documents

    async def _filter_memory_nodes(
            self,
            request: AgencyGraphContextRequest,
            document: GraphReadDocument,
    ) -> tuple[GraphReadDocument, dict[str, MemoryRecord]]:
        memory_nodes = [node for node in document.nodes if _is_memory_node(node)]
        if not memory_nodes:
            return document, {}
        current_user = await self._current_user_from_scope(request.scope)
        memory_service = MemoryService(self.context)
        kept_node_ids: set[str] = set()
        linked_memories: dict[str, MemoryRecord] = {}
        omitted_memory_nodes = 0
        for node in memory_nodes:
            memory = await self._memory_for_node(node)
            if not await self._can_include_memory_node(
                    node,
                    memory,
                    current_user=current_user,
                    memory_service=memory_service,
                    scope=request.scope,
            ):
                omitted_memory_nodes += 1
                continue
            kept_node_ids.add(node.id)
            if memory is not None:
                linked_memories[node.id] = memory
        removed_memory_ids = {node.id for node in memory_nodes if node.id not in kept_node_ids}
        nodes = [
            _node_with_memory_record(node, linked_memories[node.id])
            if node.id in linked_memories
            else node
            for node in document.nodes
        ]
        if not removed_memory_ids:
            return GraphReadDocument(nodes=nodes, edges=document.edges, meta=dict(document.meta)), linked_memories
        nodes = [node for node in nodes if node.id not in removed_memory_ids]
        node_ids = {node.id for node in nodes}
        edges = [edge for edge in document.edges if edge.source in node_ids and edge.target in node_ids]
        meta = dict(document.meta)
        meta["memory_nodes_omitted_by_policy"] = int(
            meta.get("memory_nodes_omitted_by_policy") or 0) + omitted_memory_nodes
        return GraphReadDocument(nodes=nodes, edges=edges, meta=meta), linked_memories

    def _filter_protected_nodes(self, document: GraphReadDocument) -> GraphReadDocument:
        removed_node_ids = {node.id for node in document.nodes if _is_protected_credential_node(node)}
        if not removed_node_ids:
            return document
        nodes = [node for node in document.nodes if node.id not in removed_node_ids]
        node_ids = {node.id for node in nodes}
        edges = [edge for edge in document.edges if edge.source in node_ids and edge.target in node_ids]
        meta = dict(document.meta)
        meta["protected_nodes_omitted_by_policy"] = (
                int(meta.get("protected_nodes_omitted_by_policy") or 0) + len(removed_node_ids)
        )
        return GraphReadDocument(nodes=nodes, edges=edges, meta=meta)

    def _filter_scope_restricted_nodes(
            self,
            request: AgencyGraphContextRequest,
            document: GraphReadDocument,
    ) -> GraphReadDocument:
        scope = request.scope
        if not scope:
            return document
        removed_node_ids = {
            node.id
            for node in document.nodes
            if not _node_allowed_by_scope(node, scope)
        }
        if not removed_node_ids:
            return document
        nodes = [node for node in document.nodes if node.id not in removed_node_ids]
        node_ids = {node.id for node in nodes}
        edges = [edge for edge in document.edges if edge.source in node_ids and edge.target in node_ids]
        meta = dict(document.meta)
        meta["scope_nodes_omitted_by_policy"] = (
                int(meta.get("scope_nodes_omitted_by_policy") or 0) + len(removed_node_ids)
        )
        return GraphReadDocument(nodes=nodes, edges=edges, meta=meta)

    def _sanitize_document_chunk_nodes(
            self,
            request: AgencyGraphContextRequest,
            document: GraphReadDocument,
    ) -> GraphReadDocument:
        if _scope_allows_raw_document_chunks(request.scope) and request.budget == "raw_graph":
            return document
        sanitized_nodes: list[GraphReadNode] = []
        sanitized_count = 0
        for node in document.nodes:
            if not _is_document_chunk_node(node):
                sanitized_nodes.append(node)
                continue
            properties = dict(node.properties)
            before_keys = set(properties)
            for key in tuple(properties):
                if str(key).lower() in DOCUMENT_CHUNK_RAW_CONTENT_KEYS:
                    properties.pop(key, None)
            if before_keys != set(properties):
                properties["document_chunk_text_omitted_by_policy"] = True
                sanitized_count += 1
            sanitized_nodes.append(
                GraphReadNode(id=node.id, type=node.type, labels=list(node.labels), properties=properties)
            )
        if not sanitized_count:
            return document
        meta = dict(document.meta)
        meta["document_chunks_sanitized_by_policy"] = (
                int(meta.get("document_chunks_sanitized_by_policy") or 0) + sanitized_count
        )
        return GraphReadDocument(nodes=sanitized_nodes, edges=document.edges, meta=meta)

    async def _runtime_event_fallback(
            self,
            request: AgencyGraphContextRequest,
            document: GraphReadDocument,
    ) -> list[ExecutionEvent]:
        if not request.include_events or _projected_event_nodes(document.nodes):
            return []
        execution_id = _execution_id_from_request(request)
        if not execution_id:
            return []
        execution_store = getattr(self.context, "execution_store", None)
        list_events = getattr(execution_store, "list_events", None)
        if list_events is None:
            return []
        events = await list_events(execution_id)
        event_limit = BUDGET_LIMITS[request.budget]["events"]
        recent_events = sorted(events, key=lambda item: (item.sequence, item.timestamp, item.id), reverse=True)
        document.meta["runtime_events_fallback_used"] = bool(recent_events)
        document.meta["runtime_events_fallback_count"] = len(recent_events[:event_limit])
        return recent_events[:event_limit]

    async def _can_include_memory_node(
            self,
            node: GraphReadNode,
            memory: MemoryRecord | None,
            *,
            current_user: UserDefinition | None,
            memory_service: MemoryService,
            scope: dict[str, Any] | None,
    ) -> bool:
        if memory is None:
            return not _is_sensitive_node(node)
        if memory.sensitive and not _scope_allows_sensitive_memories(scope):
            return False
        if not await memory_service.can_read(memory, current_user=current_user):
            return False
        return not _memory_excluded_for_scope(memory, scope)

    async def _memory_for_node(self, node: GraphReadNode) -> MemoryRecord | None:
        memory_id = _node_memory_id(node)
        if not memory_id or not hasattr(self.context, "memory_repo"):
            return None
        return await self.context.memory_repo.get(memory_id)

    async def _current_user_from_scope(self, scope: dict[str, Any] | None) -> UserDefinition | None:
        user_id = _scope_user_id(scope)
        if not user_id or not hasattr(self.context, "user_repo"):
            return None
        return await self.context.user_repo.get(user_id)

    def _synthesize_context(
            self,
            request: AgencyGraphContextRequest,
            document: GraphReadDocument,
            *,
            linked_memories: dict[str, MemoryRecord] | None = None,
            runtime_events: list[ExecutionEvent] | None = None,
    ) -> dict[str, Any]:
        limits = BUDGET_LIMITS[request.budget]
        nodes = _rank_context_nodes(document.nodes, request)[: limits["nodes"]]
        edges = _rank_context_edges(document.edges, nodes)[: limits["edges"]]
        omitted_nodes = max(len(document.nodes) - len(nodes), 0)
        omitted_edges = max(len(document.edges) - len(edges), 0)
        facts = [*_node_facts(nodes), *_edge_facts(edges)]
        event_nodes = [node for node in nodes if node.type in {"ExecutionEvent", "ContainerEvent"}]
        runtime_event_context = [_event_context(event) for event in (runtime_events or [])[: limits["events"]]]
        related_memories = [
            _memory_context(node, (linked_memories or {}).get(node.id))
            for node in nodes
            if request.include_memories and node.type in {"Memory", "ContextPack"}
        ]
        response = {
            "status": "ok",
            "summary": self._summary(request, document),
            "facts": facts[: limits["nodes"] + limits["edges"]],
            "related_memories": related_memories,
            "run_summaries": [
                memory
                for memory in related_memories
                if memory.get("memory_type") == "run_summary"
                   or "run_summary" in {str(tag).lower() for tag in memory.get("tags", []) if isinstance(tag, str)}
            ],
            "related_documents": [
                _node_context(node) for node in nodes if node.type in {"Document", "DocumentChunk"}
            ],
            "recent_events": (
                [_node_context(node) for node in event_nodes[: limits["events"]]]
                if request.include_events and event_nodes
                else runtime_event_context
                if request.include_events
                else []
            ),
            "prior_attempts": [
                _node_context(node)
                for node in nodes
                if node.type in {"Run", "WorkflowRun", "StepRun"} and _node_status(node) in {"failed", "completed"}
            ],
            "prior_changes": [_node_context(node) for node in nodes if _is_change_node(node)],
            "failures": [_node_context(node) for node in nodes if _is_failure_node(node)],
            "decisions": [_node_context(node) for node in nodes if node.type == "Decision"],
            "constraints": [_node_context(node) for node in nodes if node.type == "Constraint"],
            "open_questions": [_node_context(node) for node in nodes if node.type == "OpenQuestion"],
            "next_actions": [_node_context(node) for node in nodes if node.type == "NextAction"],
            "provenance": {
                "nodes": [_node_provenance(node) for node in nodes],
                "edges": [_edge_provenance(edge) for edge in edges],
            },
            "omitted": {
                "nodes": omitted_nodes,
                "edges": omitted_edges,
                "reason": "budget" if omitted_nodes or omitted_edges else None,
            },
            "query_meta": {
                "intent": request.intent,
                "mode": request.mode,
                "scope": request.scope,
                "anchor_type": request.anchor_type,
                "anchor_id": request.anchor_id,
                "preset": request.preset,
                "query": request.query,
                "depth": _request_depth(request),
                "budget": request.budget,
                "limit": request.limit,
                "node_count": len(document.nodes),
                "edge_count": len(document.edges),
                "memory_nodes_omitted_by_policy": int(document.meta.get("memory_nodes_omitted_by_policy") or 0),
                "protected_nodes_omitted_by_policy": int(document.meta.get("protected_nodes_omitted_by_policy") or 0),
                "scope_nodes_omitted_by_policy": int(document.meta.get("scope_nodes_omitted_by_policy") or 0),
                "document_chunks_sanitized_by_policy": int(
                    document.meta.get("document_chunks_sanitized_by_policy") or 0
                ),
                "runtime_events_fallback_used": bool(document.meta.get("runtime_events_fallback_used")),
                "runtime_events_fallback_count": int(document.meta.get("runtime_events_fallback_count") or 0),
                "traversal_units": _request_traversal_units(request),
                "traversal_budget_max_units": _traversal_budget_max_units(),
                "traversal_budget_window_seconds": _traversal_budget_window_seconds(),
                "projection_available": True,
                "fallback_used": bool(document.meta.get("runtime_events_fallback_used")),
            },
        }
        if request.include_raw_graph or request.budget == "raw_graph":
            response["graph"] = {
                "nodes": [_safe_node_payload(node) for node in nodes],
                "edges": [_safe_edge_payload(edge) for edge in edges],
                "meta": _redact_value(document.meta),
            }
        else:
            response["graph"] = None
        if not document.nodes and not document.edges:
            response["status"] = "no_data"
            response["summary"] = (
                "No Agency Graph records matched this request. Try a more specific anchor, durable memory search, "
                "execution events, or wait for projection/backfill."
            )
            response["query_meta"]["guidance"] = (
                "Try a clearer anchor, durable memory search, execution events, or wait for projection/backfill."
            )
        return response

    def _summary(self, request: AgencyGraphContextRequest, document: GraphReadDocument) -> str:
        anchor = f"{request.anchor_type}:{request.anchor_id}" if request.anchor_type and request.anchor_id else None
        subject = anchor or f"query:{request.query}"
        return (
            f"Agency Graph context for {subject} using intent {request.intent}: "
            f"{len(document.nodes)} nodes and {len(document.edges)} edges."
        )

    def _unavailable_response(self, request: AgencyGraphContextRequest, *, status: str, guidance: str) -> dict[
        str, Any]:
        return self._error_response(status, request, "Agency Graph projection is unavailable.", guidance=guidance)

    def _budget_exceeded_response(
            self,
            request: AgencyGraphContextRequest,
            decision: _TraversalBudgetDecision,
    ) -> dict[str, Any]:
        response = self._error_response(
            "budget_exceeded",
            request,
            "Agency Graph traversal budget was exceeded.",
            guidance=(
                "Retry after the graph traversal budget window resets, or use a narrower anchor, lower limit, "
                "or smaller budget."
            ),
        )
        response["query_meta"].update(
            {
                "traversal_actor": decision.actor_key,
                "traversal_units": decision.units,
                "traversal_units_used": decision.used_units,
                "traversal_units_remaining": decision.remaining_units,
                "traversal_budget_max_units": decision.max_units,
                "traversal_budget_window_seconds": decision.window_seconds,
            }
        )
        return response

    def _graph_read_disabled_by_settings(self) -> bool:
        if getattr(self.context, "graph_read_service", None) is not None:
            return False
        return not get_settings().neo4j_enabled

    def _error_response(
            self,
            status: str,
            request: AgencyGraphContextRequest,
            message: str,
            *,
            guidance: str,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "summary": message,
            "facts": [],
            "related_memories": [],
            "related_documents": [],
            "recent_events": [],
            "prior_attempts": [],
            "failures": [],
            "decisions": [],
            "constraints": [],
            "open_questions": [],
            "next_actions": [],
            "provenance": {"nodes": [], "edges": []},
            "graph": None,
            "omitted": {"nodes": 0, "edges": 0, "reason": None},
            "query_meta": {
                "intent": request.intent,
                "mode": request.mode,
                "scope": request.scope,
                "anchor_type": request.anchor_type,
                "anchor_id": request.anchor_id,
                "preset": request.preset,
                "query": request.query,
                "depth": _request_depth(request),
                "budget": request.budget,
                "limit": request.limit,
                "node_count": 0,
                "edge_count": 0,
                "memory_nodes_omitted_by_policy": 0,
                "protected_nodes_omitted_by_policy": 0,
                "scope_nodes_omitted_by_policy": 0,
                "document_chunks_sanitized_by_policy": 0,
                "runtime_events_fallback_used": False,
                "runtime_events_fallback_count": 0,
                "traversal_units": _request_traversal_units(request),
                "traversal_budget_max_units": _traversal_budget_max_units(),
                "traversal_budget_window_seconds": _traversal_budget_window_seconds(),
                "projection_available": False,
                "fallback_used": False,
                "guidance": guidance,
            },
        }


def _node_facts(nodes: list[GraphReadNode]) -> list[str]:
    return [f"{node.type} {node.id}: {_node_label(node)}" for node in nodes]


def _edge_facts(edges) -> list[str]:
    return [f"{edge.source} {edge.type} {edge.target}" for edge in edges]


async def _maybe_call_graph_reader(reader: Any, method_name: str, **kwargs) -> GraphReadDocument | None:
    method = getattr(reader, method_name, None)
    if method is None:
        return None
    return await method(**kwargs)


def _merge_graph_documents(documents: list[GraphReadDocument]) -> GraphReadDocument:
    nodes_by_id: dict[str, GraphReadNode] = {}
    edges_by_id: dict[str, GraphReadEdge] = {}
    meta: dict[str, Any] = {}
    source_queries: list[dict[str, Any]] = []
    for document in documents:
        meta.update(document.meta)
        query = document.meta.get("query")
        if query:
            source_queries.append(
                {
                    "query": query,
                    "node_count": len(document.nodes),
                    "edge_count": len(document.edges),
                    "anchor_label": document.meta.get("anchor_label"),
                    "target_label": document.meta.get("target_label"),
                }
            )
        for node in document.nodes:
            nodes_by_id[node.id] = node
        for edge in document.edges:
            edges_by_id[edge.id] = edge
    if source_queries:
        meta["source_queries"] = source_queries
    return GraphReadDocument(nodes=list(nodes_by_id.values()), edges=list(edges_by_id.values()), meta=meta)


def _graph_document_from_payload(payload: dict[str, Any]) -> GraphReadDocument:
    raw_nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
    raw_edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
    raw_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    nodes = [_graph_node_from_payload(item) for item in raw_nodes[:100] if isinstance(item, dict)]
    node_ids = {node.id for node in nodes}
    edges = [
        edge
        for edge in (_graph_edge_from_payload(item) for item in raw_edges[:100] if isinstance(item, dict))
        if edge.source in node_ids and edge.target in node_ids
    ]
    meta = _redact_mapping(raw_meta)
    meta["query"] = "summarize_subgraph"
    meta["input_node_count"] = len(raw_nodes)
    meta["input_edge_count"] = len(raw_edges)
    return GraphReadDocument(nodes=nodes, edges=edges, meta=meta)


def _graph_node_from_payload(payload: dict[str, Any]) -> GraphReadNode:
    node_id = _string_or_none(payload.get("id")) or "unknown-node"
    node_type = _string_or_none(payload.get("type")) or "Unknown"
    labels = payload.get("labels") if isinstance(payload.get("labels"), list) else []
    properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
    return GraphReadNode(
        id=node_id,
        type=node_type,
        labels=[item.strip() for item in labels if isinstance(item, str) and item.strip()][:20],
        properties=_redact_mapping(properties),
    )


def _graph_edge_from_payload(payload: dict[str, Any]) -> GraphReadEdge:
    edge_id = _string_or_none(payload.get("id")) or (
        f"{_string_or_none(payload.get('source')) or 'unknown'}:"
        f"{_string_or_none(payload.get('type')) or 'RELATED'}:"
        f"{_string_or_none(payload.get('target')) or 'unknown'}"
    )
    properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
    return GraphReadEdge(
        id=edge_id,
        source=_string_or_none(payload.get("source")) or "",
        target=_string_or_none(payload.get("target")) or "",
        type=_string_or_none(payload.get("type")) or "RELATED",
        properties=_redact_mapping(properties),
    )


def _query_timeout_seconds() -> float:
    configured = float(get_settings().agency_graph_context_query_timeout_seconds or 5.0)
    return max(configured, 0.05)


async def _consume_traversal_budget(request: AgencyGraphContextRequest) -> _TraversalBudgetDecision:
    max_units = _traversal_budget_max_units()
    window_seconds = _traversal_budget_window_seconds()
    units = _request_traversal_units(request)
    actor_key = _traversal_actor_key(request)
    if max_units <= 0 or window_seconds <= 0:
        return _TraversalBudgetDecision(
            allowed=True,
            actor_key=actor_key,
            units=units,
            used_units=0,
            remaining_units=max_units,
            max_units=max_units,
            window_seconds=window_seconds,
        )

    now = monotonic()
    async with _GRAPH_CONTEXT_RATE_LOCK:
        window_started_at, used_units = _GRAPH_CONTEXT_RATE_STATE.get(actor_key, (now, 0))
        if now - window_started_at >= window_seconds:
            window_started_at = now
            used_units = 0
        if used_units + units > max_units:
            return _TraversalBudgetDecision(
                allowed=False,
                actor_key=actor_key,
                units=units,
                used_units=used_units,
                remaining_units=max(max_units - used_units, 0),
                max_units=max_units,
                window_seconds=window_seconds,
            )
        used_units += units
        _GRAPH_CONTEXT_RATE_STATE[actor_key] = (window_started_at, used_units)
        return _TraversalBudgetDecision(
            allowed=True,
            actor_key=actor_key,
            units=units,
            used_units=used_units,
            remaining_units=max(max_units - used_units, 0),
            max_units=max_units,
            window_seconds=window_seconds,
        )


def _request_traversal_units(request: AgencyGraphContextRequest) -> int:
    factor = BUDGET_TRAVERSAL_FACTORS.get(request.budget, BUDGET_TRAVERSAL_FACTORS["balanced"])
    depth = max(_request_depth(request), 1)
    return max(int(request.limit), 1) * depth * factor


def _traversal_actor_key(request: AgencyGraphContextRequest) -> str:
    user_id = _scope_user_id(request.scope)
    if user_id:
        return f"user:{user_id}"
    anchor = f"{request.anchor_type}:{request.anchor_id}" if request.anchor_type and request.anchor_id else None
    return f"anonymous:{anchor or request.query or 'global'}"


def _traversal_budget_max_units() -> int:
    return max(int(get_settings().agency_graph_context_rate_limit_max_units or 0), 0)


def _traversal_budget_window_seconds() -> float:
    return max(float(get_settings().agency_graph_context_rate_limit_window_seconds or 0), 0.0)


def _request_depth(request: AgencyGraphContextRequest) -> int:
    if not request.anchor_type or not request.anchor_id:
        return 0
    if request.anchor_type in ANCHOR_PRESETS:
        return 2
    return 1


def _node_context(node: GraphReadNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.type,
        "label": _node_label(node),
        "status": _node_status(node),
        "summary": _node_summary(node),
        "provenance": _node_provenance(node),
    }


def _memory_context(node: GraphReadNode, memory: MemoryRecord | None) -> dict[str, Any]:
    context = _node_context(node)
    if memory is None:
        return context
    context.update(
        {
            "memory_id": memory.id,
            "scope": memory.scope.value,
            "memory_type": memory.memory_type.value if memory.memory_type is not None else None,
            "importance": memory.importance,
            "tags": list(memory.tags),
            "status": memory.status.value,
        }
    )
    if not memory.sensitive:
        context["content_preview"] = MemoryService._preview(memory.content, limit=240)
    return context


def _event_context(event: ExecutionEvent) -> dict[str, Any]:
    payload = _redact_mapping(event.payload)
    summary = _event_summary(event, payload)
    return {
        "id": event.id,
        "type": "ExecutionEvent",
        "event_type": event.event_type.value,
        "label": event.event_type.value,
        "status": event.status,
        "summary": summary,
        "sequence": event.sequence,
        "timestamp": event.timestamp.isoformat(),
        "agent_id": event.agent_id,
        "task_id": event.task_id,
        "tool_call_id": event.tool_call_id,
        "payload": payload,
        "provenance": {
            "id": event.id,
            "type": "ExecutionEvent",
            "source_record_type": "execution_event",
            "source_record_id": event.id,
            "execution_id": event.execution_id,
            "workflow_id": event.workflow_id,
        },
    }


def _event_summary(event: ExecutionEvent, payload: dict[str, Any]) -> str:
    for key in ("message", "error", "summary", "status", "tool_name", "agent_name", "task_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(value.strip(), 160)
    if event.status:
        return f"{event.event_type.value} ({event.status})"
    return event.event_type.value


def _node_provenance(node: GraphReadNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.type,
        "source_record_type": node.properties.get("source_record_type"),
        "source_record_id": node.properties.get("source_record_id") or node.properties.get("id"),
    }


def _edge_provenance(edge) -> dict[str, Any]:
    return {
        "id": edge.id,
        "type": edge.type,
        "source": edge.source,
        "target": edge.target,
        "source_record_type": edge.properties.get("source_record_type"),
        "source_record_id": edge.properties.get("source_record_id"),
    }


def _node_label(node: GraphReadNode) -> str:
    if _is_protected_integration_node(node):
        return f"Protected {node.type} redacted"
    if _is_sensitive_node(node):
        return f"Sensitive {node.type} redacted"
    for key in ("name", "title", "summary", "label", "filename", "error", "message", "status"):
        value = node.properties.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(value.strip(), 160)
    return node.id


def _node_summary(node: GraphReadNode) -> str:
    status = _node_status(node)
    label = _node_label(node)
    if status:
        return f"{label} ({status})"
    return label


def _node_status(node: GraphReadNode) -> str | None:
    value = node.properties.get("status")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None


def _rank_context_nodes(nodes: list[GraphReadNode], request: AgencyGraphContextRequest) -> list[GraphReadNode]:
    indexed = list(enumerate(nodes))
    return [
        node
        for _, node in sorted(
            indexed,
            key=lambda item: (-_node_context_score(item[1], request), item[0]),
        )
    ]


def _rank_context_edges(edges: list[GraphReadEdge], nodes: list[GraphReadNode]) -> list[GraphReadEdge]:
    node_ids = {node.id for node in nodes}
    indexed = list(enumerate(edges))
    return [
        edge
        for _, edge in sorted(
            indexed,
            key=lambda item: (-_edge_context_score(item[1], node_ids), item[0]),
        )
    ]


def _node_context_score(node: GraphReadNode, request: AgencyGraphContextRequest) -> int:
    score = 100
    if request.anchor_id and node.id == request.anchor_id:
        score += 1000
    if _is_failure_node(node):
        score += 900
    if node.type == "Decision":
        score += 820
    elif node.type == "Constraint":
        score += 810
    elif node.type == "NextAction":
        score += 800
    elif node.type == "OpenQuestion":
        score += 790
    elif _is_run_summary_node(node):
        score += 770
    elif node.type in {"Memory", "ContextPack"}:
        score += 740
    elif _is_change_node(node):
        score += 720
    elif node.type in {"Workflow", "WorkflowRun", "Run", "StepRun", "Task", "Agent"}:
        score += 650
    elif node.type in {"ExecutionEvent", "ContainerEvent", "ToolCall"}:
        score += 620
    elif node.type in {"Document", "DocumentChunk", "Entity"}:
        score += 480
    return score


def _edge_context_score(edge: GraphReadEdge, node_ids: set[str]) -> int:
    score = 100
    if edge.source in node_ids and edge.target in node_ids:
        score += 600
    score += {
        "FAILED_WITH": 500,
        "CAPTURES_DECISION": 450,
        "CONSTRAINED_BY": 430,
        "SUGGESTS_NEXT_ACTION": 420,
        "LINKS_MEMORY": 390,
        "HAS_MEMORY_LINK": 390,
        "PRODUCED_ARTIFACT": 360,
        "HAS_RUN": 330,
        "HAS_STEP_RUN": 320,
        "ASSIGNED_TO": 310,
        "DEPENDS_ON": 300,
        "EMITTED_EVENT": 290,
        "CALLED_TOOL": 280,
        "MENTIONS": 220,
    }.get(edge.type, 0)
    return score


def _is_run_summary_node(node: GraphReadNode) -> bool:
    memory_type = node.properties.get("memory_type")
    if isinstance(memory_type, str) and memory_type.lower() == "run_summary":
        return True
    tags = node.properties.get("tags")
    if isinstance(tags, list):
        return "run_summary" in {str(tag).lower() for tag in tags}
    return False


def _is_failure_node(node: GraphReadNode) -> bool:
    if node.type == "Error":
        return True
    status = _node_status(node)
    if status in {"failed", "error", "errored"}:
        return True
    return bool(node.properties.get("error"))


def _is_change_node(node: GraphReadNode) -> bool:
    node_type = node.type.lower()
    labels = {label.lower() for label in node.labels}
    change_types = {
        "change",
        "codechange",
        "filechange",
        "diff",
        "commit",
        "pullrequest",
        "patch",
        "artifact",
    }
    if node_type in change_types or labels.intersection(change_types):
        return True
    return any(key in node.properties for key in ("diff_summary", "files_changed", "changed_files", "patch_summary"))


def _is_memory_node(node: GraphReadNode) -> bool:
    return node.type in {"Memory", "ContextPack"} or any(label in {"Memory", "ContextPack"} for label in node.labels)


def _node_memory_id(node: GraphReadNode) -> str | None:
    for key in ("memory_id", "source_record_id", "id"):
        value = _string_or_none(node.properties.get(key))
        if value:
            return value
    return node.id if node.id else None


def _node_with_memory_record(node: GraphReadNode, memory: MemoryRecord) -> GraphReadNode:
    properties = dict(node.properties)
    properties["memory_id"] = memory.id
    properties["memory_scope"] = memory.scope.value
    properties["memory_status"] = memory.status.value
    properties["memory_type"] = memory.memory_type.value if memory.memory_type is not None else None
    properties["importance"] = memory.importance
    properties["tags"] = list(memory.tags)
    properties["sensitive"] = memory.sensitive
    if not memory.sensitive:
        properties["summary"] = memory.summary or MemoryService._preview(memory.content, limit=160)
        properties["content_preview"] = MemoryService._preview(memory.content, limit=240)
    return GraphReadNode(
        id=node.id,
        type=node.type,
        labels=list(node.labels),
        properties=properties,
    )


def _projected_event_nodes(nodes: list[GraphReadNode]) -> list[GraphReadNode]:
    return [node for node in nodes if node.type in {"ExecutionEvent", "ContainerEvent"}]


def _execution_id_from_request(request: AgencyGraphContextRequest) -> str | None:
    if request.anchor_type in {"execution", "run"}:
        return request.anchor_id
    for key in ("execution_id", "run_id", "workflow_run_id"):
        value = _scope_value(request.scope, key)
        if value:
            return value
    return None


def _scope_user_id(scope: dict[str, Any] | None) -> str | None:
    if not scope:
        return None
    runtime_context = scope.get("runtime_context")
    if isinstance(runtime_context, dict):
        nested_user_id = _scope_user_id(runtime_context)
        if nested_user_id:
            return nested_user_id
    for key in ("current_user_id", "actor_user_id", "user_id", "created_by_user_id"):
        value = _string_or_none(scope.get(key))
        if value:
            return value
    return None


def _scope_allows_sensitive_memories(scope: dict[str, Any] | None) -> bool:
    if not scope:
        return False
    runtime_context = scope.get("runtime_context")
    if isinstance(runtime_context, dict) and _scope_allows_sensitive_memories(runtime_context):
        return True
    return bool(scope.get("include_sensitive_memories") is True or scope.get("allow_sensitive_memories") is True)


def _scope_allows_raw_document_chunks(scope: dict[str, Any] | None) -> bool:
    if not scope:
        return False
    runtime_context = scope.get("runtime_context")
    if isinstance(runtime_context, dict) and _scope_allows_raw_document_chunks(runtime_context):
        return True
    return bool(scope.get("include_raw_document_chunks") is True or scope.get("allow_raw_document_chunks") is True)


def _node_allowed_by_scope(node: GraphReadNode, scope: dict[str, Any]) -> bool:
    for property_key, scope_key in (
            ("workspace_id", "workspace_id"),
            ("workflow_id", "workflow_id"),
            ("conversation_id", "conversation_id"),
    ):
        allowed_value = _scope_value(scope, scope_key)
        node_value = _string_or_none(node.properties.get(property_key))
        if allowed_value and node_value and node_value != allowed_value:
            return False
    actor_user_id = _scope_user_id(scope)
    if actor_user_id:
        for property_key in ("created_by_user_id", "owner_user_id", "user_id"):
            node_user_id = _string_or_none(node.properties.get(property_key))
            if node_user_id and node_user_id != actor_user_id:
                return False
    return True


def _memory_excluded_for_scope(memory: MemoryRecord, scope: dict[str, Any] | None) -> bool:
    for target_type, target_id in _memory_exclusion_targets(scope):
        if MemoryService._matching_exclusions(memory, target_type=target_type, target_id=target_id):
            return True
    return False


def _memory_exclusion_targets(scope: dict[str, Any] | None) -> list[tuple[str | None, str | None]]:
    targets: list[tuple[str | None, str | None]] = [(None, None)]
    for key, target_type in (
            ("workflow_id", "workflow"),
            ("agent_id", "agent"),
            ("task_id", "task"),
            ("conversation_id", "conversation"),
            ("run_id", "run"),
            ("execution_id", "run"),
            ("workflow_run_id", "run"),
    ):
        value = _scope_value(scope, key)
        if value:
            targets.append((target_type, value))
    return targets


def _scope_value(scope: dict[str, Any] | None, key: str) -> str | None:
    if not scope:
        return None
    runtime_context = scope.get("runtime_context")
    if isinstance(runtime_context, dict):
        nested_value = _scope_value(runtime_context, key)
        if nested_value:
            return nested_value
    return _string_or_none(scope.get(key))


def _is_sensitive_node(node: GraphReadNode) -> bool:
    return bool(node.properties.get("sensitive") is True or node.properties.get("sensitivity") == "sensitive")


def _is_document_chunk_node(node: GraphReadNode) -> bool:
    labels = {str(label).lower() for label in node.labels}
    return node.type.lower() in {"documentchunk", "document_chunk"} or "documentchunk" in labels


def _is_protected_credential_node(node: GraphReadNode) -> bool:
    return _node_type_key(node) in PROTECTED_CREDENTIAL_NODE_TYPES


def _is_protected_integration_node(node: GraphReadNode) -> bool:
    return _node_type_key(node) in PROTECTED_INTEGRATION_NODE_TYPES


def _node_type_key(node: GraphReadNode) -> str:
    candidates = [node.type, *node.labels]
    for candidate in candidates:
        key = _normalized_node_type(candidate)
        if key in PROTECTED_CREDENTIAL_NODE_TYPES or key in PROTECTED_INTEGRATION_NODE_TYPES:
            return key
    return _normalized_node_type(node.type)


def _normalized_node_type(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(character for character in value.lower() if character.isalnum() or character == "_")


def _safe_node_payload(node: GraphReadNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.type,
        "labels": list(node.labels),
        "properties": _safe_node_properties(node),
    }


def _safe_edge_payload(edge) -> dict[str, Any]:
    return {
        "id": edge.id,
        "source": edge.source,
        "target": edge.target,
        "type": edge.type,
        "properties": _redact_mapping(edge.properties),
    }


def _safe_node_properties(node: GraphReadNode) -> dict[str, Any]:
    properties = _redact_mapping(node.properties)
    if _is_protected_integration_node(node):
        properties = {
            key: value
            for key, value in properties.items()
            if key.lower() in PROTECTED_NODE_PROPERTY_ALLOWLIST
        }
        properties["protected"] = True
        properties["redacted"] = True
        return properties
    if _is_sensitive_node(node):
        properties = {
            key: value
            for key, value in properties.items()
            if key.lower() not in SENSITIVE_NODE_DISPLAY_KEYS
        }
        properties["sensitive"] = True
    return properties


def _redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        if _is_sensitive_property_key(key):
            continue
        redacted[key] = _redact_value(item)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value[:100]]
    if isinstance(value, str):
        return _truncate(value, MAX_GRAPH_CONTEXT_STRING_LENGTH)
    return value


def _is_sensitive_property_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_PROPERTY_KEY_PARTS)


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _labels_from_scope(scope: dict[str, Any] | None) -> list[str] | None:
    if not scope:
        return None
    labels: list[str] = []
    for key in ("labels", "node_types", "types", "type", "label"):
        value = scope.get(key)
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, list):
            candidates = value
        else:
            continue
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                labels.append(candidate.strip())
    deduped = list(dict.fromkeys(labels))
    return deduped[:20] or None


def _anchor_from_scope(scope: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not scope:
        return None, None
    runtime_context = scope.get("runtime_context")
    if isinstance(runtime_context, dict):
        nested_anchor = _anchor_from_scope(runtime_context)
        if nested_anchor[0] and nested_anchor[1]:
            return nested_anchor
    for field, anchor_type in SCOPE_ANCHOR_FIELDS:
        anchor_id = _string_or_none(scope.get(field))
        if anchor_id:
            return anchor_type, anchor_id
    return None, None


def _bounded_int(value: Any, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(min(parsed, maximum), minimum)


def _truncate(value: str, max_length: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 1]}..."


__all__ = [
    "AgencyGraphBudget",
    "AgencyGraphContextRequest",
    "AgencyGraphContextService",
    "AgencyGraphIntent",
    "SUPPORTED_ANCHORS",
    "SUPPORTED_BUDGETS",
    "SUPPORTED_INTENTS",
]
