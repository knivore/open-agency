from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from app.api.context import create_test_api_context
from app.core.config import get_settings, reset_settings_cache
from app.domain import (
    AgentDefinition,
    ContextCompactionRecord,
    Execution,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionEventType,
    GraphContextSettings,
    MemoryRecord,
    MemoryScope,
    MemoryType,
    TaskDefinition,
    UserDefinition,
    WorkflowDefinition,
    WorkflowNodeDefinition,
)
from app.graph.neo4j_read import GraphReadDocument, GraphReadEdge, GraphReadNode
from app.graph.service import (
    GRAPH_NEIGHBORHOOD_MODES,
    GRAPH_NEIGHBORHOOD_PRESETS,
    GraphReadUnavailableError,
)
from app.runtime.streaming.event_bus import RuntimeEventBus, set_default_runtime_event_bus
from app.runtime.native.graph_context import RuntimeGraphContextAutoRetriever
from app.runtime.native.state import NativeExecutionState
from app.services.agency_graph_context import AgencyGraphContextRequest, AgencyGraphContextService
from app.services.agent_tools import (
    agent_management_system_tool_definitions,
    graph_system_tool_definitions,
    tool_management_system_tool_definitions,
    workflow_system_tool_definitions,
)
from app.tools.contracts.validator import ToolContractValidationError
from app.tools.runtime.executor import ToolRuntimeExecutor
from app.tools.runtime.store import JsonlToolRunStore


class FakeAgencyGraphReader:
    def __init__(
            self,
            document: GraphReadDocument | None = None,
            *,
            path_document: GraphReadDocument | None = None,
            unavailable: bool = False,
            delay_seconds: float = 0.0,
    ):
        self.document = document or GraphReadDocument(nodes=[], edges=[])
        self.path_document = path_document or GraphReadDocument(nodes=[], edges=[])
        self.unavailable = unavailable
        self.delay_seconds = delay_seconds
        self.calls: list[tuple[str, dict]] = []

    async def get_neighborhood(
        self,
        node_id: str,
        *,
        labels=None,
        relationship_types=None,
        depth=1,
        limit=200,
        include_deleted=False,
    ):
        self.calls.append(
            (
                "get_neighborhood",
                {
                    "node_id": node_id,
                    "labels": labels,
                    "relationship_types": relationship_types,
                    "depth": depth,
                    "limit": limit,
                    "include_deleted": include_deleted,
                },
            )
        )
        if self.unavailable:
            raise GraphReadUnavailableError("graph unavailable")
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.document

    async def search_nodes(
        self,
        query: str | None = None,
        *,
        labels=None,
        node_types=None,
        workflow_id=None,
        agent_id=None,
        tool_id=None,
        document_id=None,
        entity_id=None,
        error_text=None,
        limit=50,
    ):
        self.calls.append(
            (
                "search_nodes",
                {
                    "query": query,
                    "labels": labels,
                    "node_types": node_types,
                    "workflow_id": workflow_id,
                    "agent_id": agent_id,
                    "tool_id": tool_id,
                    "document_id": document_id,
                    "entity_id": entity_id,
                    "error_text": error_text,
                    "limit": limit,
                },
            )
        )
        if self.unavailable:
            raise GraphReadUnavailableError("graph unavailable")
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.document

    async def get_failed_run_root_cause_path(self, run_id: str, *, max_depth=3, limit=25):
        self.calls.append(
            (
                "get_failed_run_root_cause_path",
                {"run_id": run_id, "max_depth": max_depth, "limit": limit},
            )
        )
        return self.path_document

    async def get_memory_source_run_path(self, memory_id: str, *, run_id=None, max_depth=4, limit=25):
        self.calls.append(
            (
                "get_memory_source_run_path",
                {"memory_id": memory_id, "run_id": run_id, "max_depth": max_depth, "limit": limit},
            )
        )
        return self.path_document

    async def get_agent_prior_runs_path(self, agent_id: str, *, run_id=None, max_depth=3, limit=25):
        self.calls.append(
            (
                "get_agent_prior_runs_path",
                {"agent_id": agent_id, "run_id": run_id, "max_depth": max_depth, "limit": limit},
            )
        )
        return self.path_document

    async def get_influence_path(self, anchor_id: str, *, anchor_type: str, workflow_id=None, max_depth=4, limit=25):
        self.calls.append(
            (
                "get_influence_path",
                {
                    "anchor_id": anchor_id,
                    "anchor_type": anchor_type,
                    "workflow_id": workflow_id,
                    "max_depth": max_depth,
                    "limit": limit,
                },
            )
        )
        return self.path_document


def _schema_property_names(schema: dict) -> set[str]:
    names: set[str] = set()
    if not isinstance(schema, dict):
        return names
    properties = schema.get("properties")
    if isinstance(properties, dict):
        names.update(str(name) for name in properties)
        for subschema in properties.values():
            names.update(_schema_property_names(subschema))
    items = schema.get("items")
    if isinstance(items, dict):
        names.update(_schema_property_names(items))
    return names


def _seeded_agency_graph_ui_document() -> GraphReadDocument:
    return GraphReadDocument(
        nodes=[
            GraphReadNode(
                id="workflow-seed",
                type="Workflow",
                labels=["Workflow"],
                properties={
                    "name": "Seeded Agency Graph workflow",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "workflow",
                },
            ),
            GraphReadNode(
                id="run-seed-failed",
                type="WorkflowRun",
                labels=["WorkflowRun"],
                properties={
                    "status": "failed",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "execution",
                },
            ),
            GraphReadNode(
                id="task-seed-stalled",
                type="Task",
                labels=["Task"],
                properties={
                    "name": "Recover stalled sub-agent",
                    "status": "blocked",
                    "task_id": "task-seed-stalled",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "task",
                },
            ),
            GraphReadNode(
                id="agent-seed-coder",
                type="Agent",
                labels=["Agent"],
                properties={
                    "name": "Coder",
                    "agent_id": "agent-seed-coder",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "agent",
                },
            ),
            GraphReadNode(
                id="error-seed-timeout",
                type="Error",
                labels=["Error"],
                properties={
                    "message": "Sub-agent repeated the same failing command.",
                    "status": "failed",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "execution_event",
                },
            ),
            GraphReadNode(
                id="decision-seed",
                type="Decision",
                labels=["Decision"],
                properties={
                    "summary": "Use graph context before retrying the coding task.",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "memory",
                },
            ),
            GraphReadNode(
                id="constraint-seed",
                type="Constraint",
                labels=["Constraint"],
                properties={
                    "summary": "Do not repeat commands that already failed.",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "memory",
                },
            ),
            GraphReadNode(
                id="next-action-seed",
                type="NextAction",
                labels=["NextAction"],
                properties={
                    "summary": "Inspect prior failure and continue from the next unchecked item.",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "memory",
                },
            ),
            GraphReadNode(
                id="memory-seed-decision",
                type="Memory",
                labels=["Memory"],
                properties={
                    "memory_id": "memory-seed-decision",
                    "summary": "Prior decision memory",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "memory",
                },
            ),
            GraphReadNode(
                id="memory-seed-pack",
                type="ContextPack",
                labels=["ContextPack", "Memory"],
                properties={
                    "memory_id": "memory-seed-pack",
                    "summary": "Seeded handoff context pack",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "memory",
                },
            ),
            GraphReadNode(
                id="document-seed",
                type="Document",
                labels=["Document"],
                properties={
                    "document_id": "document-seed",
                    "filename": "graph-rollout.md",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "document",
                },
            ),
            GraphReadNode(
                id="chunk-seed",
                type="DocumentChunk",
                labels=["DocumentChunk"],
                properties={
                    "document_id": "document-seed",
                    "chunk_index": 0,
                    "content": "Raw uploaded chunk text that should not leak in balanced raw graph output.",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "memory",
                },
            ),
            GraphReadNode(
                id="entity-seed",
                type="Entity",
                labels=["Entity"],
                properties={
                    "name": "Neo4j",
                    "entity_id": "entity-seed",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "entity",
                },
            ),
            GraphReadNode(
                id="artifact-seed-change",
                type="Artifact",
                labels=["Artifact"],
                properties={
                    "name": "graph-context.patch",
                    "patch_summary": "Added graph context tool validation.",
                    "workflow_id": "workflow-seed",
                    "source_record_type": "artifact",
                },
            ),
        ],
        edges=[
            GraphReadEdge(id="edge-workflow-run", source="workflow-seed", target="run-seed-failed", type="HAS_RUN"),
            GraphReadEdge(id="edge-run-task", source="run-seed-failed", target="task-seed-stalled", type="HAS_TASK"),
            GraphReadEdge(id="edge-task-agent", source="task-seed-stalled", target="agent-seed-coder", type="ASSIGNED_TO"),
            GraphReadEdge(id="edge-run-error", source="run-seed-failed", target="error-seed-timeout", type="FAILED_WITH"),
            GraphReadEdge(id="edge-run-memory", source="run-seed-failed", target="memory-seed-decision", type="LINKS_MEMORY"),
            GraphReadEdge(id="edge-run-pack", source="run-seed-failed", target="memory-seed-pack", type="LINKS_MEMORY"),
            GraphReadEdge(id="edge-memory-decision", source="memory-seed-decision", target="decision-seed", type="CAPTURES_DECISION"),
            GraphReadEdge(id="edge-pack-next", source="memory-seed-pack", target="next-action-seed", type="SUGGESTS_NEXT_ACTION"),
            GraphReadEdge(id="edge-decision-constraint", source="decision-seed", target="constraint-seed", type="CONSTRAINED_BY"),
            GraphReadEdge(id="edge-document-chunk", source="document-seed", target="chunk-seed", type="HAS_CHUNK"),
            GraphReadEdge(id="edge-chunk-entity", source="chunk-seed", target="entity-seed", type="MENTIONS"),
            GraphReadEdge(id="edge-run-artifact", source="run-seed-failed", target="artifact-seed-change", type="PRODUCED_ARTIFACT"),
        ],
        meta={"query": "seeded_agency_graph_ui_fixture"},
    )


class AgencyGraphContextServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()

    async def test_builds_deterministic_context_from_run_anchor(self) -> None:
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="run-1",
                    type="WorkflowRun",
                    labels=["WorkflowRun"],
                    properties={"status": "failed", "source_record_type": "execution"},
                ),
                GraphReadNode(
                    id="error-1",
                    type="Error",
                    labels=["Error"],
                    properties={"message": "Tool timed out", "source_record_type": "execution_event"},
                ),
                GraphReadNode(
                    id="memory-1",
                    type="Memory",
                    labels=["Memory"],
                    properties={"summary": "Use the smaller batch size", "source_record_type": "memory"},
                ),
                GraphReadNode(
                    id="event-1",
                    type="ExecutionEvent",
                    labels=["ExecutionEvent"],
                    properties={"event_type": "execution.failed", "source_record_type": "execution_event"},
                ),
            ],
            edges=[
                GraphReadEdge(id="edge-1", source="run-1", target="error-1", type="FAILED_WITH"),
                GraphReadEdge(id="edge-2", source="run-1", target="memory-1", type="LINKS_MEMORY"),
            ],
        )
        reader = FakeAgencyGraphReader(document)
        self.context.graph_read_service = reader

        result = await AgencyGraphContextService(self.context).build_context(
            AgencyGraphContextRequest(
                intent="debug",
                anchor_type="run",
                anchor_id="run-1",
                include_events=True,
                budget="balanced",
                limit=25,
            )
        )

        self.assertEqual(result["status"], "ok")
        self.assertIn("run:run-1", result["summary"])
        self.assertEqual(result["failures"][0]["id"], "run-1")
        self.assertEqual(result["related_memories"][0]["id"], "memory-1")
        self.assertEqual(result["recent_events"][0]["id"], "event-1")
        self.assertEqual(result["query_meta"]["intent"], "debug")
        self.assertEqual(result["query_meta"]["depth"], 2)
        self.assertFalse(result["query_meta"]["fallback_used"])
        self.assertEqual(
            reader.calls[0][1]["labels"],
            ["WorkflowRun"],
        )
        self.assertIn("HAS_STEP_RUN", reader.calls[0][1]["relationship_types"])
        self.assertEqual(reader.calls[1][0], "get_failed_run_root_cause_path")

    async def test_query_only_context_uses_graph_search(self) -> None:
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="memory-1",
                    type="Memory",
                    labels=["Memory"],
                    properties={"summary": "Prior integration decision"},
                )
            ],
            edges=[],
        )
        reader = FakeAgencyGraphReader(document)
        self.context.graph_read_service = reader

        result = await AgencyGraphContextService(self.context).build_context(
            {
                "query": "integration decision",
                "intent": "resume",
                "scope": {"node_types": ["Memory"]},
                "budget": "brief",
                "include_raw_graph": True,
            }
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            reader.calls[-1],
            (
                "search_nodes",
                {
                    "query": "integration decision",
                    "labels": ["Memory"],
                    "node_types": None,
                    "workflow_id": None,
                    "agent_id": None,
                    "tool_id": None,
                    "document_id": None,
                    "entity_id": None,
                    "error_text": None,
                    "limit": 50,
                },
            ),
        )
        self.assertEqual(result["query_meta"]["scope"], {"node_types": ["Memory"]})
        self.assertEqual(result["graph"]["nodes"][0]["id"], "memory-1")

    async def test_runtime_scope_context_can_supply_anchor(self) -> None:
        reader = FakeAgencyGraphReader(
            GraphReadDocument(
                nodes=[
                    GraphReadNode(
                        id="execution-1",
                        type="WorkflowRun",
                        labels=["WorkflowRun"],
                        properties={"status": "running"},
                    )
                ],
                edges=[],
            )
        )
        self.context.graph_read_service = reader

        result = await AgencyGraphContextService(self.context).build_context(
            {
                "scope": {"runtime_context": {"execution_id": "execution-1", "workflow_id": "workflow-1"}},
                "intent": "handoff",
                "budget": "brief",
            }
        )

        self.assertEqual(result["status"], "ok")
        self.assertIn("execution:execution-1", result["summary"])
        self.assertEqual(result["query_meta"]["anchor_type"], "execution")
        self.assertEqual(result["query_meta"]["anchor_id"], "execution-1")
        self.assertEqual(reader.calls[-1][0], "get_neighborhood")
        self.assertEqual(reader.calls[-1][1]["node_id"], "execution-1")

    async def test_retrieves_relevant_paths_for_root_cause_context(self) -> None:
        base_document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="run-1",
                    type="WorkflowRun",
                    labels=["WorkflowRun"],
                    properties={"status": "failed"},
                )
            ],
            edges=[],
            meta={"query": "neighborhood"},
        )
        path_document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="run-1",
                    type="WorkflowRun",
                    labels=["WorkflowRun"],
                    properties={"status": "failed"},
                ),
                GraphReadNode(
                    id="error-1",
                    type="Error",
                    labels=["Error"],
                    properties={"message": "Container exited"},
                ),
            ],
            edges=[GraphReadEdge(id="edge-root-cause", source="run-1", target="error-1", type="FAILED_WITH")],
            meta={"query": "failed_run_root_cause_path"},
        )
        reader = FakeAgencyGraphReader(base_document, path_document=path_document)
        self.context.graph_read_service = reader

        result = await AgencyGraphContextService(self.context).build_context(
            {"anchor_type": "run", "anchor_id": "run-1", "intent": "root_cause", "budget": "brief", "limit": 25}
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual([call[0] for call in reader.calls], ["get_neighborhood", "get_failed_run_root_cause_path"])
        self.assertEqual(reader.calls[1][1]["limit"], 8)
        self.assertEqual(result["failures"][0]["id"], "run-1")
        self.assertEqual(result["failures"][1]["id"], "error-1")
        self.assertIn("run-1 FAILED_WITH error-1", result["facts"])
        self.assertEqual(result["query_meta"]["node_count"], 2)
        self.assertEqual(result["query_meta"]["edge_count"], 1)

    async def test_retrieves_execution_events_when_projection_lacks_event_nodes(self) -> None:
        await self.context.execution_store.save_event(
            ExecutionEvent(
                id="event-1",
                execution_id="run-1",
                workflow_id="workflow-1",
                event_type=ExecutionEventType.EXECUTION_STARTED,
                sequence=1,
                status="running",
                payload={"message": "Execution started"},
            )
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                id="event-2",
                execution_id="run-1",
                workflow_id="workflow-1",
                event_type=ExecutionEventType.TOOL_CALL_FAILED,
                sequence=2,
                status="failed",
                payload={"error": "Tool timed out", "token": "secret-token"},
            )
        )
        self.context.graph_read_service = FakeAgencyGraphReader(
            GraphReadDocument(
                nodes=[
                    GraphReadNode(
                        id="run-1",
                        type="WorkflowRun",
                        labels=["WorkflowRun"],
                        properties={"status": "failed"},
                    )
                ],
                edges=[],
            )
        )

        result = await AgencyGraphContextService(self.context).build_context(
            {
                "anchor_type": "run",
                "anchor_id": "run-1",
                "intent": "debug",
                "include_events": True,
                "budget": "balanced",
            }
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual([event["id"] for event in result["recent_events"]], ["event-2", "event-1"])
        self.assertEqual(result["recent_events"][0]["summary"], "Tool timed out")
        self.assertNotIn("token", result["recent_events"][0]["payload"])
        self.assertTrue(result["query_meta"]["runtime_events_fallback_used"])
        self.assertEqual(result["query_meta"]["runtime_events_fallback_count"], 2)
        self.assertTrue(result["query_meta"]["fallback_used"])

    async def test_hydrates_linked_memory_nodes_through_memory_service(self) -> None:
        await self.context.user_repo.create(UserDefinition(id="owner-user", email="owner@example.com"))
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-1",
                scope=MemoryScope.USER,
                content="Use the smaller batch size for imports because the API times out above 100 records.",
                summary="Use smaller import batches",
                tags=["imports", "timeouts"],
                importance=80,
                created_by_user_id="owner-user",
            )
        )
        self.context.graph_read_service = FakeAgencyGraphReader(
            GraphReadDocument(
                nodes=[
                    GraphReadNode(
                        id="memory-1",
                        type="Memory",
                        labels=["Memory"],
                        properties={"memory_id": "memory-1", "summary": "stale graph summary"},
                    )
                ],
                edges=[],
            )
        )

        result = await AgencyGraphContextService(self.context).build_context(
            {"query": "imports", "intent": "learn", "scope": {"current_user_id": "owner-user"}}
        )

        self.assertEqual(result["status"], "ok")
        memory = result["related_memories"][0]
        self.assertEqual(memory["id"], "memory-1")
        self.assertEqual(memory["summary"], "Use smaller import batches")
        self.assertIn("above 100 records", memory["content_preview"])
        self.assertEqual(memory["scope"], "user")
        self.assertEqual(memory["importance"], 80)
        self.assertEqual(memory["tags"], ["imports", "timeouts"])

    async def test_rejects_invalid_intent_before_graph_read(self) -> None:
        reader = FakeAgencyGraphReader()
        self.context.graph_read_service = reader

        result = await AgencyGraphContextService(self.context).build_context(
            {"query": "anything", "intent": "unknown"}
        )

        self.assertEqual(result["status"], "invalid_intent")
        self.assertEqual(reader.calls, [])

    async def test_rejects_invalid_preset_before_graph_read(self) -> None:
        reader = FakeAgencyGraphReader()
        self.context.graph_read_service = reader

        result = await AgencyGraphContextService(self.context).build_context(
            {"anchor_type": "run", "anchor_id": "run-1", "preset": "unknown"}
        )

        self.assertEqual(result["status"], "invalid_preset")
        self.assertIn("Unsupported Agency Graph preset", result["summary"])
        self.assertEqual(result["query_meta"]["preset"], "unknown")
        self.assertEqual(reader.calls, [])

    async def test_explicit_valid_preset_overrides_anchor_default(self) -> None:
        reader = FakeAgencyGraphReader(
            GraphReadDocument(
                nodes=[GraphReadNode(id="memory-1", type="Memory", labels=["Memory"], properties={"summary": "Memory"})],
                edges=[],
            )
        )
        self.context.graph_read_service = reader

        result = await AgencyGraphContextService(self.context).build_context(
            {"anchor_type": "run", "anchor_id": "memory-1", "preset": "memory", "intent": "learn"}
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["query_meta"]["preset"], "memory")
        self.assertEqual(reader.calls[0][1]["labels"], ["Memory"])
        self.assertIn("LINKS_MEMORY", reader.calls[0][1]["relationship_types"])

    async def test_rejects_partial_anchor_before_graph_read(self) -> None:
        reader = FakeAgencyGraphReader()
        self.context.graph_read_service = reader

        result = await AgencyGraphContextService(self.context).build_context(
            {"anchor_type": "run", "intent": "debug"}
        )

        self.assertEqual(result["status"], "invalid_anchor")
        self.assertEqual(reader.calls, [])

    async def test_reports_graph_unavailable_with_next_step_guidance(self) -> None:
        self.context.graph_read_service = FakeAgencyGraphReader(unavailable=True)

        result = await AgencyGraphContextService(self.context).build_context(
            {"anchor_type": "run", "anchor_id": "run-1", "intent": "root_cause"}
        )

        self.assertEqual(result["status"], "graph_unavailable")
        self.assertFalse(result["query_meta"]["projection_available"])
        self.assertIn("durable memory search", result["query_meta"]["guidance"])

    async def test_reports_timeout_when_graph_read_exceeds_query_timeout(self) -> None:
        previous = os.environ.get("AGENCY_GRAPH_CONTEXT_QUERY_TIMEOUT_SECONDS")
        try:
            os.environ["AGENCY_GRAPH_CONTEXT_QUERY_TIMEOUT_SECONDS"] = "0.05"
            reset_settings_cache()
            self.context.graph_read_service = FakeAgencyGraphReader(delay_seconds=0.2)

            result = await AgencyGraphContextService(self.context).build_context(
                {"anchor_type": "run", "anchor_id": "run-1", "intent": "root_cause"}
            )
        finally:
            if previous is None:
                os.environ.pop("AGENCY_GRAPH_CONTEXT_QUERY_TIMEOUT_SECONDS", None)
            else:
                os.environ["AGENCY_GRAPH_CONTEXT_QUERY_TIMEOUT_SECONDS"] = previous
            reset_settings_cache()

        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["query_meta"]["projection_available"])
        self.assertEqual(result["query_meta"]["depth"], 2)
        self.assertIn("timed out", result["query_meta"]["guidance"])

    async def test_rejects_repeated_graph_traversal_when_budget_is_exceeded(self) -> None:
        previous_max = os.environ.get("AGENCY_GRAPH_CONTEXT_RATE_LIMIT_MAX_UNITS")
        previous_window = os.environ.get("AGENCY_GRAPH_CONTEXT_RATE_LIMIT_WINDOW_SECONDS")
        reader = FakeAgencyGraphReader(GraphReadDocument(nodes=[], edges=[]))
        try:
            os.environ["AGENCY_GRAPH_CONTEXT_RATE_LIMIT_MAX_UNITS"] = "15"
            os.environ["AGENCY_GRAPH_CONTEXT_RATE_LIMIT_WINDOW_SECONDS"] = "60"
            reset_settings_cache()
            self.context.graph_read_service = reader

            first = await AgencyGraphContextService(self.context).build_context(
                {
                    "query": "limited traversal",
                    "intent": "audit",
                    "budget": "brief",
                    "limit": 10,
                    "scope": {"current_user_id": "limited-graph-user"},
                }
            )
            second = await AgencyGraphContextService(self.context).build_context(
                {
                    "query": "limited traversal",
                    "intent": "audit",
                    "budget": "brief",
                    "limit": 10,
                    "scope": {"current_user_id": "limited-graph-user"},
                }
            )
        finally:
            if previous_max is None:
                os.environ.pop("AGENCY_GRAPH_CONTEXT_RATE_LIMIT_MAX_UNITS", None)
            else:
                os.environ["AGENCY_GRAPH_CONTEXT_RATE_LIMIT_MAX_UNITS"] = previous_max
            if previous_window is None:
                os.environ.pop("AGENCY_GRAPH_CONTEXT_RATE_LIMIT_WINDOW_SECONDS", None)
            else:
                os.environ["AGENCY_GRAPH_CONTEXT_RATE_LIMIT_WINDOW_SECONDS"] = previous_window
            reset_settings_cache()

        self.assertEqual(first["status"], "no_data")
        self.assertEqual(first["query_meta"]["traversal_units"], 10)
        self.assertEqual(second["status"], "budget_exceeded")
        self.assertEqual(second["query_meta"]["traversal_units"], 10)
        self.assertEqual(second["query_meta"]["traversal_units_used"], 10)
        self.assertEqual(second["query_meta"]["traversal_units_remaining"], 5)
        self.assertEqual(second["query_meta"]["traversal_budget_max_units"], 15)
        self.assertEqual(len(reader.calls), 1)

    async def test_budget_synthesis_prioritizes_high_signal_nodes(self) -> None:
        filler_nodes = [
            GraphReadNode(
                id=f"tool-filler-{index}",
                type="Tool",
                labels=["Tool"],
                properties={"name": f"Filler tool {index}"},
            )
            for index in range(25)
        ]
        document = GraphReadDocument(
            nodes=[
                *filler_nodes,
                GraphReadNode(
                    id="decision-late",
                    type="Decision",
                    labels=["Decision"],
                    properties={"summary": "Keep the graph context budget focused."},
                ),
                GraphReadNode(
                    id="error-late",
                    type="Error",
                    labels=["Error"],
                    properties={"message": "Late failure should survive budget slicing."},
                ),
            ],
            edges=[],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)

        result = await AgencyGraphContextService(self.context).build_context(
            {
                "anchor_type": "workflow",
                "anchor_id": "workflow-budget-ranking",
                "intent": "steer",
                "budget": "balanced",
                "limit": 50,
            }
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["omitted"]["nodes"], 7)
        self.assertEqual(result["failures"][0]["id"], "error-late")
        self.assertEqual(result["decisions"][0]["id"], "decision-late")
        self.assertNotIn("tool-filler-24", {item["id"] for item in result["provenance"]["nodes"]})

    def test_rollout_presets_include_agent_context_signal_relationships(self) -> None:
        self.assertTrue(
            {
                "FAILED_WITH",
                "CAPTURES_DECISION",
                "CONSTRAINED_BY",
                "SUGGESTS_NEXT_ACTION",
                "PRODUCED_ARTIFACT",
            }.issubset(set(GRAPH_NEIGHBORHOOD_PRESETS["workflow_run"]["relationship_types"]))
        )
        self.assertTrue(
            {
                "LINKS_MEMORY",
                "FAILED_WITH",
                "CAPTURES_DECISION",
                "SUGGESTS_NEXT_ACTION",
                "PRODUCED_ARTIFACT",
            }.issubset(set(GRAPH_NEIGHBORHOOD_PRESETS["task"]["relationship_types"]))
        )
        self.assertIn("FAILED_WITH", GRAPH_NEIGHBORHOOD_PRESETS["workflow"]["relationship_types"])

    async def test_reports_graph_disabled_when_neo4j_reads_are_off(self) -> None:
        previous = os.environ.get("NEO4J_ENABLED")
        try:
            os.environ["NEO4J_ENABLED"] = "false"
            reset_settings_cache()
            result = await AgencyGraphContextService(self.context).build_context(
                {"anchor_type": "run", "anchor_id": "run-1", "intent": "root_cause"}
            )
        finally:
            if previous is None:
                os.environ.pop("NEO4J_ENABLED", None)
            else:
                os.environ["NEO4J_ENABLED"] = previous
            reset_settings_cache()

        self.assertEqual(result["status"], "graph_disabled")
        self.assertFalse(result["query_meta"]["projection_available"])
        self.assertIn("NEO4J_ENABLED=true", result["query_meta"]["guidance"])

    async def test_no_data_response_includes_fallback_guidance(self) -> None:
        self.context.graph_read_service = FakeAgencyGraphReader(GraphReadDocument(nodes=[], edges=[]))

        result = await AgencyGraphContextService(self.context).build_context(
            {"query": "missing run", "intent": "debug"}
        )

        self.assertEqual(result["status"], "no_data")
        self.assertIn("No Agency Graph records", result["summary"])
        self.assertIn("durable memory search", result["query_meta"]["guidance"])

    async def test_rollout_validates_seeded_agency_graph_ui_fixture(self) -> None:
        await self.context.user_repo.create(UserDefinition(id="owner-user", email="owner@example.com", roles=["admin"]))
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-seed-decision",
                scope=MemoryScope.WORKFLOW,
                workflow_id="workflow-seed",
                content="Prior decision: use graph context before retrying the coding task.",
                summary="Use graph context before retrying.",
                tags=["rollout", "decision"],
                memory_type=MemoryType.DECISION,
                created_by_user_id="owner-user",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-seed-pack",
                scope=MemoryScope.WORKFLOW,
                workflow_id="workflow-seed",
                content="Context pack: prior command failed; continue from the next unchecked item.",
                summary="Seeded graph rollout context pack",
                tags=["context_pack", "rollout"],
                memory_type=MemoryType.CONTEXT_PACK,
                created_by_user_id="owner-user",
            )
        )
        self.context.graph_read_service = FakeAgencyGraphReader(_seeded_agency_graph_ui_document())

        result = await AgencyGraphContextService(self.context).build_context(
            {
                "anchor_type": "run",
                "anchor_id": "run-seed-failed",
                "intent": "root_cause",
                "scope": {"current_user_id": "owner-user", "workflow_id": "workflow-seed"},
                "include_events": True,
                "include_raw_graph": True,
                "budget": "balanced",
                "limit": 50,
            }
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["query_meta"]["intent"], "root_cause")
        self.assertEqual(result["query_meta"]["projection_available"], True)
        self.assertEqual(result["query_meta"]["node_count"], 14)
        self.assertEqual(result["query_meta"]["edge_count"], 12)
        self.assertIn("run-seed-failed FAILED_WITH error-seed-timeout", result["facts"])
        self.assertEqual({item["id"] for item in result["failures"]}, {"run-seed-failed", "error-seed-timeout"})
        self.assertEqual(result["decisions"][0]["id"], "decision-seed")
        self.assertEqual(result["constraints"][0]["id"], "constraint-seed")
        self.assertEqual(result["next_actions"][0]["id"], "next-action-seed")
        self.assertEqual(result["prior_changes"][0]["id"], "artifact-seed-change")
        self.assertEqual({item["id"] for item in result["related_memories"]}, {"memory-seed-decision", "memory-seed-pack"})
        self.assertTrue(
            any(item["id"] == "document-seed" for item in result["related_documents"])
        )
        chunk = next(node for node in result["graph"]["nodes"] if node["id"] == "chunk-seed")
        self.assertNotIn("content", chunk["properties"])
        self.assertTrue(chunk["properties"]["document_chunk_text_omitted_by_policy"])
        self.assertEqual(result["graph"]["meta"]["document_chunks_sanitized_by_policy"], 1)
        output_bytes = len(json.dumps(result, separators=(",", ":"), default=str).encode("utf-8"))
        self.assertLess(output_bytes, 50000)
        self.assertGreaterEqual(len(result["facts"]), 10)

    async def test_main_agent_dogfoods_workflow_context_and_prior_coding_summary(self) -> None:
        await self.context.user_repo.create(UserDefinition(id="owner-user", email="owner@example.com", roles=["admin"]))
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-run-summary",
                scope=MemoryScope.WORKFLOW,
                workflow_id="workflow-main-dogfood",
                content="Run summary: graph context validation was added and the next step is preset tuning.",
                summary="Graph context validation added",
                tags=["run_summary", "coding"],
                memory_type=MemoryType.RUN_SUMMARY,
                created_by_user_id="owner-user",
            )
        )
        self.context.graph_read_service = FakeAgencyGraphReader(
            GraphReadDocument(
                nodes=[
                    GraphReadNode(
                        id="workflow-main-dogfood",
                        type="Workflow",
                        labels=["Workflow"],
                        properties={"name": "Main-agent dogfood workflow", "workflow_id": "workflow-main-dogfood"},
                    ),
                    GraphReadNode(
                        id="run-main-dogfood",
                        type="WorkflowRun",
                        labels=["WorkflowRun"],
                        properties={"status": "completed", "workflow_id": "workflow-main-dogfood"},
                    ),
                    GraphReadNode(
                        id="memory-run-summary",
                        type="Memory",
                        labels=["Memory"],
                        properties={
                            "memory_id": "memory-run-summary",
                            "summary": "stale projected run summary",
                            "workflow_id": "workflow-main-dogfood",
                        },
                    ),
                    GraphReadNode(
                        id="decision-main-dogfood",
                        type="Decision",
                        labels=["Decision"],
                        properties={
                            "summary": "Tune graph context presets only after dogfood tests pass.",
                            "workflow_id": "workflow-main-dogfood",
                        },
                    ),
                    GraphReadNode(
                        id="artifact-main-dogfood",
                        type="Artifact",
                        labels=["Artifact"],
                        properties={
                            "patch_summary": "Added graph context validation tests.",
                            "workflow_id": "workflow-main-dogfood",
                        },
                    ),
                ],
                edges=[
                    GraphReadEdge(
                        id="edge-workflow-run",
                        source="workflow-main-dogfood",
                        target="run-main-dogfood",
                        type="HAS_RUN",
                    ),
                    GraphReadEdge(
                        id="edge-run-memory",
                        source="run-main-dogfood",
                        target="memory-run-summary",
                        type="LINKS_MEMORY",
                    ),
                    GraphReadEdge(
                        id="edge-run-decision",
                        source="run-main-dogfood",
                        target="decision-main-dogfood",
                        type="CAPTURES_DECISION",
                    ),
                    GraphReadEdge(
                        id="edge-run-artifact",
                        source="run-main-dogfood",
                        target="artifact-main-dogfood",
                        type="PRODUCED_ARTIFACT",
                    ),
                ],
            )
        )

        result = await AgencyGraphContextService(self.context).build_context(
            {
                "anchor_type": "workflow",
                "anchor_id": "workflow-main-dogfood",
                "intent": "handoff",
                "scope": {"current_user_id": "owner-user", "workflow_id": "workflow-main-dogfood"},
                "budget": "balanced",
                "limit": 25,
            }
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["query_meta"]["anchor_type"], "workflow")
        self.assertEqual(result["query_meta"]["anchor_id"], "workflow-main-dogfood")
        self.assertEqual(result["query_meta"]["scope"]["workflow_id"], "workflow-main-dogfood")
        self.assertEqual(result["run_summaries"][0]["id"], "memory-run-summary")
        self.assertIn("preset tuning", result["run_summaries"][0]["content_preview"])
        self.assertEqual(result["decisions"][0]["id"], "decision-main-dogfood")
        self.assertEqual(result["prior_changes"][0]["id"], "artifact-main-dogfood")
        self.assertIn("workflow-main-dogfood HAS_RUN run-main-dogfood", result["facts"])

    async def test_main_agent_dogfoods_failed_run_debug_context(self) -> None:
        self.context.graph_read_service = FakeAgencyGraphReader(
            GraphReadDocument(
                nodes=[
                    GraphReadNode(
                        id="run-main-failed",
                        type="WorkflowRun",
                        labels=["WorkflowRun"],
                        properties={"status": "failed", "workflow_id": "workflow-main-debug"},
                    ),
                    GraphReadNode(
                        id="error-main-failed",
                        type="Error",
                        labels=["Error"],
                        properties={"message": "Container exited before producing an artifact."},
                    ),
                    GraphReadNode(
                        id="next-action-main-failed",
                        type="NextAction",
                        labels=["NextAction"],
                        properties={"summary": "Inspect container logs before retrying the task."},
                    ),
                ],
                edges=[
                    GraphReadEdge(
                        id="edge-run-error",
                        source="run-main-failed",
                        target="error-main-failed",
                        type="FAILED_WITH",
                    ),
                    GraphReadEdge(
                        id="edge-error-next",
                        source="error-main-failed",
                        target="next-action-main-failed",
                        type="SUGGESTS_NEXT_ACTION",
                    ),
                ],
            )
        )

        result = await AgencyGraphContextService(self.context).build_context(
            {
                "anchor_type": "run",
                "anchor_id": "run-main-failed",
                "intent": "root_cause",
                "scope": {"workflow_id": "workflow-main-debug"},
                "include_events": True,
                "budget": "balanced",
                "limit": 25,
            }
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["query_meta"]["intent"], "root_cause")
        self.assertEqual(result["query_meta"]["anchor_id"], "run-main-failed")
        self.assertEqual({item["id"] for item in result["failures"]}, {"run-main-failed", "error-main-failed"})
        self.assertEqual(result["next_actions"][0]["id"], "next-action-main-failed")
        self.assertIn("run-main-failed FAILED_WITH error-main-failed", result["facts"])

    async def test_filters_memory_nodes_the_actor_cannot_read(self) -> None:
        await self.context.user_repo.create(UserDefinition(id="owner-user", email="owner@example.com"))
        await self.context.user_repo.create(UserDefinition(id="other-user", email="other@example.com"))
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-private",
                scope=MemoryScope.USER,
                content="Private owner memory.",
                summary="Private owner memory",
                created_by_user_id="owner-user",
            )
        )
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="memory-private",
                    type="Memory",
                    labels=["Memory"],
                    properties={"memory_id": "memory-private", "summary": "Private owner memory"},
                ),
                GraphReadNode(id="tool-1", type="Tool", labels=["Tool"], properties={"name": "Tool"}),
            ],
            edges=[GraphReadEdge(id="edge-1", source="memory-private", target="tool-1", type="MENTIONS")],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)

        result = await AgencyGraphContextService(self.context).build_context(
            {
                "query": "private",
                "intent": "audit",
                "scope": {"current_user_id": "other-user"},
                "include_raw_graph": True,
            }
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["related_memories"], [])
        self.assertEqual(result["graph"]["nodes"][0]["id"], "tool-1")
        self.assertEqual(result["graph"]["edges"], [])
        self.assertEqual(result["graph"]["meta"]["memory_nodes_omitted_by_policy"], 1)

    async def test_filters_memory_nodes_excluded_for_runtime_target(self) -> None:
        await self.context.user_repo.create(UserDefinition(id="owner-user", email="owner@example.com"))
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-excluded",
                scope=MemoryScope.USER,
                content="Do not use for this task.",
                summary="Excluded memory",
                created_by_user_id="owner-user",
                metadata={
                    "exclusions": [
                        {
                            "id": "exclusion-1",
                            "target_type": "task",
                            "target_id": "task-1",
                            "reason": "Outdated for this task.",
                        }
                    ]
                },
            )
        )
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="memory-excluded",
                    type="Memory",
                    labels=["Memory"],
                    properties={"memory_id": "memory-excluded", "summary": "Excluded memory"},
                ),
                GraphReadNode(id="task-1", type="Task", labels=["Task"], properties={"name": "Task"}),
            ],
            edges=[GraphReadEdge(id="edge-1", source="task-1", target="memory-excluded", type="LINKS_MEMORY")],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)

        result = await AgencyGraphContextService(self.context).build_context(
            {
                "query": "excluded",
                "intent": "debug",
                "scope": {"current_user_id": "owner-user", "runtime_context": {"task_id": "task-1"}},
                "include_raw_graph": True,
            }
        )

        self.assertEqual(result["related_memories"], [])
        self.assertEqual([node["id"] for node in result["graph"]["nodes"]], ["task-1"])
        self.assertEqual(result["graph"]["edges"], [])

    async def test_hides_sensitive_memory_nodes_unless_scope_allows_them(self) -> None:
        await self.context.user_repo.create(UserDefinition(id="owner-user", email="owner@example.com"))
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-sensitive",
                scope=MemoryScope.USER,
                content="Sensitive token value.",
                summary="Sensitive memory summary",
                sensitive=True,
                created_by_user_id="owner-user",
            )
        )
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="memory-sensitive",
                    type="Memory",
                    labels=["Memory"],
                    properties={"memory_id": "memory-sensitive", "summary": "Sensitive memory summary", "sensitive": True},
                )
            ],
            edges=[],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)

        result = await AgencyGraphContextService(self.context).build_context(
            {"query": "sensitive", "scope": {"current_user_id": "owner-user"}, "include_raw_graph": True}
        )
        allowed = await AgencyGraphContextService(self.context).build_context(
            {
                "query": "sensitive",
                "scope": {"current_user_id": "owner-user", "include_sensitive_memories": True},
                "include_raw_graph": True,
            }
        )

        self.assertEqual(result["related_memories"], [])
        self.assertEqual(result["graph"]["nodes"], [])
        self.assertEqual(allowed["related_memories"][0]["id"], "memory-sensitive")
        self.assertEqual(allowed["related_memories"][0]["summary"], "Sensitive Memory redacted")

    async def test_filters_graph_nodes_outside_runtime_scope(self) -> None:
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="task-allowed",
                    type="Task",
                    labels=["Task"],
                    properties={"name": "Allowed", "workflow_id": "workflow-allowed", "workspace_id": "workspace-1"},
                ),
                GraphReadNode(
                    id="task-other-workflow",
                    type="Task",
                    labels=["Task"],
                    properties={"name": "Other", "workflow_id": "workflow-other", "workspace_id": "workspace-1"},
                ),
                GraphReadNode(
                    id="task-other-workspace",
                    type="Task",
                    labels=["Task"],
                    properties={"name": "Other workspace", "workflow_id": "workflow-allowed", "workspace_id": "workspace-2"},
                ),
            ],
            edges=[
                GraphReadEdge(id="edge-1", source="task-allowed", target="task-other-workflow", type="DEPENDS_ON"),
                GraphReadEdge(id="edge-2", source="task-allowed", target="task-other-workspace", type="DEPENDS_ON"),
            ],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)

        result = await AgencyGraphContextService(self.context).build_context(
            {
                "query": "task",
                "scope": {"workflow_id": "workflow-allowed", "workspace_id": "workspace-1"},
                "include_raw_graph": True,
            }
        )

        self.assertEqual([node["id"] for node in result["graph"]["nodes"]], ["task-allowed"])
        self.assertEqual(result["graph"]["edges"], [])
        self.assertEqual(result["query_meta"]["scope_nodes_omitted_by_policy"], 2)

    async def test_redacts_raw_document_chunks_unless_policy_and_budget_allow(self) -> None:
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="chunk-1",
                    type="DocumentChunk",
                    labels=["DocumentChunk"],
                    properties={"text": "raw document chunk", "summary": "Chunk summary"},
                )
            ],
            edges=[],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)

        redacted = await AgencyGraphContextService(self.context).build_context(
            {"query": "chunk", "include_raw_graph": True}
        )
        allowed = await AgencyGraphContextService(self.context).build_context(
            {
                "query": "chunk",
                "include_raw_graph": True,
                "budget": "raw_graph",
                "scope": {"allow_raw_document_chunks": True},
            }
        )

        self.assertNotIn("text", redacted["graph"]["nodes"][0]["properties"])
        self.assertTrue(redacted["graph"]["nodes"][0]["properties"]["document_chunk_text_omitted_by_policy"])
        self.assertEqual(redacted["query_meta"]["document_chunks_sanitized_by_policy"], 1)
        self.assertEqual(allowed["graph"]["nodes"][0]["properties"]["text"], "raw document chunk")

    async def test_redacts_sensitive_graph_context_and_raw_graph_properties(self) -> None:
        await self.context.user_repo.create(UserDefinition(id="owner-user", email="owner@example.com"))
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-sensitive",
                scope=MemoryScope.USER,
                content="Sensitive memory content.",
                summary="Sensitive memory summary",
                sensitive=True,
                created_by_user_id="owner-user",
            )
        )
        secret = "sk-test-secret-value"
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="memory-sensitive",
                    type="Memory",
                    labels=["Memory"],
                    properties={
                        "summary": f"Customer token is {secret}",
                        "content": f"Raw secret {secret}",
                        "api_key": secret,
                        "embedding": [0.1, 0.2],
                        "sensitive": True,
                    },
                ),
                GraphReadNode(
                    id="tool-1",
                    type="Tool",
                    labels=["Tool"],
                    properties={"name": "Connector", "credential_ref": "secret://agency/token"},
                ),
            ],
            edges=[
                GraphReadEdge(
                    id="edge-sensitive",
                    source="memory-sensitive",
                    target="tool-1",
                    type="MENTIONS",
                    properties={"token": secret, "reason": "allowed"},
                )
            ],
            meta={"query": "search", "authorization": f"Bearer {secret}"},
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)

        result = await AgencyGraphContextService(self.context).build_context(
            {
                "query": "secret",
                "intent": "audit",
                "scope": {"current_user_id": "owner-user", "include_sensitive_memories": True},
                "include_raw_graph": True,
            }
        )
        serialized = str(result)

        self.assertEqual(result["status"], "ok")
        self.assertNotIn(secret, serialized)
        self.assertEqual(result["related_memories"][0]["label"], "Sensitive Memory redacted")
        self.assertEqual(result["related_memories"][0]["summary"], "Sensitive Memory redacted")
        memory_properties = result["graph"]["nodes"][0]["properties"]
        self.assertNotIn("summary", memory_properties)
        self.assertNotIn("content", memory_properties)
        self.assertNotIn("api_key", memory_properties)
        self.assertNotIn("embedding", memory_properties)
        self.assertTrue(memory_properties["sensitive"])
        self.assertNotIn("credential_ref", result["graph"]["nodes"][1]["properties"])
        self.assertEqual(result["graph"]["edges"][0]["properties"], {"reason": "allowed"})
        self.assertNotIn("authorization", result["graph"]["meta"])

    async def test_omits_credential_nodes_and_connected_edges_even_in_raw_graph(self) -> None:
        secret = "secret://agency/slack-token"
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="tool-1",
                    type="Tool",
                    labels=["Tool"],
                    properties={"name": "Slack tool"},
                ),
                GraphReadNode(
                    id=secret,
                    type="Credential",
                    labels=["Credential"],
                    properties={
                        "name": "Slack production credential",
                        "secret_ref": secret,
                        "external_account_id": "workspace-acme-secret",
                        "source_record_id": "credential-secret",
                    },
                ),
            ],
            edges=[
                GraphReadEdge(
                    id="edge-credential",
                    source="tool-1",
                    target=secret,
                    type="USES_CREDENTIAL",
                    properties={"reason": "production workspace"},
                )
            ],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)

        result = await AgencyGraphContextService(self.context).build_context(
            {"query": "slack", "intent": "audit", "include_raw_graph": True}
        )
        serialized = str(result)

        self.assertEqual(result["status"], "ok")
        self.assertEqual([node["id"] for node in result["graph"]["nodes"]], ["tool-1"])
        self.assertEqual(result["graph"]["edges"], [])
        self.assertEqual(result["graph"]["meta"]["protected_nodes_omitted_by_policy"], 1)
        self.assertEqual(result["query_meta"]["protected_nodes_omitted_by_policy"], 1)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("workspace-acme-secret", serialized)
        self.assertNotIn("USES_CREDENTIAL", serialized)

    async def test_redacts_integration_nodes_aggressively_in_context_and_raw_graph(self) -> None:
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="integration-slack",
                    type="Integration",
                    labels=["Integration"],
                    properties={
                        "name": "ACME Slack workspace",
                        "status": "healthy",
                        "health": "ok",
                        "credential_ref": "secret://agency/slack-token",
                        "external_account_id": "T-acme-private",
                        "config": {"workspace": "acme-private"},
                        "source_record_type": "integration",
                        "source_record_id": "integration-slack",
                    },
                ),
                GraphReadNode(
                    id="tool-1",
                    type="Tool",
                    labels=["Tool"],
                    properties={"name": "Slack sender"},
                ),
            ],
            edges=[GraphReadEdge(id="edge-1", source="tool-1", target="integration-slack", type="USES_INTEGRATION")],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)

        result = await AgencyGraphContextService(self.context).build_context(
            {"query": "slack", "intent": "audit", "include_raw_graph": True}
        )
        serialized = str(result)
        integration = next(node for node in result["graph"]["nodes"] if node["id"] == "integration-slack")

        self.assertEqual(result["status"], "ok")
        self.assertIn("Protected Integration redacted", result["facts"][0])
        self.assertEqual(result["graph"]["meta"].get("protected_nodes_omitted_by_policy"), None)
        self.assertEqual(
            integration["properties"],
            {
                "status": "healthy",
                "health": "ok",
                "source_record_type": "integration",
                "source_record_id": "integration-slack",
                "protected": True,
                "redacted": True,
            },
        )
        self.assertNotIn("ACME Slack workspace", serialized)
        self.assertNotIn("secret://agency/slack-token", serialized)
        self.assertNotIn("T-acme-private", serialized)
        self.assertNotIn("acme-private", serialized)

    async def test_budget_reports_omitted_graph_content(self) -> None:
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(id=f"node-{index}", type="Memory", labels=["Memory"], properties={"summary": f"Memory {index}"})
                for index in range(12)
            ],
            edges=[
                GraphReadEdge(id=f"edge-{index}", source="node-0", target=f"node-{index}", type="MENTIONS")
                for index in range(1, 12)
            ],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)

        result = await AgencyGraphContextService(self.context).build_context(
            {"query": "memory", "intent": "learn", "budget": "brief"}
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["omitted"]["nodes"], 4)
        self.assertEqual(result["omitted"]["edges"], 3)
        self.assertEqual(result["omitted"]["reason"], "budget")
        self.assertEqual(result["query_meta"]["memory_nodes_omitted_by_policy"], 0)

    def test_graph_context_auto_retrieval_settings_default_off(self) -> None:
        previous = {
            key: os.environ.get(key)
            for key in (
                "GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED",
                "GRAPH_CONTEXT_SUBAGENT_STEERING_ENABLED",
                "GRAPH_CONTEXT_CODING_AGENT_RESUME_ENABLED",
            )
        }
        try:
            for key in previous:
                os.environ.pop(key, None)
            reset_settings_cache()
            settings = get_settings()

            self.assertFalse(settings.graph_context_auto_retrieval_enabled)
            self.assertFalse(settings.graph_context_subagent_steering_enabled)
            self.assertFalse(settings.graph_context_coding_agent_resume_enabled)

            os.environ["GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED"] = "true"
            os.environ["GRAPH_CONTEXT_SUBAGENT_STEERING_ENABLED"] = "true"
            os.environ["GRAPH_CONTEXT_CODING_AGENT_RESUME_ENABLED"] = "true"
            reset_settings_cache()
            settings = get_settings()

            self.assertTrue(settings.graph_context_auto_retrieval_enabled)
            self.assertTrue(settings.graph_context_subagent_steering_enabled)
            self.assertTrue(settings.graph_context_coding_agent_resume_enabled)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reset_settings_cache()

    async def test_runtime_auto_retriever_uses_brief_handoff_scope_before_subagent_start(self) -> None:
        previous = {
            key: os.environ.get(key)
            for key in (
                "GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED",
                "GRAPH_CONTEXT_SUBAGENT_STEERING_ENABLED",
            )
        }
        try:
            os.environ["GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED"] = "true"
            os.environ["GRAPH_CONTEXT_SUBAGENT_STEERING_ENABLED"] = "true"
            reset_settings_cache()

            reader = FakeAgencyGraphReader(
                GraphReadDocument(
                    nodes=[
                        GraphReadNode(
                            id="task-runtime-graph",
                            type="Task",
                            labels=["Task"],
                            properties={"name": "Runtime Graph Task"},
                        )
                    ],
                    edges=[],
                )
            )
            self.context.graph_read_service = reader
            agent = AgentDefinition(
                id="agent-runtime-graph",
                name="Runtime Graph Agent",
                model_profile_id="profile-runtime-graph",
                graph_context=GraphContextSettings(
                    enabled=True,
                    auto_retrieval_enabled=True,
                    subagent_steering_enabled=True,
                    default_intent="plan",
                    include_events=True,
                    max_records=7,
                ),
            )
            task = TaskDefinition(
                id="task-runtime-graph",
                name="Runtime Graph Task",
                description="Use graph context before starting.",
                agent_id=agent.id,
            )
            workflow = WorkflowDefinition(
                id="workflow-runtime-graph",
                name="Runtime Graph Workflow",
                nodes=[
                    WorkflowNodeDefinition(
                        id="node-runtime-graph",
                        name="Runtime Graph Node",
                        node_type="task",
                        task_id=task.id,
                        agent_id=agent.id,
                    )
                ],
                entrypoint="node-runtime-graph",
                task_definitions=[task],
                agent_definitions=[agent],
            )
            execution = Execution(
                id="run-runtime-graph",
                workflow_id=workflow.id,
                runtime_adapter="native",
                input_json={"prompt": "continue"},
            )
            state = NativeExecutionState(execution_id=execution.id, workflow_id=workflow.id)
            state.current_node_id = "node-runtime-graph"

            entry = await RuntimeGraphContextAutoRetriever(self.context).retrieve_before_subagent_start(
                workflow,
                task,
                agent,
                execution,
                execution.input_payload,
                state,
            )

            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry["trigger"], "subagent_start")
            self.assertEqual(entry["intent"], "plan")
            self.assertEqual(entry["budget"], "brief")
            self.assertEqual(entry["anchor_type"], "task")
            self.assertEqual(entry["anchor_id"], task.id)
            self.assertEqual(reader.calls[0][0], "get_neighborhood")
            self.assertEqual(reader.calls[0][1]["node_id"], task.id)
            self.assertEqual(reader.calls[0][1]["limit"], 7)
            query_meta = entry["context"]["query_meta"]
            self.assertEqual(query_meta["intent"], "plan")
            self.assertEqual(query_meta["budget"], "brief")
            self.assertEqual(query_meta["anchor_type"], "task")
            self.assertEqual(query_meta["anchor_id"], task.id)
            self.assertEqual(query_meta["scope"]["workflow_id"], workflow.id)
            self.assertEqual(query_meta["scope"]["execution_id"], execution.id)
            self.assertEqual(query_meta["scope"]["run_id"], execution.id)
            self.assertEqual(query_meta["scope"]["task_id"], task.id)
            self.assertEqual(query_meta["scope"]["agent_id"], agent.id)
            counters = self.context.runtime_operations.snapshot_dict()["counters"]
            self.assertEqual(counters["graph_context.auto_retrieval.injections"], 1)
            self.assertEqual(counters["graph_context.auto_retrieval.injections.subagent_start"], 1)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reset_settings_cache()

    async def test_runtime_auto_retriever_uses_root_cause_scope_after_execution_failure(self) -> None:
        previous = os.environ.get("GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED")
        try:
            os.environ["GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED"] = "true"
            reset_settings_cache()

            reader = FakeAgencyGraphReader(
                GraphReadDocument(
                    nodes=[
                        GraphReadNode(
                            id="run-failure-graph",
                            type="Run",
                            labels=["Run"],
                            properties={"name": "Failed run", "status": "failed"},
                        ),
                        GraphReadNode(
                            id="decision-failure-graph",
                            type="Decision",
                            labels=["Decision"],
                            properties={"summary": "Retry only after validating the tool input."},
                        ),
                    ],
                    edges=[
                        GraphReadEdge(
                            id="edge-failure-decision",
                            source="run-failure-graph",
                            target="decision-failure-graph",
                            type="HAS_DECISION",
                        )
                    ],
                )
            )
            self.context.graph_read_service = reader
            workflow = WorkflowDefinition(
                id="workflow-failure-graph",
                name="Failure Graph Workflow",
                nodes=[],
                entrypoint="",
                task_definitions=[],
                agent_definitions=[],
            )
            execution = Execution(
                id="run-failure-graph",
                workflow_id=workflow.id,
                runtime_adapter="native",
                input_json={"prompt": "debug failure"},
            )
            await self.context.execution_store.save_execution(execution)
            await self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id=execution.id,
                    workflow_id=workflow.id,
                    event_type=ExecutionEventType.LLM_REQUEST_CREATED,
                    model_request_id="model-request-failure",
                    payload_json={"model": "fake-model", "prompt": "debug failure"},
                )
            )
            await self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id=execution.id,
                    workflow_id=workflow.id,
                    event_type=ExecutionEventType.TOOL_CALL_FAILED,
                    tool_call_id="tool-call-failure",
                    payload_json={"tool_name": "Fail Tool", "error": "boom"},
                )
            )
            failure_event = await self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id=execution.id,
                    workflow_id=workflow.id,
                    event_type=ExecutionEventType.EXECUTION_FAILED,
                    payload_json={"error": "boom"},
                )
            )
            await self.context.execution_store.save_artifact(
                ExecutionArtifact(
                    execution_id=execution.id,
                    event_id=failure_event.id,
                    artifact_type="log",
                    name="failure-log",
                    file_path="memory://failure-log",
                    metadata_json={"source": "test"},
                )
            )
            state = NativeExecutionState(execution_id=execution.id, workflow_id=workflow.id)

            entry = await RuntimeGraphContextAutoRetriever(self.context).retrieve_after_execution_failed(
                workflow,
                execution,
                state,
                "boom",
                failure_event.id,
            )

            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry["trigger"], "execution_failed")
            self.assertEqual(entry["intent"], "root_cause")
            self.assertEqual(entry["budget"], "balanced")
            self.assertEqual(entry["anchor_type"], "run")
            self.assertEqual(entry["anchor_id"], execution.id)
            self.assertEqual(entry["failure_event_id"], failure_event.id)
            self.assertTrue(any(item["event_type"] == "execution.failed" for item in entry["failed_events"]))
            self.assertTrue(any(item["event_type"] == "tool.call.failed" for item in entry["tool_calls"]))
            self.assertTrue(any(item["event_type"] == "llm.request.created" for item in entry["model_requests"]))
            self.assertEqual(entry["artifacts"][0]["name"], "failure-log")
            self.assertTrue(entry["prior_attempts"])
            self.assertEqual(reader.calls[0][0], "get_neighborhood")
            self.assertEqual(reader.calls[0][1]["node_id"], execution.id)
            query_meta = entry["context"]["query_meta"]
            self.assertEqual(query_meta["intent"], "root_cause")
            self.assertEqual(query_meta["budget"], "balanced")
            self.assertEqual(query_meta["anchor_type"], "run")
            self.assertEqual(query_meta["anchor_id"], execution.id)
            self.assertEqual(query_meta["scope"]["failure_event_id"], failure_event.id)
            self.assertTrue(query_meta["scope"]["failed_events"])
            self.assertTrue(query_meta["scope"]["tool_calls"])
            self.assertTrue(query_meta["scope"]["artifacts"])
            self.assertTrue(query_meta["scope"]["model_requests"])
        finally:
            if previous is None:
                os.environ.pop("GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED", None)
            else:
                os.environ["GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED"] = previous
            reset_settings_cache()

    async def test_runtime_auto_retriever_attaches_handoff_metadata_after_context_compaction(self) -> None:
        previous = os.environ.get("GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED")
        try:
            os.environ["GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED"] = "true"
            reset_settings_cache()

            await self.context.memory_repo.create(
                MemoryRecord(
                    id="context-pack-handoff",
                    scope=MemoryScope.GLOBAL,
                    content="Runtime Context Compaction Summary\n\nPrior compacted messages.",
                    summary="Compacted handoff pack",
                    tags=["context_pack", "runtime_context_compaction"],
                    memory_type=MemoryType.CONTEXT_PACK,
                    source="runtime_context_compaction",
                    metadata={"compaction_reason": "context_health_threshold"},
                )
            )
            reader = FakeAgencyGraphReader(
                GraphReadDocument(
                    nodes=[
                        GraphReadNode(
                            id="run-compaction-graph",
                            type="Run",
                            labels=["Run"],
                            properties={"status": "running"},
                        ),
                        GraphReadNode(
                            id="decision-compaction-graph",
                            type="Decision",
                            labels=["Decision"],
                            properties={"summary": "Preserve the user-facing contract."},
                        ),
                        GraphReadNode(
                            id="constraint-compaction-graph",
                            type="Constraint",
                            labels=["Constraint"],
                            properties={"summary": "Keep context-pack metadata compact."},
                        ),
                        GraphReadNode(
                            id="next-action-compaction-graph",
                            type="NextAction",
                            labels=["NextAction"],
                            properties={"summary": "Continue with proposal-tool trigger."},
                        ),
                    ],
                    edges=[
                        GraphReadEdge(
                            id="edge-compaction-decision",
                            source="run-compaction-graph",
                            target="decision-compaction-graph",
                            type="HAS_DECISION",
                        )
                    ],
                )
            )
            self.context.graph_read_service = reader
            agent = AgentDefinition(
                id="agent-compaction-graph",
                name="Compaction Graph Agent",
                model_profile_id="profile-compaction-graph",
                graph_context=GraphContextSettings(
                    enabled=True,
                    auto_retrieval_enabled=True,
                    include_memories=True,
                    max_records=9,
                ),
            )
            task = TaskDefinition(
                id="task-compaction-graph",
                name="Compaction Graph Task",
                description="Continue after compaction.",
                agent_id=agent.id,
            )
            workflow = WorkflowDefinition(
                id="workflow-compaction-graph",
                name="Compaction Graph Workflow",
                nodes=[
                    WorkflowNodeDefinition(
                        id="node-compaction-graph",
                        name="Compaction Graph Node",
                        node_type="task",
                        task_id=task.id,
                        agent_id=agent.id,
                    )
                ],
                entrypoint="node-compaction-graph",
                task_definitions=[task],
                agent_definitions=[agent],
                metadata={"conversation_id": "conversation-compaction-graph"},
            )
            execution = Execution(
                id="run-compaction-graph",
                workflow_id=workflow.id,
                runtime_adapter="native",
                input_json={"prompt": "continue after compaction"},
            )
            state = NativeExecutionState(execution_id=execution.id, workflow_id=workflow.id)
            state.current_node_id = "node-compaction-graph"
            record = ContextCompactionRecord(
                compacted=True,
                reason="context_health_threshold",
                memory_id="context-pack-handoff",
                source_model_request_id="model-request-compaction",
                estimated_tokens_saved=321,
                metadata={"source_event_start_sequence": 1, "source_event_end_sequence": 8},
            )

            entry = await RuntimeGraphContextAutoRetriever(self.context).retrieve_after_context_compaction(
                workflow,
                task,
                agent,
                execution,
                state,
                record,
            )

            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry["trigger"], "context_compaction")
            self.assertEqual(entry["intent"], "handoff")
            self.assertEqual(entry["budget"], "brief")
            self.assertEqual(entry["anchor_type"], "run")
            self.assertEqual(entry["anchor_id"], execution.id)
            self.assertEqual(entry["context_pack_id"], "context-pack-handoff")
            self.assertTrue(entry["graph_context_metadata_attached"])
            self.assertEqual(reader.calls[0][0], "get_neighborhood")
            self.assertEqual(reader.calls[0][1]["node_id"], execution.id)
            self.assertEqual(reader.calls[0][1]["limit"], 9)
            query_meta = entry["context"]["query_meta"]
            self.assertEqual(query_meta["intent"], "handoff")
            self.assertEqual(query_meta["budget"], "brief")
            self.assertEqual(query_meta["anchor_type"], "run")
            self.assertEqual(query_meta["anchor_id"], execution.id)
            self.assertEqual(query_meta["scope"]["trigger"], "context_compaction")
            self.assertEqual(query_meta["scope"]["context_pack_id"], "context-pack-handoff")
            self.assertEqual(query_meta["scope"]["conversation_id"], "conversation-compaction-graph")

            updated_pack = await self.context.memory_repo.get("context-pack-handoff")
            self.assertIsNotNone(updated_pack)
            assert updated_pack is not None
            metadata = updated_pack.metadata["runtime_graph_context"]
            self.assertEqual(metadata["trigger"], "context_compaction")
            self.assertEqual(metadata["query_meta"]["intent"], "handoff")
            self.assertEqual(metadata["query_meta"]["anchor_type"], "run")
            self.assertEqual(metadata["query_meta"]["anchor_id"], execution.id)
            self.assertEqual(metadata["section_counts"]["decisions"], 1)
            self.assertEqual(metadata["section_counts"]["constraints"], 1)
            self.assertEqual(metadata["section_counts"]["next_actions"], 1)
            self.assertIn("run-compaction-graph", metadata["provenance"]["node_ids"])
        finally:
            if previous is None:
                os.environ.pop("GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED", None)
            else:
                os.environ["GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED"] = previous
            reset_settings_cache()

    async def test_runtime_auto_retriever_uses_graph_context_before_proposal_tools(self) -> None:
        previous = os.environ.get("GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED")
        try:
            os.environ["GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED"] = "true"
            reset_settings_cache()

            reader = FakeAgencyGraphReader(
                GraphReadDocument(
                    nodes=[
                        GraphReadNode(
                            id="workflow-proposal-graph",
                            type="Workflow",
                            labels=["Workflow"],
                            properties={"name": "Existing workflow"},
                        ),
                        GraphReadNode(
                            id="decision-proposal-graph",
                            type="Decision",
                            labels=["Decision"],
                            properties={"summary": "Prefer explicit approval boundaries."},
                        ),
                        GraphReadNode(
                            id="constraint-proposal-graph",
                            type="Constraint",
                            labels=["Constraint"],
                            properties={"summary": "Do not mutate live runs without approval."},
                        ),
                    ],
                    edges=[
                        GraphReadEdge(
                            id="edge-proposal-decision",
                            source="workflow-proposal-graph",
                            target="decision-proposal-graph",
                            type="HAS_DECISION",
                        )
                    ],
                )
            )
            self.context.graph_read_service = reader
            tools = {
                tool.id: tool
                for tool in (
                    workflow_system_tool_definitions()
                    + tool_management_system_tool_definitions()
                    + agent_management_system_tool_definitions()
                )
            }
            agent = AgentDefinition(
                id="agent-proposal-graph",
                name="Proposal Graph Agent",
                model_profile_id="profile-proposal-graph",
                graph_context=GraphContextSettings(
                    enabled=True,
                    auto_retrieval_enabled=True,
                    include_events=True,
                    default_budget="balanced",
                    max_records=11,
                ),
            )
            task = TaskDefinition(
                id="task-proposal-graph",
                name="Proposal Graph Task",
                description="Prepare safe mutation proposals.",
                agent_id=agent.id,
            )
            workflow = WorkflowDefinition(
                id="workflow-proposal-graph",
                name="Proposal Graph Workflow",
                nodes=[
                    WorkflowNodeDefinition(
                        id="node-proposal-graph",
                        name="Proposal Graph Node",
                        node_type="task",
                        task_id=task.id,
                        agent_id=agent.id,
                    )
                ],
                entrypoint="node-proposal-graph",
                task_definitions=[task],
                agent_definitions=[agent],
                metadata={"conversation_id": "conversation-proposal-graph"},
            )
            execution = Execution(
                id="run-proposal-graph",
                workflow_id=workflow.id,
                runtime_adapter="native",
                input_json={"prompt": "prepare proposal"},
            )
            state = NativeExecutionState(execution_id=execution.id, workflow_id=workflow.id)
            state.current_node_id = "node-proposal-graph"
            cases = [
                (
                    "agency.workflow.propose-create",
                    {"summary": "Create a workflow", "goal": "Build a safe workflow."},
                    "plan",
                    "task",
                    task.id,
                    "workflow",
                    None,
                ),
                (
                    "agency.workflow.propose-update",
                    {"workflow_id": workflow.id, "summary": "Update workflow", "goal": "Add approval."},
                    "audit",
                    "workflow",
                    workflow.id,
                    "workflow",
                    workflow.id,
                ),
                (
                    "agency.tool.propose-create",
                    {"summary": "Create a tool", "tool": {"id": "tool-new-proposal", "name": "new_tool"}},
                    "plan",
                    "task",
                    task.id,
                    "tool",
                    "tool-new-proposal",
                ),
                (
                    "agency.tool.propose-update",
                    {"tool_id": "tool-existing-proposal", "summary": "Update tool"},
                    "audit",
                    "tool",
                    "tool-existing-proposal",
                    "tool",
                    "tool-existing-proposal",
                ),
                (
                    "agency.agent.propose-update",
                    {"agent_id": agent.id, "summary": "Update agent"},
                    "audit",
                    "agent",
                    agent.id,
                    "agent",
                    agent.id,
                ),
            ]

            for tool_id, arguments, intent, anchor_type, anchor_id, target_type, target_id in cases:
                reader.calls.clear()
                entry = await RuntimeGraphContextAutoRetriever(self.context).retrieve_before_proposal_tool(
                    workflow,
                    task,
                    agent,
                    execution,
                    state,
                    tools[tool_id],
                    arguments,
                    f"call-{tool_id}",
                )

                self.assertIsNotNone(entry)
                assert entry is not None
                self.assertEqual(entry["trigger"], "proposal_tool")
                self.assertEqual(entry["intent"], intent)
                self.assertEqual(entry["budget"], "balanced")
                self.assertEqual(entry["anchor_type"], anchor_type)
                self.assertEqual(entry["anchor_id"], anchor_id)
                self.assertEqual(entry["proposal_tool_id"], tool_id)
                self.assertEqual(entry["proposal_target_type"], target_type)
                self.assertEqual(entry["proposal_target_id"], target_id)
                self.assertEqual(reader.calls[0][0], "get_neighborhood")
                self.assertEqual(reader.calls[0][1]["node_id"], anchor_id)
                self.assertEqual(reader.calls[0][1]["limit"], 11)
                query_meta = entry["context"]["query_meta"]
                self.assertEqual(query_meta["intent"], intent)
                self.assertEqual(query_meta["budget"], "balanced")
                self.assertEqual(query_meta["anchor_type"], anchor_type)
                self.assertEqual(query_meta["anchor_id"], anchor_id)
                self.assertEqual(query_meta["scope"]["trigger"], "proposal_tool")
                self.assertEqual(query_meta["scope"]["proposal_tool_id"], tool_id)
                self.assertEqual(query_meta["scope"]["proposal_target_type"], target_type)
                self.assertEqual(query_meta["scope"]["proposal_target_id"], target_id)
        finally:
            if previous is None:
                os.environ.pop("GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED", None)
            else:
                os.environ["GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED"] = previous
            reset_settings_cache()

    async def test_runtime_auto_retriever_loop_guard_skips_without_progress(self) -> None:
        previous = {
            key: os.environ.get(key)
            for key in (
                "GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED",
                "GRAPH_CONTEXT_LOOP_GUARD_ENABLED",
            )
        }
        try:
            os.environ["GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED"] = "true"
            os.environ["GRAPH_CONTEXT_LOOP_GUARD_ENABLED"] = "true"
            reset_settings_cache()

            reader = FakeAgencyGraphReader(
                GraphReadDocument(
                    nodes=[
                        GraphReadNode(
                            id="workflow-loop-guard",
                            type="Workflow",
                            labels=["Workflow"],
                            properties={"name": "Loop Guard Workflow"},
                        )
                    ],
                    edges=[],
                )
            )
            self.context.graph_read_service = reader
            tool = next(
                item
                for item in workflow_system_tool_definitions()
                if item.id == "agency.workflow.propose-update"
            )
            agent = AgentDefinition(
                id="agent-loop-guard",
                name="Loop Guard Agent",
                model_profile_id="profile-loop-guard",
                graph_context=GraphContextSettings(enabled=True, auto_retrieval_enabled=True),
            )
            task = TaskDefinition(
                id="task-loop-guard",
                name="Loop Guard Task",
                description="Avoid repeated graph context.",
                agent_id=agent.id,
            )
            workflow = WorkflowDefinition(
                id="workflow-loop-guard",
                name="Loop Guard Workflow",
                nodes=[
                    WorkflowNodeDefinition(
                        id="node-loop-guard",
                        name="Loop Guard Node",
                        node_type="task",
                        task_id=task.id,
                        agent_id=agent.id,
                    )
                ],
                entrypoint="node-loop-guard",
                task_definitions=[task],
                agent_definitions=[agent],
            )
            execution = Execution(
                id="run-loop-guard",
                workflow_id=workflow.id,
                runtime_adapter="native",
                input_json={"prompt": "avoid loop"},
            )
            state = NativeExecutionState(execution_id=execution.id, workflow_id=workflow.id)
            state.current_node_id = "node-loop-guard"
            arguments = {"workflow_id": workflow.id, "summary": "Update workflow"}
            retriever = RuntimeGraphContextAutoRetriever(self.context)

            first_entry = await retriever.retrieve_before_proposal_tool(
                workflow,
                task,
                agent,
                execution,
                state,
                tool,
                arguments,
                "call-loop-guard-1",
            )
            self.assertIsNotNone(first_entry)
            assert first_entry is not None
            state.graph_context_entries.append(first_entry)

            second_entry = await retriever.retrieve_before_proposal_tool(
                workflow,
                task,
                agent,
                execution,
                state,
                tool,
                arguments,
                "call-loop-guard-2",
            )
            self.assertIsNotNone(second_entry)
            assert second_entry is not None
            self.assertTrue(second_entry["skipped"])
            self.assertEqual(second_entry["reason"], "auto_retrieval_loop_guard_no_progress")
            self.assertEqual(second_entry["context"]["status"], "skipped")
            self.assertEqual(len(reader.calls), 1)

            state.memory_entries.append({"tool_name": "propose_workflow_update", "output": {"status": "ok"}})
            third_entry = await retriever.retrieve_before_proposal_tool(
                workflow,
                task,
                agent,
                execution,
                state,
                tool,
                arguments,
                "call-loop-guard-3",
            )
            self.assertIsNotNone(third_entry)
            assert third_entry is not None
            self.assertFalse(third_entry.get("skipped", False))
            self.assertEqual(len(reader.calls), 2)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reset_settings_cache()

    async def test_runtime_auto_retriever_uses_resume_scope_before_coding_agent_start(self) -> None:
        previous = {
            key: os.environ.get(key)
            for key in (
                "GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED",
                "GRAPH_CONTEXT_CODING_AGENT_RESUME_ENABLED",
            )
        }
        try:
            os.environ["GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED"] = "true"
            os.environ["GRAPH_CONTEXT_CODING_AGENT_RESUME_ENABLED"] = "true"
            reset_settings_cache()

            await self.context.memory_repo.create(
                MemoryRecord(
                    id="memory-run-summary",
                    scope=MemoryScope.GLOBAL,
                    content="Previous run changed graph_context.py and left coding resume as the next step.",
                    summary="Previous coding run summary",
                    tags=["run_summary", "coding"],
                    memory_type=MemoryType.RUN_SUMMARY,
                    importance=70,
                )
            )
            reader = FakeAgencyGraphReader(
                GraphReadDocument(
                    nodes=[
                        GraphReadNode(
                            id="task-coding-graph",
                            type="Task",
                            labels=["Task"],
                            properties={"name": "Continue graph context coding"},
                        ),
                        GraphReadNode(
                            id="change-coding-graph",
                            type="CodeChange",
                            labels=["CodeChange"],
                            properties={"summary": "Added graph context failure retrieval.", "status": "completed"},
                        ),
                        GraphReadNode(
                            id="run-prior-failed-coding-graph",
                            type="WorkflowRun",
                            labels=["WorkflowRun"],
                            properties={
                                "summary": "Prior coding attempt repeated the same failing command.",
                                "status": "failed",
                            },
                        ),
                        GraphReadNode(
                            id="error-prior-failed-coding-graph",
                            type="Error",
                            labels=["Error"],
                            properties={"message": "Do not rerun the failing command without changing inputs."},
                        ),
                        GraphReadNode(
                            id="decision-coding-graph",
                            type="Decision",
                            labels=["Decision"],
                            properties={"summary": "Keep graph context read-only."},
                        ),
                        GraphReadNode(
                            id="constraint-coding-graph",
                            type="Constraint",
                            labels=["Constraint"],
                            properties={"summary": "Do not revert unrelated user changes."},
                        ),
                        GraphReadNode(
                            id="next-action-coding-graph",
                            type="NextAction",
                            labels=["NextAction"],
                            properties={"summary": "Implement coding-agent resume retrieval."},
                        ),
                        GraphReadNode(
                            id="memory-run-summary",
                            type="Memory",
                            labels=["Memory"],
                            properties={"memory_id": "memory-run-summary", "memory_type": "run_summary"},
                        ),
                    ],
                    edges=[
                        GraphReadEdge(
                            id="edge-coding-change",
                            source="task-coding-graph",
                            target="change-coding-graph",
                            type="HAS_CHANGE",
                        ),
                        GraphReadEdge(
                            id="edge-coding-failed-run",
                            source="task-coding-graph",
                            target="run-prior-failed-coding-graph",
                            type="HAS_PRIOR_RUN",
                        ),
                        GraphReadEdge(
                            id="edge-coding-error",
                            source="run-prior-failed-coding-graph",
                            target="error-prior-failed-coding-graph",
                            type="FAILED_WITH",
                        ),
                    ],
                )
            )
            self.context.graph_read_service = reader
            agent = AgentDefinition(
                id="agent-coder",
                name="Coder",
                role="Senior Software Engineer",
                model_profile_id="profile-codex",
                tool_ids=["agency.command.run"],
                graph_context=GraphContextSettings(
                    enabled=True,
                    auto_retrieval_enabled=True,
                    coding_agent_resume_enabled=True,
                    default_budget="balanced",
                    max_records=12,
                ),
            )
            task = TaskDefinition(
                id="task-coding-graph",
                name="Continue Graph Memory Tools",
                description="Continue implementation without repeating completed graph work.",
                expected_output="Focused diff and test evidence",
                agent_id=agent.id,
                metadata={"workspace_id": "workspace-agency", "repository": "agency"},
            )
            workflow = WorkflowDefinition(
                id="workflow-coding-graph",
                name="Coding Graph Workflow",
                nodes=[
                    WorkflowNodeDefinition(
                        id="node-coding-graph",
                        name="Coding Node",
                        node_type="task",
                        task_id=task.id,
                        agent_id=agent.id,
                    )
                ],
                entrypoint="node-coding-graph",
                task_definitions=[task],
                agent_definitions=[agent],
                metadata={"conversation_id": "conversation-coding-graph"},
            )
            execution = Execution(
                id="run-coding-graph",
                workflow_id=workflow.id,
                runtime_adapter="native",
                input_json={"repo_path": "/workspace/agency", "prompt": "continue"},
            )
            state = NativeExecutionState(execution_id=execution.id, workflow_id=workflow.id)
            state.current_node_id = "node-coding-graph"

            entry = await RuntimeGraphContextAutoRetriever(self.context).retrieve_before_subagent_start(
                workflow,
                task,
                agent,
                execution,
                execution.input_payload,
                state,
            )

            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry["trigger"], "coding_agent_start")
            self.assertEqual(entry["intent"], "resume")
            self.assertEqual(entry["budget"], "balanced")
            self.assertEqual(entry["anchor_type"], "task")
            self.assertEqual(entry["anchor_id"], task.id)
            self.assertEqual(entry["workspace_id"], "workspace-agency")
            self.assertEqual(entry["repository"], "agency")
            self.assertEqual(entry["repo_path"], "/workspace/agency")
            self.assertEqual(entry["conversation_id"], "conversation-coding-graph")
            self.assertTrue(entry["prior_changes"])
            self.assertTrue(entry["decisions"])
            self.assertTrue(entry["run_summaries"])
            self.assertTrue(entry["constraints"])
            self.assertTrue(entry["next_actions"])
            self.assertTrue(entry["prior_attempts"])
            self.assertTrue(entry["failures"])
            self.assertIn("Do not rerun the failing command", str(entry["failures"]))
            self.assertEqual(reader.calls[0][0], "get_neighborhood")
            self.assertEqual(reader.calls[0][1]["node_id"], task.id)
            self.assertEqual(reader.calls[0][1]["limit"], 12)
            query_meta = entry["context"]["query_meta"]
            self.assertEqual(query_meta["intent"], "resume")
            self.assertEqual(query_meta["budget"], "balanced")
            self.assertEqual(query_meta["anchor_type"], "task")
            self.assertEqual(query_meta["anchor_id"], task.id)
            self.assertEqual(query_meta["scope"]["trigger"], "coding_agent_start")
            self.assertEqual(query_meta["scope"]["workspace_id"], "workspace-agency")
            self.assertEqual(query_meta["scope"]["repo_path"], "/workspace/agency")
            self.assertEqual(query_meta["scope"]["needs"], [
                "prior_changes",
                "prior_attempts",
                "failures",
                "decisions",
                "run_summaries",
                "constraints",
                "next_actions",
            ])
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reset_settings_cache()

    def test_graph_tool_definitions_are_read_only_and_agent_assignable(self) -> None:
        tools = graph_system_tool_definitions()

        self.assertEqual(
            [tool.id for tool in tools],
            [
                "agency.graph.context",
                "agency.graph.search",
                "agency.graph.expand",
                "agency.graph.neighbors",
                "agency.graph.path",
                "agency.graph.summarize-subgraph",
                "agency.graph.working-set.create",
                "agency.graph.working-set.add",
                "agency.graph.working-set.remove",
                "agency.graph.working-set.summarize",
                "agency.graph.working-set.clear",
                "agency.graph.working-set.persist-context-pack",
            ],
        )
        context_tool = tools[0]
        search_tool = tools[1]
        expand_tool = tools[2]
        neighbors_tool = tools[3]
        path_tool = tools[4]
        summarize_tool = tools[5]
        self.assertTrue(context_tool.security.read_only)
        self.assertIn("agent_assignable", context_tool.tags)
        self.assertEqual(context_tool.implementation.target, "agency.system.graph")
        self.assertIn("anchor_type", context_tool.input_schema["properties"])
        self.assertIn("scope", context_tool.input_schema["properties"])
        self.assertEqual(context_tool.output_schema["properties"]["status"]["type"], "string")
        self.assertTrue(search_tool.security.read_only)
        self.assertIn("agent_assignable", search_tool.tags)
        self.assertEqual(search_tool.implementation.callable_name, "search_agency_graph")
        self.assertIn("node_types", search_tool.input_schema["properties"])
        self.assertEqual(search_tool.output_schema["properties"]["nodes"]["type"], "array")
        self.assertTrue(expand_tool.security.read_only)
        self.assertIn("agent_assignable", expand_tool.tags)
        self.assertEqual(expand_tool.implementation.callable_name, "expand_agency_graph_node")
        self.assertIn("preset", expand_tool.input_schema["properties"])
        self.assertEqual(expand_tool.output_schema["properties"]["edges"]["type"], "array")
        self.assertTrue(neighbors_tool.security.read_only)
        self.assertIn("agent_assignable", neighbors_tool.tags)
        self.assertEqual(neighbors_tool.implementation.callable_name, "list_agency_graph_neighbors")
        self.assertIn("relationship_types", neighbors_tool.input_schema["properties"])
        self.assertEqual(neighbors_tool.output_schema["properties"]["groups"]["type"], "array")
        self.assertTrue(path_tool.security.read_only)
        self.assertIn("agent_assignable", path_tool.tags)
        self.assertEqual(path_tool.implementation.callable_name, "find_agency_graph_path")
        self.assertIn("path_type", path_tool.input_schema["properties"])
        self.assertEqual(path_tool.output_schema["properties"]["nodes"]["type"], "array")
        self.assertTrue(summarize_tool.security.read_only)
        self.assertIn("agent_assignable", summarize_tool.tags)
        self.assertEqual(summarize_tool.implementation.callable_name, "summarize_agency_graph_subgraph")
        self.assertIn("nodes", summarize_tool.input_schema["properties"])
        self.assertEqual(summarize_tool.output_schema["properties"]["facts"]["type"], "array")
        working_set_tools = tools[6:]
        self.assertEqual(
            [tool.implementation.callable_name for tool in working_set_tools],
            [
                "create_agency_graph_working_set",
                "add_to_agency_graph_working_set",
                "remove_from_agency_graph_working_set",
                "summarize_agency_graph_working_set",
                "clear_agency_graph_working_set",
                "persist_agency_graph_working_set_context_pack",
            ],
        )
        self.assertFalse(working_set_tools[0].security.read_only)
        self.assertFalse(working_set_tools[1].security.read_only)
        self.assertFalse(working_set_tools[2].security.read_only)
        self.assertTrue(working_set_tools[3].security.read_only)
        self.assertFalse(working_set_tools[4].security.read_only)
        self.assertFalse(working_set_tools[5].security.read_only)
        self.assertIn("execution_id", working_set_tools[0].input_schema["required"])
        self.assertIn("working_set_id", working_set_tools[5].input_schema["required"])

    def test_graph_tool_definitions_do_not_expose_arbitrary_cypher(self) -> None:
        tools = graph_system_tool_definitions()
        tool_ids = {tool.id for tool in tools}

        self.assertNotIn("agency.graph.query", tool_ids)
        self.assertNotIn("agency.graph.cypher", tool_ids)
        self.assertNotIn("agency.graph.run-cypher", tool_ids)
        for tool in tools:
            property_names = _schema_property_names(tool.input_schema)
            forbidden_properties = {
                "cypher",
                "raw_cypher",
                "raw_query",
                "query_template",
                "query_template_id",
            }
            self.assertFalse(
                property_names & forbidden_properties,
                f"{tool.id} exposes forbidden graph query properties: {property_names & forbidden_properties}",
            )

        path_tool = next(tool for tool in tools if tool.id == "agency.graph.path")
        path_type_schema = path_tool.input_schema["properties"]["path_type"]
        self.assertEqual(
            path_type_schema["enum"],
            ["shortest", "memory_source_run", "failed_run_root_cause", "influence", "agent_prior_runs"],
        )
        self.assertIn("path_type", path_tool.input_schema["required"])

    async def test_runtime_dispatches_graph_context_tool(self) -> None:
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id="run-1",
                    type="WorkflowRun",
                    labels=["WorkflowRun"],
                    properties={"status": "failed", "name": "Failed import run"},
                ),
                GraphReadNode(
                    id="memory-1",
                    type="Memory",
                    labels=["Memory"],
                    properties={"summary": "Retry with smaller input batches."},
                ),
            ],
            edges=[GraphReadEdge(id="edge-1", source="run-1", target="memory-1", type="LINKS_MEMORY")],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)
        bus = RuntimeEventBus()
        set_default_runtime_event_bus(bus)
        subscriber = await bus.subscribe()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
                executor = ToolRuntimeExecutor(context=self.context, run_store=store)

                response = await executor.run_async(
                    "agency.graph.context",
                    {"anchor_type": "run", "anchor_id": "run-1", "intent": "debug", "include_raw_graph": True},
                    actor="user-graph",
                )
                await asyncio.sleep(0)
                await asyncio.sleep(0)

                self.assertEqual(response.verdict, "ok")
                self.assertEqual(response.result["status"], "ok")
                self.assertEqual(response.result["query_meta"]["depth"], 2)
                self.assertEqual(response.result["query_meta"]["node_count"], 2)
                self.assertEqual(response.result["query_meta"]["edge_count"], 1)
                self.assertEqual(response.result["related_memories"][0]["id"], "memory-1")
                self.assertEqual(response.result["graph"]["nodes"][0]["id"], "run-1")
                records = store.list_records()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].tool_name, "agency.graph.context")
                self.assertEqual(records[0].actor, "user-graph")

                events = []
                while not subscriber.empty():
                    events.append(await subscriber.get())
                graph_events = [
                    event
                    for event in events
                    if event.metadata.get("semanticType") == "agency.graph.context.completed"
                ]
                self.assertEqual(len(graph_events), 1)
                metadata = graph_events[0].metadata
                self.assertEqual(metadata["tool_id"], "agency.graph.context")
                self.assertEqual(metadata["actor"], "user-graph")
                self.assertEqual(metadata["status"], "ok")
                self.assertEqual(metadata["intent"], "debug")
                self.assertEqual(metadata["anchor_type"], "run")
                self.assertEqual(metadata["anchor_id"], "run-1")
                self.assertEqual(metadata["depth"], 2)
                self.assertEqual(metadata["limit"], 50)
                self.assertEqual(metadata["budget"], "balanced")
                self.assertEqual(metadata["node_count"], 2)
                self.assertEqual(metadata["edge_count"], 1)
                self.assertEqual(metadata["omitted_count"], 0)
                self.assertIsInstance(metadata["duration_ms"], int)
                self.assertEqual(metadata["graph_availability"], "available")
                self.assertFalse(metadata["fallback_used"])
                self.assertIsNone(metadata["error_kind"])
                self.assertEqual(metadata["graph_error_counters"]["graph_unavailable"], 0)
                self.assertEqual(metadata["graph_success_metrics"]["calls_by_intent"], {"debug": 1})
                self.assertEqual(metadata["graph_success_metrics"]["node_count"], 2)

                graph_tool_events = [
                    event
                    for event in events
                    if event.metadata.get("semanticType") == "agency.graph.tool.completed"
                ]
                self.assertEqual(len(graph_tool_events), 1)
                tool_metadata = graph_tool_events[0].metadata
                self.assertEqual(tool_metadata["tool_id"], "agency.graph.context")
                self.assertTrue(tool_metadata["success"])
                self.assertEqual(tool_metadata["node_count"], 2)
                self.assertEqual(tool_metadata["edge_count"], 1)
                self.assertGreater(tool_metadata["output_bytes"], 0)
                counters = self.context.runtime_operations.snapshot_dict()["counters"]
                self.assertEqual(counters["graph_tool.calls"], 1)
                self.assertEqual(counters["graph_tool.calls.agency_graph_context"], 1)
                self.assertEqual(counters["graph_tool.success"], 1)
                self.assertEqual(counters["graph_tool.intent.debug"], 1)
                self.assertEqual(counters["graph_tool.output.node_total"], 2)
                self.assertEqual(counters["graph_tool.output.edge_total"], 1)
            finally:
                set_default_runtime_event_bus(None)

    async def test_runtime_dispatches_graph_context_from_runtime_scope(self) -> None:
        self.context.graph_read_service = FakeAgencyGraphReader(
            GraphReadDocument(
                nodes=[
                    GraphReadNode(
                        id="execution-1",
                        type="WorkflowRun",
                        labels=["WorkflowRun"],
                        properties={"status": "running"},
                    )
                ],
                edges=[],
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(context=self.context, run_store=store)

            response = await executor.run_async(
                "agency.graph.context",
                {
                    "intent": "handoff",
                    "scope": {"runtime_context": {"execution_id": "execution-1", "workflow_id": "workflow-1"}},
                    "budget": "brief",
                },
                actor="user-graph",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["status"], "ok")
            self.assertEqual(response.result["query_meta"]["anchor_type"], "execution")
            self.assertEqual(response.result["query_meta"]["anchor_id"], "execution-1")
            self.assertEqual(
                response.result["query_meta"]["scope"]["runtime_context"],
                {"execution_id": "execution-1", "workflow_id": "workflow-1", "current_user_id": "user-graph"},
            )
            self.assertEqual(self.context.graph_read_service.calls[-1][0], "get_neighborhood")
            self.assertEqual(self.context.graph_read_service.calls[-1][1]["node_id"], "execution-1")

    async def test_runtime_graph_context_records_error_counters(self) -> None:
        self.context.graph_read_service = FakeAgencyGraphReader(unavailable=True)
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(context=self.context, run_store=store)

            response = await executor.run_async(
                "agency.graph.context",
                {"anchor_type": "run", "anchor_id": "run-1", "intent": "root_cause"},
                actor="user-graph",
            )

            self.assertEqual(response.verdict, "warn")
            self.assertEqual(response.result["status"], "graph_unavailable")
            counters = self.context.runtime_operations.snapshot_dict()["counters"]
            self.assertEqual(counters["graph_tool.calls"], 1)
            self.assertEqual(counters["graph_tool.errors"], 1)
            self.assertEqual(counters["graph_tool.errors.graph_unavailable"], 1)
            self.assertEqual(counters["graph_tool.intent.root_cause"], 1)

    async def test_runtime_graph_expand_rejects_depth_and_limit_over_contract_max(self) -> None:
        document = GraphReadDocument(
            nodes=[GraphReadNode(id="node-1", type="Task", labels=["Task"], properties={"name": "Task"})],
            edges=[],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(document)
        executor = ToolRuntimeExecutor(context=self.context)

        with self.assertRaises(ToolContractValidationError):
            await executor.run_async(
                "agency.graph.expand",
                {"node_id": "node-1", "depth": 99, "limit": 100},
                actor="user-graph",
            )
        with self.assertRaises(ToolContractValidationError):
            await executor.run_async(
                "agency.graph.expand",
                {"node_id": "node-1", "depth": 2, "limit": 999},
                actor="user-graph",
            )
        self.assertEqual(self.context.graph_read_service.calls, [])

    async def test_runtime_graph_path_rejects_max_depth_and_limit_over_contract_max(self) -> None:
        path_document = GraphReadDocument(
            nodes=[GraphReadNode(id="run-1", type="WorkflowRun", labels=["WorkflowRun"], properties={})],
            edges=[],
        )
        self.context.graph_read_service = FakeAgencyGraphReader(path_document=path_document)
        executor = ToolRuntimeExecutor(context=self.context)

        with self.assertRaises(ToolContractValidationError):
            await executor.run_async(
                "agency.graph.path",
                {"path_type": "failed_run_root_cause", "run_id": "run-1", "max_depth": 99, "limit": 100},
                actor="user-graph",
            )
        with self.assertRaises(ToolContractValidationError):
            await executor.run_async(
                "agency.graph.path",
                {"path_type": "failed_run_root_cause", "run_id": "run-1", "max_depth": 4, "limit": 999},
                actor="user-graph",
            )
        self.assertEqual(self.context.graph_read_service.calls, [])

    async def test_runtime_dispatches_graph_working_set_tools(self) -> None:
        execution = Execution(
            id="execution-working-set-tool",
            workflow_id="workflow-working-set-tool",
            runtime_adapter="native",
            input_json={"prompt": "explore graph"},
        )
        await self.context.execution_store.save_execution(execution)
        state = NativeExecutionState(execution_id=execution.id, workflow_id=execution.workflow_id)
        self.context.execution_engine._states[execution.id] = state

        executor = ToolRuntimeExecutor(context=self.context)
        create_response = await executor.run_async(
            "agency.graph.working-set.create",
            {
                "execution_id": execution.id,
                "owner_agent_id": "agent-working-set",
                "anchors": [{"type": "task", "id": "task-1"}],
                "notes": [{"summary": "start from task"}],
            },
            actor="user-graph",
        )
        working_set_id = create_response.result["working_set"]["working_set_id"]
        add_response = await executor.run_async(
            "agency.graph.working-set.add",
            {
                "execution_id": execution.id,
                "working_set_id": working_set_id,
                "visited_nodes": [{"id": "decision-1", "type": "Decision"}],
                "selected_nodes": [{"id": "decision-1", "type": "Decision"}],
            },
            actor="user-graph",
        )
        summarize_response = await executor.run_async(
            "agency.graph.working-set.summarize",
            {"execution_id": execution.id, "working_set_id": working_set_id},
            actor="user-graph",
        )
        remove_response = await executor.run_async(
            "agency.graph.working-set.remove",
            {
                "execution_id": execution.id,
                "working_set_id": working_set_id,
                "selected_node_ids": ["decision-1"],
            },
            actor="user-graph",
        )
        persist_response = await executor.run_async(
            "agency.graph.working-set.persist-context-pack",
            {
                "execution_id": execution.id,
                "working_set_id": working_set_id,
                "summary": "Persisted graph working set",
                "tags": ["handoff"],
            },
            actor="user-graph",
        )
        clear_response = await executor.run_async(
            "agency.graph.working-set.clear",
            {"execution_id": execution.id, "working_set_id": working_set_id},
            actor="user-graph",
        )

        self.assertEqual(create_response.verdict, "ok")
        self.assertEqual(add_response.result["working_set"]["visited_nodes"][0]["id"], "decision-1")
        self.assertEqual(summarize_response.result["counts"]["visited_nodes"], 1)
        self.assertEqual(remove_response.result["working_set"]["selected_nodes"], [])
        self.assertEqual(persist_response.result["memory"]["memory_type"], "context_pack")
        self.assertEqual(persist_response.result["memory"]["source"], "graph_working_set")
        self.assertEqual(
            persist_response.result["memory"]["metadata"]["graph_provenance"]["node_ids"],
            ["decision-1"],
        )
        self.assertEqual(persist_response.result["context_pack_id"], persist_response.result["memory"]["id"])
        self.assertTrue(clear_response.result["cleared"])
        self.assertEqual(state.graph_working_sets, {})

    async def test_graph_working_set_context_pack_requires_confirmation_for_sensitive_nodes(self) -> None:
        execution = Execution(
            id="execution-working-set-sensitive",
            workflow_id="workflow-working-set-sensitive",
            runtime_adapter="native",
            input_json={"prompt": "explore sensitive graph"},
        )
        await self.context.execution_store.save_execution(execution)
        state = NativeExecutionState(execution_id=execution.id, workflow_id=execution.workflow_id)
        self.context.execution_engine._states[execution.id] = state

        executor = ToolRuntimeExecutor(context=self.context)
        create_response = await executor.run_async(
            "agency.graph.working-set.create",
            {"execution_id": execution.id, "owner_agent_id": "agent-sensitive"},
            actor="user-graph",
        )
        working_set_id = create_response.result["working_set"]["working_set_id"]
        await executor.run_async(
            "agency.graph.working-set.add",
            {
                "execution_id": execution.id,
                "working_set_id": working_set_id,
                "selected_nodes": [{"id": "memory-sensitive", "type": "Memory", "sensitive": True}],
            },
            actor="user-graph",
        )

        blocked = await executor.run_async(
            "agency.graph.working-set.persist-context-pack",
            {"execution_id": execution.id, "working_set_id": working_set_id},
            actor="user-graph",
        )
        confirmed = await executor.run_async(
            "agency.graph.working-set.persist-context-pack",
            {"execution_id": execution.id, "working_set_id": working_set_id, "confirmed": True},
            actor="user-graph",
        )

        self.assertEqual(blocked.verdict, "warn")
        self.assertIn("Sensitive memory writes require explicit user confirmation", blocked.errors[0])
        self.assertTrue(confirmed.result["memory"]["sensitive"])
        self.assertTrue(
            confirmed.result["memory"]["metadata"]["graph_provenance_contains_sensitive_nodes"]
        )

    async def test_runtime_dispatches_graph_search_tool(self) -> None:
        self.context.graph_read_service = FakeAgencyGraphReader(
            GraphReadDocument(
                nodes=[
                    GraphReadNode(
                        id="memory-1",
                        type="Memory",
                        labels=["Memory"],
                        properties={"summary": "Prior integration decision"},
                    )
                ],
                edges=[GraphReadEdge(id="edge-ignored", source="memory-1", target="run-1", type="SOURCE_EXECUTION")],
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(context=self.context, run_store=store)

            response = await executor.run_async(
                "agency.graph.search",
                {
                    "query": "integration",
                    "labels": ["Memory"],
                    "node_types": ["Memory"],
                    "workflow_id": "workflow-1",
                    "limit": 10,
                },
                actor="user-graph",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["nodes"][0]["id"], "memory-1")
            self.assertEqual(response.result["edges"], [])
            self.assertEqual(response.result["meta"]["query"], "agency.graph.search")
            self.assertEqual(getattr(self.context.graph_read_service, "access_user_id"), "user-graph")
            self.assertEqual(
                self.context.graph_read_service.calls[-1],
                (
                    "search_nodes",
                    {
                        "query": "integration",
                        "labels": ["Memory"],
                        "node_types": ["Memory"],
                        "workflow_id": "workflow-1",
                        "agent_id": None,
                        "tool_id": None,
                        "document_id": None,
                        "entity_id": None,
                        "error_text": None,
                        "limit": 10,
                    },
                ),
            )
            self.assertEqual(store.list_records()[0].tool_name, "agency.graph.search")

    async def test_runtime_dispatches_graph_expand_tool(self) -> None:
        self.context.graph_read_service = FakeAgencyGraphReader(
            GraphReadDocument(
                nodes=[
                    GraphReadNode(
                        id="run-1",
                        type="WorkflowRun",
                        labels=["WorkflowRun"],
                        properties={"status": "failed"},
                    ),
                    GraphReadNode(
                        id="event-1",
                        type="ExecutionEvent",
                        labels=["ExecutionEvent"],
                        properties={"event_type": "execution.failed"},
                    ),
                ],
                edges=[
                    GraphReadEdge(id="edge-1", source="run-1", target="event-1", type="EMITTED_EVENT"),
                    GraphReadEdge(id="edge-2", source="run-1", target="memory-1", type="LINKS_MEMORY"),
                ],
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(context=self.context, run_store=store)

            response = await executor.run_async(
                "agency.graph.expand",
                {
                    "node_id": "run-1",
                    "preset": "workflow_run",
                    "depth": 2,
                    "limit": 1,
                },
                actor="user-graph",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["meta"]["query"], "agency.graph.expand")
            self.assertEqual(response.result["meta"]["node_id"], "run-1")
            self.assertEqual(response.result["meta"]["edge_count"], 1)
            self.assertEqual(response.result["meta"]["total_edge_count"], 2)
            self.assertEqual(response.result["edges"][0]["type"], "EMITTED_EVENT")
            self.assertEqual(getattr(self.context.graph_read_service, "access_user_id"), "user-graph")
            self.assertEqual(
                self.context.graph_read_service.calls[-1],
                (
                    "get_neighborhood",
                    {
                        "node_id": "run-1",
                        "labels": ["WorkflowRun"],
                        "relationship_types": GRAPH_NEIGHBORHOOD_PRESETS["workflow_run"]["relationship_types"],
                        "depth": 2,
                        "limit": 1,
                        "include_deleted": False,
                    },
                ),
            )
            self.assertEqual(store.list_records()[0].tool_name, "agency.graph.expand")

    async def test_runtime_dispatches_graph_neighbors_tool(self) -> None:
        self.context.graph_read_service = FakeAgencyGraphReader(
            GraphReadDocument(
                nodes=[
                    GraphReadNode(
                        id="run-1",
                        type="WorkflowRun",
                        labels=["WorkflowRun"],
                        properties={"status": "failed"},
                    ),
                    GraphReadNode(
                        id="event-1",
                        type="ExecutionEvent",
                        labels=["ExecutionEvent"],
                        properties={"event_type": "execution.failed"},
                    ),
                    GraphReadNode(
                        id="memory-1",
                        type="Memory",
                        labels=["Memory"],
                        properties={"summary": "Prior context"},
                    ),
                ],
                edges=[
                    GraphReadEdge(id="edge-1", source="run-1", target="event-1", type="EMITTED_EVENT"),
                    GraphReadEdge(id="edge-2", source="memory-1", target="run-1", type="SOURCE_EXECUTION"),
                ],
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(context=self.context, run_store=store)

            response = await executor.run_async(
                "agency.graph.neighbors",
                {
                    "node_id": "run-1",
                    "mode": "lineage",
                    "limit": 25,
                },
                actor="user-graph",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["meta"]["query"], "agency.graph.neighbors")
            self.assertEqual(response.result["meta"]["node_id"], "run-1")
            self.assertEqual(response.result["center"]["id"], "run-1")
            self.assertEqual(response.result["meta"]["neighbor_group_count"], 2)
            groups = {
                (group["relationship_type"], group["direction"], group["node_type"]): group
                for group in response.result["groups"]
            }
            self.assertIn(("EMITTED_EVENT", "outgoing", "ExecutionEvent"), groups)
            self.assertIn(("SOURCE_EXECUTION", "incoming", "Memory"), groups)
            self.assertEqual(getattr(self.context.graph_read_service, "access_user_id"), "user-graph")
            self.assertEqual(
                self.context.graph_read_service.calls[-1],
                (
                    "get_neighborhood",
                    {
                        "node_id": "run-1",
                        "labels": GRAPH_NEIGHBORHOOD_MODES["lineage"]["labels"],
                        "relationship_types": GRAPH_NEIGHBORHOOD_MODES["lineage"]["relationship_types"],
                        "depth": 1,
                        "limit": 25,
                        "include_deleted": False,
                    },
                ),
            )
            self.assertEqual(store.list_records()[0].tool_name, "agency.graph.neighbors")

    async def test_runtime_dispatches_graph_path_tool(self) -> None:
        self.context.graph_read_service = FakeAgencyGraphReader(
            path_document=GraphReadDocument(
                nodes=[
                    GraphReadNode(
                        id="memory-1",
                        type="Memory",
                        labels=["Memory"],
                        properties={"summary": "Prior context"},
                    ),
                    GraphReadNode(
                        id="run-1",
                        type="WorkflowRun",
                        labels=["WorkflowRun"],
                        properties={"status": "failed"},
                    ),
                ],
                edges=[GraphReadEdge(id="edge-1", source="memory-1", target="run-1", type="SOURCE_EXECUTION")],
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(context=self.context, run_store=store)

            response = await executor.run_async(
                "agency.graph.path",
                {
                    "path_type": "memory_source_run",
                    "memory_id": "memory-1",
                    "run_id": "run-1",
                    "max_depth": 3,
                    "limit": 10,
                },
                actor="user-graph",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["meta"]["query"], "agency.graph.path")
            self.assertEqual(response.result["meta"]["path_type"], "memory_source_run")
            self.assertEqual(response.result["edges"][0]["type"], "SOURCE_EXECUTION")
            self.assertEqual(getattr(self.context.graph_read_service, "access_user_id"), "user-graph")
            self.assertEqual(
                self.context.graph_read_service.calls[-1],
                (
                    "get_memory_source_run_path",
                    {"memory_id": "memory-1", "run_id": "run-1", "max_depth": 3, "limit": 10},
                ),
            )
            self.assertEqual(store.list_records()[0].tool_name, "agency.graph.path")

    async def test_runtime_dispatches_graph_summarize_subgraph_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlToolRunStore(Path(tmp) / "tool_runs.jsonl")
            executor = ToolRuntimeExecutor(context=self.context, run_store=store)

            response = await executor.run_async(
                "agency.graph.summarize-subgraph",
                {
                    "nodes": [
                        {
                            "id": "run-1",
                            "type": "WorkflowRun",
                            "labels": ["WorkflowRun"],
                            "properties": {"status": "failed", "summary": "Import failed"},
                        },
                        {
                            "id": "error-1",
                            "type": "Error",
                            "labels": ["Error"],
                            "properties": {"message": "Tool timed out", "token": "must-not-leak"},
                        },
                    ],
                    "edges": [
                        {"id": "edge-1", "source": "run-1", "target": "error-1", "type": "FAILED_WITH"}
                    ],
                    "intent": "debug",
                    "anchor_type": "run",
                    "anchor_id": "run-1",
                    "budget": "brief",
                },
                actor="user-graph",
            )

            self.assertEqual(response.verdict, "ok")
            self.assertEqual(response.result["status"], "ok")
            self.assertIn("run:run-1", response.result["summary"])
            self.assertIn("run-1 FAILED_WITH error-1", response.result["facts"])
            self.assertEqual(response.result["query_meta"]["query"], None)
            self.assertEqual(response.result["query_meta"]["anchor_id"], "run-1")
            self.assertEqual(response.result["query_meta"]["node_count"], 2)
            self.assertEqual(response.result["query_meta"]["edge_count"], 1)
            self.assertNotIn("must-not-leak", str(response.result))
            self.assertEqual(store.list_records()[0].tool_name, "agency.graph.summarize-subgraph")


if __name__ == "__main__":
    unittest.main()
