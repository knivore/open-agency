"""Neo4j graph read adapter returning Agency-neutral graph DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

MAX_GRAPH_READ_LIMIT = 1000
MAX_GRAPH_READ_DEPTH = 4

REDACTED_PROPERTY_KEYS = {
    "api_key",
    "api_token",
    "authorization",
    "content",
    "credential",
    "credentials",
    "embedding",
    "password",
    "private_key",
    "raw_content",
    "refresh_token",
    "secret",
    "secret_ref",
    "secret_reference",
    "token",
    "access_token",
    "bearer_token",
}

SAFE_GRAPH_METRIC_KEYS = {
    "token_count",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "budget_tokens",
    "used_tokens",
    "remaining_tokens",
}

REDACTED_PROPERTY_KEY_ALIASES = {
    "".join(character for character in key.lower() if character.isalnum())
    for key in REDACTED_PROPERTY_KEYS
}
SAFE_GRAPH_METRIC_KEY_ALIASES = {
    "".join(character for character in key.lower() if character.isalnum())
    for key in SAFE_GRAPH_METRIC_KEYS
}


@dataclass(slots=True)
class GraphReadConfig:
    database: str | None = None


@dataclass(slots=True)
class GraphReadNode:
    id: str
    type: str
    labels: list[str]
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "labels": self.labels,
            "properties": self.properties,
        }


@dataclass(slots=True)
class GraphReadEdge:
    id: str
    source: str
    target: str
    type: str
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "type": self.type,
            "properties": self.properties,
        }


@dataclass(slots=True)
class GraphReadDocument:
    nodes: list[GraphReadNode]
    edges: list[GraphReadEdge]
    meta: dict[str, Any] = field(default_factory=dict)
    operational_nodes: list[GraphReadNode] = field(default_factory=list)
    operational_edges: list[GraphReadEdge] = field(default_factory=list)
    operational_coverage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "meta": self.meta,
        }
        if self.operational_nodes or self.operational_edges or self.operational_coverage:
            payload["operational"] = {
                "nodes": [node.to_dict() for node in self.operational_nodes],
                "edges": [edge.to_dict() for edge in self.operational_edges],
                "coverage": self.operational_coverage,
            }
        return payload


class Neo4jGraphReadError(RuntimeError):
    """Raised when Neo4j graph read operations fail."""


class Neo4jGraphReader:
    """Read-only graph adapter for Agency Graph and observability consumers.

    The adapter deliberately returns normalized DTOs instead of exposing Neo4j
    records, nodes, paths, or relationship instances to route handlers.
    """

    def __init__(self, driver, *, config: GraphReadConfig | None = None):
        self.driver = driver
        self.config = config or GraphReadConfig()

    async def close(self) -> None:
        close = getattr(self.driver, "close", None)
        if close is not None:
            await close()

    async def ping(self) -> bool:
        await self._run("RETURN 1 AS ok", {})
        return True

    async def get_node(self, node_id: str, *, labels: Iterable[str] | None = None) -> GraphReadDocument:
        label_filter = _labels_predicate(labels, variable="n")
        cypher = f"""
        MATCH (n {{id: $node_id}})
        WHERE coalesce(n.deleted, false) = false{label_filter}
          {_access_predicate("n")}
        RETURN n
        LIMIT 1
        """
        records = await self._run(cypher, {"node_id": node_id, **self._access_params()})
        document = _document_from_records(records, expected_keys=("n",))
        document.meta.update({"query": "node", "node_id": node_id})
        return document

    async def get_neighborhood(
            self,
            node_id: str,
            *,
            labels: Iterable[str] | None = None,
            relationship_types: Iterable[str] | None = None,
            depth: int = 1,
            limit: int = 200,
            include_deleted: bool = False,
    ) -> GraphReadDocument:
        depth = _bounded_int(depth, minimum=1, maximum=MAX_GRAPH_READ_DEPTH)
        limit = _bounded_int(limit, minimum=1, maximum=MAX_GRAPH_READ_LIMIT)
        rel_pattern = _relationship_pattern(relationship_types, depth=depth)
        label_filter = _labels_predicate(labels, variable="center")
        deleted_filter = (
            ""
            if include_deleted
            else """
          AND all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
          AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
            """
        )
        cypher = f"""
        MATCH (center {{id: $node_id}})
        WHERE ($include_deleted OR coalesce(center.deleted, false) = false){label_filter}
          {_access_predicate("center")}
        MATCH p=(center){rel_pattern}(neighbor)
        WHERE ($include_deleted OR coalesce(neighbor.deleted, false) = false)
          {_access_predicate("neighbor")}
          {deleted_filter}
          AND all(node IN nodes(p) WHERE {_access_node_expression("node")})
        RETURN p
        LIMIT $limit
        """
        records = await self._run(
            cypher,
            {
                "node_id": node_id,
                "include_deleted": include_deleted,
                "limit": limit,
                **self._access_params(),
            },
        )
        document = _document_from_records(records, expected_keys=("p",))
        document.meta.update(
            {
                "query": "neighborhood",
                "node_id": node_id,
                "depth": depth,
                "limit": limit,
                "include_deleted": include_deleted,
            }
        )
        return document

    async def search_nodes(
            self,
            query: str | None = None,
            *,
            labels: Iterable[str] | None = None,
            node_types: Iterable[str] | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            tool_id: str | None = None,
            document_id: str | None = None,
            entity_id: str | None = None,
            error_text: str | None = None,
            limit: int = 50,
    ) -> GraphReadDocument:
        limit = _bounded_int(limit, minimum=1, maximum=MAX_GRAPH_READ_LIMIT)
        label_filter = _labels_predicate([*(labels or []), *(node_types or [])], variable="n")
        search_query = (query or "").lower().strip()
        error_query = (error_text or "").lower().strip()
        if not any(
                [
                    search_query,
                    error_query,
                    labels,
                    node_types,
                    workflow_id,
                    agent_id,
                    tool_id,
                    document_id,
                    entity_id,
                ]
        ):
            raise ValueError("Graph search requires a query, label/type, scope, or error_text filter")
        text_filter = (
            """
          AND (
            $query = ''
            OR toLower(coalesce(n.id, '')) CONTAINS $query
            OR toLower(coalesce(n.name, '')) CONTAINS $query
            OR toLower(coalesce(n.display_name, '')) CONTAINS $query
            OR toLower(coalesce(n.summary, '')) CONTAINS $query
            OR toLower(coalesce(n.filename, '')) CONTAINS $query
            OR toLower(coalesce(n.status, '')) CONTAINS $query
            OR toLower(coalesce(n.entity_type, '')) CONTAINS $query
            OR toLower(coalesce(n.message, '')) CONTAINS $query
            OR toLower(coalesce(n.error_message, '')) CONTAINS $query
          )
            """
            if search_query
            else ""
        )
        error_filter = (
            """
          AND (
            $error_text = ''
            OR n:Error
            OR n:ExecutionEvent
            OR n:ContainerEvent
          )
          AND (
            $error_text = ''
            OR toLower(coalesce(n.id, '')) CONTAINS $error_text
            OR toLower(coalesce(n.message, '')) CONTAINS $error_text
            OR toLower(coalesce(n.error_message, '')) CONTAINS $error_text
            OR toLower(coalesce(n.status, '')) CONTAINS $error_text
            OR toLower(coalesce(n.event_type, '')) CONTAINS $error_text
          )
            """
            if error_query
            else ""
        )
        cypher = f"""
        MATCH (n)
        WHERE coalesce(n.deleted, false) = false{label_filter}
          {_access_predicate("n")}
          {text_filter}
          {error_filter}
          AND (
            $workflow_id IS NULL
            OR n.id = $workflow_id
            OR n.workflow_id = $workflow_id
            OR n.defined_in_workflow_id = $workflow_id
            OR EXISTS {{
                MATCH (n)-[workflowRel*1..2]-(:Workflow {{id: $workflow_id}})
                WHERE all(rel IN workflowRel WHERE coalesce(rel.deleted, false) = false)
            }}
          )
          AND (
            $agent_id IS NULL
            OR n.id = $agent_id
            OR n.agent_id = $agent_id
            OR EXISTS {{
                MATCH (n)-[agentRel*1..2]-(:Agent {{id: $agent_id}})
                WHERE all(rel IN agentRel WHERE coalesce(rel.deleted, false) = false)
            }}
          )
          AND (
            $tool_id IS NULL
            OR n.id = $tool_id
            OR n.tool_id = $tool_id
            OR EXISTS {{
                MATCH (n)-[toolRel*1..2]-(:Tool {{id: $tool_id}})
                WHERE all(rel IN toolRel WHERE coalesce(rel.deleted, false) = false)
            }}
          )
          AND (
            $document_id IS NULL
            OR n.id = $document_id
            OR n.document_id = $document_id
            OR EXISTS {{
                MATCH (n)-[documentRel*1..2]-(:Document {{id: $document_id}})
                WHERE all(rel IN documentRel WHERE coalesce(rel.deleted, false) = false)
            }}
          )
          AND (
            $entity_id IS NULL
            OR n.id = $entity_id
            OR n.entity_id = $entity_id
            OR EXISTS {{
                MATCH (n)-[entityRel*1..2]-(:Entity {{id: $entity_id}})
                WHERE all(rel IN entityRel WHERE coalesce(rel.deleted, false) = false)
            }}
          )
        RETURN n
        LIMIT $limit
        """
        records = await self._run(
            cypher,
            {
                "query": search_query,
                "error_text": error_query,
                "workflow_id": workflow_id,
                "agent_id": agent_id,
                "tool_id": tool_id,
                "document_id": document_id,
                "entity_id": entity_id,
                "limit": limit,
                **self._access_params(),
            },
        )
        document = _document_from_records(records, expected_keys=("n",))
        document.meta.update(
            {
                "query": "search",
                "q": query,
                "labels": list(labels or []),
                "node_types": list(node_types or []),
                "workflow_id": workflow_id,
                "agent_id": agent_id,
                "tool_id": tool_id,
                "document_id": document_id,
                "entity_id": entity_id,
                "error_text": error_text,
                "limit": limit,
            }
        )
        return document

    async def get_shortest_path(
            self,
            source_id: str,
            target_id: str,
            *,
            relationship_types: Iterable[str] | None = None,
            max_depth: int = 4,
            limit: int = 1,
    ) -> GraphReadDocument:
        max_depth = _bounded_int(max_depth, minimum=1, maximum=MAX_GRAPH_READ_DEPTH)
        limit = _bounded_int(limit, minimum=1, maximum=MAX_GRAPH_READ_LIMIT)
        rel_pattern = _relationship_pattern(relationship_types, depth=max_depth)
        cypher = f"""
        MATCH (source {{id: $source_id}}), (target {{id: $target_id}})
        WHERE coalesce(source.deleted, false) = false
          AND coalesce(target.deleted, false) = false
          {_access_predicate("source")}
          {_access_predicate("target")}
        MATCH p = shortestPath((source){rel_pattern}(target))
        WHERE all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
          AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
          AND all(node IN nodes(p) WHERE {_access_node_expression("node")})
        RETURN p
        LIMIT $limit
        """
        records = await self._run(
            cypher,
            {"source_id": source_id, "target_id": target_id, "limit": limit, **self._access_params()},
        )
        document = _document_from_records(records, expected_keys=("p",))
        document.meta.update(
            {
                "query": "shortest_path",
                "source_id": source_id,
                "target_id": target_id,
                "relationship_types": list(relationship_types or []),
                "max_depth": max_depth,
                "limit": limit,
            }
        )
        return document

    async def get_memory_source_run_path(
            self,
            memory_id: str,
            *,
            run_id: str | None = None,
            max_depth: int = 4,
            limit: int = 25,
    ) -> GraphReadDocument:
        relationship_types = [
            "SOURCE_EXECUTION",
            "LINKS_MEMORY",
            "HAS_MEMORY_LINK",
            "HAS_RUN",
            "HAS_STEP_RUN",
            "EMITTED_EVENT",
            "CREATED_MEMORY",
        ]
        document = await self._get_label_path(
            query_name="memory_source_run_path",
            anchor_label="Memory",
            anchor_id=memory_id,
            target_label="WorkflowRun",
            target_id=run_id,
            relationship_types=relationship_types,
            max_depth=max_depth,
            limit=limit,
        )
        document.meta.update({"memory_id": memory_id, "run_id": run_id})
        return document

    async def get_failed_run_root_cause_path(
            self,
            run_id: str,
            *,
            max_depth: int = 3,
            limit: int = 25,
    ) -> GraphReadDocument:
        max_depth = _bounded_int(max_depth, minimum=1, maximum=MAX_GRAPH_READ_DEPTH)
        limit = _bounded_int(limit, minimum=1, maximum=MAX_GRAPH_READ_LIMIT)
        relationship_types = [
            "FAILED_WITH",
            "EMITTED_EVENT",
            "HAS_CONTEXT_HEALTH",
            "HAS_BUDGET_SIGNAL",
            "HAS_COMPACTION",
            "RAISED_FINDING",
            "RECORDED_USAGE",
            "OCCURRED_IN",
            "CALLED_TOOL",
        ]
        rel_pattern = _relationship_pattern(relationship_types, depth=max_depth)
        signal_labels = [
            "Error",
            "ExecutionEvent",
            "ContainerEvent",
            "ToolCall",
            "ContextHealth",
            "TokenBudget",
            "ContextCompaction",
            "MonitorFinding",
        ]
        cypher = f"""
        MATCH (run:WorkflowRun {{id: $run_id}})
        WHERE coalesce(run.deleted, false) = false
          {_access_predicate("run")}
        MATCH p=(run){rel_pattern}(signal)
        WHERE any(label IN labels(signal) WHERE label IN $signal_labels)
          AND all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
          AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
          AND all(node IN nodes(p) WHERE {_access_node_expression("node")})
        RETURN p
        ORDER BY length(p) ASC
        LIMIT $limit
        """
        records = await self._run(cypher, {"run_id": run_id, "signal_labels": signal_labels, "limit": limit,
                                           **self._access_params()})
        document = _document_from_records(records, expected_keys=("p",))
        document.meta.update(
            {
                "query": "failed_run_root_cause_path",
                "run_id": run_id,
                "relationship_types": relationship_types,
                "max_depth": max_depth,
                "limit": limit,
            }
        )
        return document

    async def get_influence_path(
            self,
            anchor_id: str,
            *,
            anchor_type: str,
            workflow_id: str | None = None,
            max_depth: int = 4,
            limit: int = 25,
    ) -> GraphReadDocument:
        anchor_labels = {"document": "Document", "entity": "Entity"}
        try:
            anchor_label = anchor_labels[anchor_type.lower()]
        except KeyError as exc:
            raise ValueError("Influence path anchor_type must be 'document' or 'entity'") from exc
        relationship_types = [
            "HAS_CHUNK",
            "PART_OF_DOCUMENT",
            "MENTIONS",
            "LINKS_MEMORY",
            "HAS_MEMORY_LINK",
            "AVAILABLE_TO",
            "SOURCE_EXECUTION",
            "HAS_RUN",
            "DEFINES_TASK",
            "DEFINES_AGENT",
        ]
        document = await self._get_label_path(
            query_name="influence_path",
            anchor_label=anchor_label,
            anchor_id=anchor_id,
            target_label="Workflow",
            target_id=workflow_id,
            relationship_types=relationship_types,
            max_depth=max_depth,
            limit=limit,
        )
        document.meta.update({"anchor_type": anchor_type.lower(), "anchor_id": anchor_id, "workflow_id": workflow_id})
        return document

    async def get_agent_prior_runs_path(
            self,
            agent_id: str,
            *,
            run_id: str | None = None,
            max_depth: int = 3,
            limit: int = 25,
    ) -> GraphReadDocument:
        relationship_types = [
            "PARTICIPATED_IN",
            "ASSIGNED_TO",
            "HAS_STEP_RUN",
            "HAS_RUN",
            "EMITTED_EVENT",
            "PERFORMED_BY",
            "DEFINES_AGENT",
        ]
        document = await self._get_label_path(
            query_name="agent_prior_runs_path",
            anchor_label="Agent",
            anchor_id=agent_id,
            target_label="WorkflowRun",
            target_id=run_id,
            relationship_types=relationship_types,
            max_depth=max_depth,
            limit=limit,
        )
        document.meta.update({"agent_id": agent_id, "run_id": run_id})
        return document

    async def get_workflow_lineage(self, workflow_id: str, *, limit: int = 300) -> GraphReadDocument:
        limit = _bounded_int(limit, minimum=1, maximum=MAX_GRAPH_READ_LIMIT)
        cypher = """
        MATCH (workflow:Workflow {id: $workflow_id})
        WHERE coalesce(workflow.deleted, false) = false
          AND ($access_user_id IS NULL OR true)
        OPTIONAL MATCH p=(workflow)-[:HAS_RUN|HAS_STEP_RUN|LINKS_MEMORY|HAS_CHUNK*1..3]-(related)
        WHERE (related IS NULL OR coalesce(related.deleted, false) = false)
          AND (p IS NULL OR all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false))
          AND (related IS NULL OR """ + _access_node_expression("related") + """)
          AND (p IS NULL OR all(node IN nodes(p) WHERE """ + _access_node_expression("node") + """))
        RETURN workflow, p
        LIMIT $limit
        """
        records = await self._run(cypher, {"workflow_id": workflow_id, "limit": limit, **self._access_params()})
        document = _document_from_records(records, expected_keys=("workflow", "p"))
        document.meta.update({"query": "workflow_lineage", "workflow_id": workflow_id, "limit": limit})
        return document

    async def get_graph_preset(
            self,
            preset: str,
            *,
            workflow_id: str | None = None,
            run_id: str | None = None,
            memory_id: str | None = None,
            agent_id: str | None = None,
            tool_id: str | None = None,
            persona_id: str | None = None,
            device_id: str | None = None,
            room: str | None = None,
            limit: int = 50,
    ) -> GraphReadDocument:
        limit = _bounded_int(limit, minimum=1, maximum=MAX_GRAPH_READ_LIMIT)
        preset_key = preset.lower().strip().replace("-", "_")
        if preset_key == "failed_run_root_cause":
            if not run_id:
                raise ValueError("Graph preset 'failed_run_root_cause' requires run_id")
            document = await self.get_failed_run_root_cause_path(run_id, limit=limit)
            document.meta.update({"query": "graph_preset", "preset": preset_key})
            return document
        if preset_key == "workflow_lineage":
            if not workflow_id:
                raise ValueError("Graph preset 'workflow_lineage' requires workflow_id")
            document = await self.get_workflow_lineage(workflow_id, limit=limit)
            document.meta.update({"query": "graph_preset", "preset": preset_key})
            return document
        if preset_key == "memory_provenance":
            if not memory_id:
                raise ValueError("Graph preset 'memory_provenance' requires memory_id")
            document = await self.get_memory_source_run_path(memory_id, run_id=run_id, limit=limit)
            document.meta.update({"query": "graph_preset", "preset": preset_key})
            return document
        if preset_key == "sub_agent_steering":
            if not agent_id:
                raise ValueError("Graph preset 'sub_agent_steering' requires agent_id")
            document = await self.get_agent_prior_runs_path(agent_id, run_id=run_id, limit=limit)
            document.meta.update({"query": "graph_preset", "preset": preset_key})
            return document
        if preset_key in {"persona_lineage", "persona_capability_map"} and not persona_id:
            raise ValueError(f"Graph preset '{preset_key}' requires persona_id")
        cypher, params, expected_keys = _graph_preset_query(
            preset_key,
            workflow_id=workflow_id,
            run_id=run_id,
            agent_id=agent_id,
            tool_id=tool_id,
            persona_id=persona_id,
            device_id=device_id,
            room=room,
            limit=limit,
        )
        params.update(self._access_params())
        records = await self._run(cypher, params)
        document = _document_from_records(records, expected_keys=expected_keys)
        document.meta.update(
            {
                "query": "graph_preset",
                "preset": preset_key,
                "workflow_id": workflow_id,
                "run_id": run_id,
                "memory_id": memory_id,
                "agent_id": agent_id,
                "tool_id": tool_id,
                "persona_id": persona_id,
                "device_id": device_id,
                "room": room,
                "limit": limit,
            }
        )
        return document

    async def _get_label_path(
            self,
            *,
            query_name: str,
            anchor_label: str,
            anchor_id: str,
            target_label: str,
            target_id: str | None,
            relationship_types: Iterable[str],
            max_depth: int,
            limit: int,
    ) -> GraphReadDocument:
        max_depth = _bounded_int(max_depth, minimum=1, maximum=MAX_GRAPH_READ_DEPTH)
        limit = _bounded_int(limit, minimum=1, maximum=MAX_GRAPH_READ_LIMIT)
        safe_anchor_label = _safe_identifier(anchor_label)
        safe_target_label = _safe_identifier(target_label)
        rel_pattern = _relationship_pattern(relationship_types, depth=max_depth)
        cypher = f"""
        MATCH (anchor:{safe_anchor_label} {{id: $anchor_id}})
        WHERE coalesce(anchor.deleted, false) = false
          {_access_predicate("anchor")}
        MATCH p=(anchor){rel_pattern}(target:{safe_target_label})
        WHERE ($target_id IS NULL OR target.id = $target_id)
          AND coalesce(target.deleted, false) = false
          {_access_predicate("target")}
          AND all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
          AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
          AND all(node IN nodes(p) WHERE {_access_node_expression("node")})
        RETURN p
        ORDER BY length(p) ASC
        LIMIT $limit
        """
        records = await self._run(
            cypher,
            {"anchor_id": anchor_id, "target_id": target_id, "limit": limit, **self._access_params()},
        )
        document = _document_from_records(records, expected_keys=("p",))
        document.meta.update(
            {
                "query": query_name,
                "anchor_label": safe_anchor_label,
                "target_label": safe_target_label,
                "target_id": target_id,
                "relationship_types": list(relationship_types),
                "max_depth": max_depth,
                "limit": limit,
            }
        )
        return document

    async def _run(self, cypher: str, params: dict[str, Any]) -> list[Any]:
        session_kwargs = {}
        if self.config.database:
            session_kwargs["database"] = self.config.database
        try:
            async with self.driver.session(**session_kwargs) as session:
                result = await session.run(cypher, **params)
                return await _collect_records(result)
        except Exception as exc:
            raise Neo4jGraphReadError(str(exc)) from exc

    def _access_params(self) -> dict[str, Any]:
        return {"access_user_id": getattr(self, "access_user_id", None)}


async def _collect_records(result: Any) -> list[Any]:
    if isinstance(result, list):
        return result
    records: list[Any] = []
    if hasattr(result, "__aiter__"):
        async for record in result:
            records.append(record)
        return records
    values = getattr(result, "values", None)
    if values is not None:
        maybe_values = values()
        if hasattr(maybe_values, "__await__"):
            maybe_values = await maybe_values
        return list(maybe_values)
    return list(result or [])


def _document_from_records(records: Iterable[Any], *, expected_keys: tuple[str, ...]) -> GraphReadDocument:
    nodes_by_id: dict[str, GraphReadNode] = {}
    edges_by_id: dict[str, GraphReadEdge] = {}
    for record in records:
        for key in expected_keys:
            value = _record_get(record, key)
            if value is None:
                continue
            _collect_value(value, nodes_by_id=nodes_by_id, edges_by_id=edges_by_id)
    return GraphReadDocument(nodes=list(nodes_by_id.values()), edges=list(edges_by_id.values()))


def _collect_value(value: Any, *, nodes_by_id: dict[str, GraphReadNode], edges_by_id: dict[str, GraphReadEdge]) -> None:
    if _looks_like_path(value):
        for node in _path_nodes(value):
            _add_node(node, nodes_by_id)
        for relationship in _path_relationships(value):
            _add_edge(relationship, nodes_by_id=nodes_by_id, edges_by_id=edges_by_id)
        return
    if _looks_like_relationship(value):
        _add_edge(value, nodes_by_id=nodes_by_id, edges_by_id=edges_by_id)
        return
    if _looks_like_node(value):
        _add_node(value, nodes_by_id)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_value(item, nodes_by_id=nodes_by_id, edges_by_id=edges_by_id)


def _add_node(node: Any, nodes_by_id: dict[str, GraphReadNode]) -> str:
    properties = _safe_properties(node)
    node_id = str(properties.get("id") or getattr(node, "element_id", None) or getattr(node, "id", ""))
    if not node_id:
        node_id = str(hash(repr(properties)))
    labels = sorted(str(label) for label in getattr(node, "labels", []) or [])
    if not labels and isinstance(node, dict):
        labels = sorted(str(label) for label in node.get("labels", []) or [])
    node_type = labels[0] if labels else "Node"
    nodes_by_id[node_id] = GraphReadNode(
        id=node_id,
        type=node_type,
        labels=labels,
        properties={key: value for key, value in properties.items() if key != "id"},
    )
    return node_id


def _add_edge(
        relationship: Any,
        *,
        nodes_by_id: dict[str, GraphReadNode],
        edges_by_id: dict[str, GraphReadEdge],
) -> None:
    source_node = getattr(relationship, "start_node", None)
    target_node = getattr(relationship, "end_node", None)
    source = _add_node(source_node, nodes_by_id) if source_node is not None else str(
        _mapping_get(relationship, "source", ""))
    target = _add_node(target_node, nodes_by_id) if target_node is not None else str(
        _mapping_get(relationship, "target", ""))
    if not source or not target:
        return
    rel_type = str(getattr(relationship, "type", None) or _mapping_get(relationship, "type", "RELATED_TO"))
    properties = _safe_properties(relationship)
    rel_identity = properties.get("id") or properties.get("link_id") or getattr(relationship, "element_id", None)
    edge_id = str(rel_identity or f"{source}:{rel_type}:{target}")
    edges_by_id[edge_id] = GraphReadEdge(
        id=edge_id,
        source=source,
        target=target,
        type=rel_type,
        properties={key: value for key, value in properties.items() if key != "id"},
    )


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


def _safe_properties(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        raw = value.get("properties", value)
    else:
        try:
            raw = dict(value)
        except (TypeError, ValueError):
            items = getattr(value, "items", None)
            raw = dict(items()) if items is not None else {}
    return {
        str(key): _json_safe(value)
        for key, value in raw.items()
        if not _should_redact_property_key(key)
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items() if not _should_redact_property_key(key)}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _should_redact_property_key(key: Any) -> bool:
    # Graph reads must hide secret-bearing aliases such as accessToken or secretRef
    # while preserving non-secret metrics like token_count for debugging and cost views.
    normalized = "".join(character for character in str(key).lower() if character.isalnum())
    if normalized in SAFE_GRAPH_METRIC_KEY_ALIASES:
        return False
    return normalized in REDACTED_PROPERTY_KEY_ALIASES


def _labels_predicate(labels: Iterable[str] | None, *, variable: str) -> str:
    sanitized = [_safe_identifier(label) for label in labels or [] if label]
    if not sanitized:
        return ""
    allowed = ", ".join(f"'{label}'" for label in sanitized)
    return f" AND any(label IN labels({variable}) WHERE label IN [{allowed}])"


def _relationship_pattern(relationship_types: Iterable[str] | None, *, depth: int) -> str:
    sanitized = [_safe_identifier(rel_type) for rel_type in relationship_types or [] if rel_type]
    if sanitized:
        rel_types = "|".join(sanitized)
        return f"-[:{rel_types}*1..{depth}]-"
    return f"-[*1..{depth}]-"


def _safe_identifier(value: str) -> str:
    normalized = "".join(character for character in str(value) if character.isalnum() or character == "_")
    if not normalized:
        raise ValueError("Graph labels and relationship types must include at least one identifier character")
    return normalized


def _bounded_int(value: int, *, minimum: int, maximum: int) -> int:
    return max(min(int(value), maximum), minimum)


def _looks_like_path(value: Any) -> bool:
    return hasattr(value, "nodes") and hasattr(value, "relationships")


def _looks_like_node(value: Any) -> bool:
    return value is not None and (hasattr(value, "labels") or (isinstance(value, dict) and "labels" in value))


def _looks_like_relationship(value: Any) -> bool:
    return value is not None and (
            hasattr(value, "start_node")
            or hasattr(value, "end_node")
            or (isinstance(value, dict) and {"source", "target"}.issubset(value))
    )


def _path_nodes(path: Any) -> Iterable[Any]:
    return getattr(path, "nodes", []) or []


def _path_relationships(path: Any) -> Iterable[Any]:
    return getattr(path, "relationships", []) or []


def _mapping_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _access_predicate(variable: str) -> str:
    return f"AND {_access_node_expression(variable)}"


def _access_node_expression(variable: str) -> str:
    return (
        "$access_user_id IS NULL "
        f"OR NOT ({variable}:Memory OR {variable}:Document) "
        f"OR coalesce({variable}.created_by_user_id, {variable}.owner_user_id) IS NULL "
        f"OR {variable}.created_by_user_id = $access_user_id "
        f"OR {variable}.owner_user_id = $access_user_id"
    )


def _with_access(template: str, **expressions: str) -> str:
    for key, value in expressions.items():
        template = template.replace(f"__{key}__", value)
    return template


def _graph_preset_query(
        preset: str,
        *,
        workflow_id: str | None,
        run_id: str | None,
        agent_id: str | None,
        tool_id: str | None,
        persona_id: str | None,
        device_id: str | None,
        room: str | None,
        limit: int,
) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    params: dict[str, Any] = {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "agent_id": agent_id,
        "tool_id": tool_id,
        "persona_id": persona_id,
        "device_id": device_id,
        "room": room,
        "limit": limit,
        "access_user_id": None,
    }
    if preset == "physical_device_audit":
        if not device_id:
            raise ValueError("Graph preset 'physical_device_audit' requires device_id")
        return (
            _with_access(
                """
            MATCH (device:Device {id: $device_id})
            WHERE coalesce(device.deleted, false) = false
              AND (__DEVICE_ACCESS__)
            OPTIONAL MATCH p=(device)-[:LOCATED_IN|TARGETS_DEVICE|REQUESTED_DEVICE_COMMAND|INFLUENCED_DEVICE_COMMAND|EMITTED_DEVICE_EVENT|CORRELATES_WITH_COMMAND*1..3]-(related)
            WHERE p IS NULL OR (
              all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
              AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
              AND all(node IN nodes(p) WHERE __NODE_ACCESS__)
            )
            RETURN device, p
            ORDER BY coalesce(related.updated_at, related.timestamp, related.created_at, device.updated_at, '') DESC
            LIMIT $limit
            """,
                DEVICE_ACCESS=_access_node_expression("device"),
                NODE_ACCESS=_access_node_expression("node"),
            ),
            params,
            ("device", "p"),
        )
    if preset == "physical_room_context":
        if not room:
            raise ValueError("Graph preset 'physical_room_context' requires room")
        return (
            _with_access(
                """
            MATCH (room:Room)
            WHERE coalesce(room.deleted, false) = false
              AND (__ROOM_ACCESS__)
              AND (toLower(room.name) = toLower($room) OR toLower(room.id) = toLower($room))
            OPTIONAL MATCH p=(room)-[:LOCATED_IN|TARGETS_DEVICE|REQUESTED_DEVICE_COMMAND|INFLUENCED_DEVICE_COMMAND|EMITTED_DEVICE_EVENT|CORRELATES_WITH_COMMAND*1..3]-(related)
            WHERE p IS NULL OR (
              all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
              AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
              AND all(node IN nodes(p) WHERE __NODE_ACCESS__)
            )
            RETURN room, p
            ORDER BY coalesce(related.updated_at, related.timestamp, related.created_at, room.updated_at, '') DESC
            LIMIT $limit
            """,
                ROOM_ACCESS=_access_node_expression("room"),
                NODE_ACCESS=_access_node_expression("node"),
            ),
            params,
            ("room", "p"),
        )
    if preset == "recent_failures":
        return (
            _with_access(
                """
            MATCH (run:WorkflowRun)
            WHERE coalesce(run.deleted, false) = false
              AND (__RUN_ACCESS__)
              AND ($workflow_id IS NULL OR run.workflow_id = $workflow_id OR EXISTS {
                MATCH (:Workflow {id: $workflow_id})-[workflowRel:HAS_RUN|STARTED]->(run)
                WHERE coalesce(workflowRel.deleted, false) = false
              })
              AND toLower(coalesce(run.status, '')) IN ['failed', 'error', 'cancelled', 'timed_out']
            OPTIONAL MATCH p=(run)-[:FAILED_WITH|EMITTED_EVENT|HAS_CONTEXT_HEALTH|HAS_BUDGET_SIGNAL|RAISED_FINDING*1..2]-(signal)
            WHERE p IS NULL OR (
              all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
              AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
              AND all(node IN nodes(p) WHERE __NODE_ACCESS__)
            )
            RETURN run, p
            ORDER BY coalesce(run.completed_at, run.updated_at, run.started_at, '') DESC
            LIMIT $limit
            """,
                RUN_ACCESS=_access_node_expression("run"),
                NODE_ACCESS=_access_node_expression("node"),
            ),
            params,
            ("run", "p"),
        )
    if preset == "stale_context":
        return (
            _with_access(
                """
            MATCH (n)
            WHERE coalesce(n.deleted, false) = false
              AND (__NODE_ACCESS__)
              AND (
                coalesce(n.stale, false) = true
                OR coalesce(n.missing_embedding, false) = true
                OR (n:ContextHealth AND toLower(coalesce(n.status, '')) IN ['warning', 'critical', 'stale'])
              )
              AND ($workflow_id IS NULL OR n.workflow_id = $workflow_id OR EXISTS {
                MATCH (n)-[workflowRel*1..2]-(:Workflow {id: $workflow_id})
                WHERE all(rel IN workflowRel WHERE coalesce(rel.deleted, false) = false)
              })
            RETURN n
            ORDER BY coalesce(n.updated_at, n.last_seen_at, '') DESC
            LIMIT $limit
            """,
                NODE_ACCESS=_access_node_expression("n"),
            ),
            params,
            ("n",),
        )
    if preset == "missing_embeddings":
        return (
            _with_access(
                """
            MATCH (n)
            WHERE coalesce(n.deleted, false) = false
              AND (__NODE_ACCESS__)
              AND (n:Memory OR n:Document)
              AND coalesce(n.missing_embedding, false) = true
              AND ($workflow_id IS NULL OR n.workflow_id = $workflow_id OR EXISTS {
                MATCH (n)-[workflowRel*1..2]-(:Workflow {id: $workflow_id})
                WHERE all(rel IN workflowRel WHERE coalesce(rel.deleted, false) = false)
              })
            RETURN n
            ORDER BY coalesce(n.updated_at, '') DESC
            LIMIT $limit
            """,
                NODE_ACCESS=_access_node_expression("n"),
            ),
            params,
            ("n",),
        )
    if preset == "high_cost_runs":
        return (
            _with_access(
                """
            MATCH p=(run:WorkflowRun)-[:RECORDED_USAGE|HAS_BUDGET_SIGNAL|USED_MODEL|HAS_CONTEXT_HEALTH*1..2]-(signal)
            WHERE coalesce(run.deleted, false) = false
              AND (__RUN_ACCESS__)
              AND ($workflow_id IS NULL OR run.workflow_id = $workflow_id)
              AND all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
              AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
              AND all(node IN nodes(p) WHERE __NODE_ACCESS__)
              AND (
                coalesce(signal.estimated_cost, 0) > 0
                OR coalesce(signal.usage_ratio, 0) >= 1
                OR toLower(coalesce(signal.status, '')) IN ['warning', 'exceeded', 'critical']
              )
            RETURN p
            ORDER BY coalesce(signal.estimated_cost, signal.usage_ratio, 0) DESC
            LIMIT $limit
            """,
                RUN_ACCESS=_access_node_expression("run"),
                NODE_ACCESS=_access_node_expression("node"),
            ),
            params,
            ("p",),
        )
    if preset == "tool_failure_hotspots":
        return (
            _with_access(
                """
            MATCH p=(tool:Tool)-[:CALLED_TOOL|USES_TOOL|CAN_USE|OCCURRED_IN|EMITTED_EVENT|FAILED_WITH*1..3]-(signal)
            WHERE coalesce(tool.deleted, false) = false
              AND (__TOOL_ACCESS__)
              AND ($tool_id IS NULL OR tool.id = $tool_id)
              AND all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
              AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
              AND all(node IN nodes(p) WHERE __NODE_ACCESS__)
              AND (
                signal:Error
                OR toLower(coalesce(signal.status, '')) IN ['failed', 'error']
                OR toLower(coalesce(signal.event_type, '')) CONTAINS 'failed'
              )
            RETURN p
            ORDER BY coalesce(signal.updated_at, signal.occurred_at, '') DESC
            LIMIT $limit
            """,
                TOOL_ACCESS=_access_node_expression("tool"),
                NODE_ACCESS=_access_node_expression("node"),
            ),
            params,
            ("p",),
        )
    if preset == "coding_agent_resume":
        return (
            _with_access(
                """
            MATCH p=(anchor)-[:HAS_RUN|HAS_STEP_RUN|PARTICIPATED_IN|ASSIGNED_TO|USES_TOOL|LINKS_MEMORY|HAS_MEMORY_LINK|SOURCE_EXECUTION|EMITTED_EVENT|FAILED_WITH*1..3]-(related)
            WHERE coalesce(anchor.deleted, false) = false
              AND (__ANCHOR_ACCESS__)
              AND coalesce(related.deleted, false) = false
              AND (__RELATED_ACCESS__)
              AND ($workflow_id IS NULL OR anchor.id = $workflow_id OR anchor.workflow_id = $workflow_id OR EXISTS {
                MATCH (anchor)-[workflowRel*1..2]-(:Workflow {id: $workflow_id})
                WHERE all(rel IN workflowRel WHERE coalesce(rel.deleted, false) = false)
              })
              AND ($agent_id IS NULL OR anchor.id = $agent_id OR anchor.agent_id = $agent_id OR EXISTS {
                MATCH (anchor)-[agentRel*1..2]-(:Agent {id: $agent_id})
                WHERE all(rel IN agentRel WHERE coalesce(rel.deleted, false) = false)
              })
              AND any(label IN labels(anchor) WHERE label IN ['Workflow', 'WorkflowRun', 'StepRun', 'Task', 'Agent', 'Memory'])
              AND all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
              AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
              AND all(node IN nodes(p) WHERE __NODE_ACCESS__)
            RETURN p
            ORDER BY coalesce(anchor.updated_at, related.updated_at, '') DESC
            LIMIT $limit
            """,
                ANCHOR_ACCESS=_access_node_expression("anchor"),
                RELATED_ACCESS=_access_node_expression("related"),
                NODE_ACCESS=_access_node_expression("node"),
            ),
            params,
            ("p",),
        )
    if preset == "persona_lineage":
        return (
            _with_access(
                """
            MATCH (persona:Persona {id: $persona_id})
            WHERE coalesce(persona.deleted, false) = false
              AND (__PERSONA_ACCESS__)
            OPTIONAL MATCH p=(persona)-[:PERSONA_HAS_DISTILLATION_RUN|RUN_EXTRACTED_ITEM|ITEM_DERIVED_FROM_MEMORY|RUN_USED_SOURCE_MEMORY|PERSONA_HAS_VERSION|RUN_PRODUCED_VERSION|PERSONA_PUBLISHED_MEMORY|PERSONA_MATERIALIZED_AS_AGENT|PERSONA_INVOKED_IN_CONVERSATION|MENTIONS|KNOWS|USES|FOLLOWS|PRODUCES|REVIEWS|APPROVES|ESCALATES_TO|PARTICIPATES_IN|DERIVED_FROM|RELATES_TO*1..4]-(related)
            WHERE p IS NULL OR (
              all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
              AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
              AND all(node IN nodes(p) WHERE __NODE_ACCESS__)
            )
            RETURN persona, p
            ORDER BY coalesce(related.updated_at, persona.updated_at, '') DESC
            LIMIT $limit
            """,
                PERSONA_ACCESS=_access_node_expression("persona"),
                NODE_ACCESS=_access_node_expression("node"),
            ),
            params,
            ("persona", "p"),
        )
    if preset == "persona_capability_map":
        return (
            _with_access(
                """
            MATCH (persona:Persona {id: $persona_id})
            WHERE coalesce(persona.deleted, false) = false
              AND (__PERSONA_ACCESS__)
            OPTIONAL MATCH p=(persona)-[:PERSONA_USES_TOOL|PERSONA_FOLLOWS_WORKFLOW|PERSONA_PRODUCES_ARTIFACT|PERSONA_HAS_VERSION|PERSONA_PUBLISHED_MEMORY|MENTIONS|KNOWS|USES|FOLLOWS|PRODUCES|REVIEWS|APPROVES|ESCALATES_TO|PARTICIPATES_IN|DERIVED_FROM|RELATES_TO*1..3]-(capability)
            WHERE p IS NULL OR (
              all(node IN nodes(p) WHERE coalesce(node.deleted, false) = false)
              AND all(rel IN relationships(p) WHERE coalesce(rel.deleted, false) = false)
              AND all(node IN nodes(p) WHERE __NODE_ACCESS__)
            )
            RETURN persona, p
            ORDER BY coalesce(capability.updated_at, persona.updated_at, '') DESC
            LIMIT $limit
            """,
                PERSONA_ACCESS=_access_node_expression("persona"),
                NODE_ACCESS=_access_node_expression("node"),
            ),
            params,
            ("persona", "p"),
        )
    raise ValueError(f"Unknown graph preset: {preset}")


__all__ = [
    "GraphReadConfig",
    "GraphReadDocument",
    "GraphReadEdge",
    "GraphReadNode",
    "MAX_GRAPH_READ_DEPTH",
    "MAX_GRAPH_READ_LIMIT",
    "Neo4jGraphReadError",
    "Neo4jGraphReader",
]
