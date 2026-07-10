from __future__ import annotations

import asyncio
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.core.time import utc_now
from app.domain import Execution, ExecutionStatus
from app.graph.neo4j_read import GraphReadDocument, GraphReadEdge, GraphReadNode, Neo4jGraphReadError
from app.graph.service import (
    GRAPH_NEIGHBORHOOD_MODES,
    GRAPH_NEIGHBORHOOD_PRESETS,
    GRAPH_QUERY_PRESETS,
    graph_document_payload,
)


class FakeGraphReadService:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def get_node(self, node_id: str, *, labels=None):
        self.calls.append(("get_node", {"node_id": node_id, "labels": labels}))
        return GraphReadDocument(
            nodes=[GraphReadNode(id=node_id, type="Workflow", labels=["Workflow"], properties={"name": "Research"})],
            edges=[],
        )

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
        return GraphReadDocument(
            nodes=[
                GraphReadNode(id=node_id, type="Workflow", labels=["Workflow"], properties={"name": "Research"}),
                GraphReadNode(id="run-1", type="WorkflowRun", labels=["WorkflowRun"], properties={"status": "completed"}),
            ],
            edges=[GraphReadEdge(id="edge-1", source=node_id, target="run-1", type="HAS_RUN")],
            meta={"source": "fake"},
        )

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
        return GraphReadDocument(
            nodes=[GraphReadNode(id="memory-1", type="Memory", labels=["Memory"], properties={"summary": query or error_text})],
            edges=[],
        )

    async def get_workflow_lineage(self, workflow_id: str, *, limit=300):
        self.calls.append(("get_workflow_lineage", {"workflow_id": workflow_id, "limit": limit}))
        return GraphReadDocument(
            nodes=[GraphReadNode(id=workflow_id, type="Workflow", labels=["Workflow"])],
            edges=[],
        )

    async def get_shortest_path(self, source_id: str, target_id: str, *, relationship_types=None, max_depth=4, limit=1):
        self.calls.append(
            (
                "get_shortest_path",
                {
                    "source_id": source_id,
                    "target_id": target_id,
                    "relationship_types": relationship_types,
                    "max_depth": max_depth,
                    "limit": limit,
                },
            )
        )
        return self._path_document(source_id, target_id, query="shortest_path")

    async def get_memory_source_run_path(self, memory_id: str, *, run_id=None, max_depth=4, limit=25):
        self.calls.append(
            (
                "get_memory_source_run_path",
                {"memory_id": memory_id, "run_id": run_id, "max_depth": max_depth, "limit": limit},
            )
        )
        return self._path_document(memory_id, run_id or "run-1", query="memory_source_run_path")

    async def get_failed_run_root_cause_path(self, run_id: str, *, max_depth=3, limit=25):
        self.calls.append(
            ("get_failed_run_root_cause_path", {"run_id": run_id, "max_depth": max_depth, "limit": limit})
        )
        return self._path_document(run_id, "error-1", query="failed_run_root_cause_path")

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
        if anchor_type not in {"document", "entity"}:
            raise ValueError("Influence path anchor_type must be 'document' or 'entity'")
        return self._path_document(anchor_id, workflow_id or "workflow-1", query="influence_path")

    async def get_agent_prior_runs_path(self, agent_id: str, *, run_id=None, max_depth=3, limit=25):
        self.calls.append(
            (
                "get_agent_prior_runs_path",
                {"agent_id": agent_id, "run_id": run_id, "max_depth": max_depth, "limit": limit},
            )
        )
        return self._path_document(agent_id, run_id or "run-1", query="agent_prior_runs_path")

    async def get_graph_preset(
        self,
        preset: str,
        *,
        workflow_id=None,
        run_id=None,
        memory_id=None,
        agent_id=None,
        tool_id=None,
        persona_id=None,
        device_id=None,
        room=None,
        limit=50,
    ):
        self.calls.append(
            (
                "get_graph_preset",
                {
                    "preset": preset,
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "memory_id": memory_id,
                    "agent_id": agent_id,
                    "tool_id": tool_id,
                    "persona_id": persona_id,
                    "device_id": device_id,
                    "room": room,
                    "limit": limit,
                },
            )
        )
        if preset not in GRAPH_QUERY_PRESETS:
            raise ValueError(f"Unknown graph preset: {preset}")
        return self._path_document(
            workflow_id or run_id or memory_id or agent_id or tool_id or persona_id or preset,
            "preset-target",
            query="graph_preset",
        )

    def _path_document(self, source_id: str, target_id: str, *, query: str) -> GraphReadDocument:
        return GraphReadDocument(
            nodes=[
                GraphReadNode(id=source_id, type="Memory", labels=["Memory"]),
                GraphReadNode(id=target_id, type="WorkflowRun", labels=["WorkflowRun"]),
            ],
            edges=[GraphReadEdge(id="edge-path-1", source=source_id, target=target_id, type="RELATED_TO")],
            meta={"query": query},
        )


class UnavailableGraphReadService(FakeGraphReadService):
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
        raise Neo4jGraphReadError("projection unavailable")


class GraphReadApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_settings_cache()
        self.context = create_test_api_context()
        self.context.graph_read_service = FakeGraphReadService()
        self.client = TestClient(create_app(context=self.context))
        self.headers = {
            "x-agency-user-id": "admin-graph-read",
            "x-agency-user-email": "admin-graph-read@example.com",
        }
        self.client.post(
            "/users/sync",
            json={"id": "admin-graph-read", "email": "admin-graph-read@example.com", "roles": ["admin"]},
        )

    def tearDown(self) -> None:
        reset_settings_cache()

    def _create_bearer_token(self, *, scopes: list[str], name: str = "Graph read token") -> str:
        response = self.client.post(
            "/api-tokens",
            headers=self.headers,
            json={"name": name, "scopes": scopes},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["token"]

    def _save_execution(self, execution: Execution) -> None:
        asyncio.run(self.context.execution_store.save_execution(execution))

    def test_graph_read_status_uses_injected_reader(self) -> None:
        response = self.client.get("/graph/read/status", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": True, "available": True, "source": "injected"})

    def test_neighborhood_endpoint_returns_graph_document(self) -> None:
        response = self.client.get(
            "/graph/read/nodes/workflow-1/neighborhood",
            headers=self.headers,
            params={
                "labels": "Workflow",
                "relationship_types": "HAS_RUN,HAS_STEP_RUN",
                "depth": 2,
                "limit": 25,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["nodes"]), 2)
        self.assertEqual(payload["edges"][0]["type"], "HAS_RUN")
        self.assertEqual(payload["meta"]["projection_mode"], "neo4j")
        self.assertTrue(payload["meta"]["projection_available"])
        self.assertEqual(
            self.context.graph_read_service.calls[-1],
            (
                "get_neighborhood",
                {
                    "node_id": "workflow-1",
                    "labels": ["Workflow"],
                    "relationship_types": ["HAS_RUN", "HAS_STEP_RUN"],
                    "depth": 2,
                    "limit": 25,
                    "include_deleted": False,
                },
            ),
        )

    def test_expand_endpoint_uses_preset_and_returns_expansion_meta(self) -> None:
        response = self.client.get(
            "/graph/read/nodes/run-1/expand",
            headers=self.headers,
            params={"preset": "workflow_run", "depth": 2, "limit": 25},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["meta"]["query"], "expand")
        self.assertEqual(payload["meta"]["preset"], "workflow_run")
        self.assertEqual(payload["meta"]["node_id"], "run-1")
        self.assertEqual(payload["meta"]["depth"], 2)
        self.assertEqual(payload["meta"]["limit"], 25)
        self.assertFalse(payload["meta"]["truncated"])
        self.assertEqual(
            self.context.graph_read_service.calls[-1],
            (
                "get_neighborhood",
                {
                    "node_id": "run-1",
                    "labels": ["WorkflowRun"],
                    "relationship_types": GRAPH_NEIGHBORHOOD_PRESETS["workflow_run"]["relationship_types"],
                    "depth": 2,
                    "limit": 25,
                    "include_deleted": False,
                },
            ),
        )

    def test_expand_endpoint_uses_neighborhood_modes(self) -> None:
        for mode in ["operational", "knowledge", "lineage", "health", "cost", "security"]:
            with self.subTest(mode=mode):
                response = self.client.get(
                    "/graph/read/nodes/run-1/expand",
                    headers=self.headers,
                    params={"mode": mode, "depth": 2, "limit": 25},
                )

                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["meta"]["mode"], mode)
                self.assertEqual(payload["meta"]["relationship_types"], GRAPH_NEIGHBORHOOD_MODES[mode]["relationship_types"])
                self.assertEqual(
                    self.context.graph_read_service.calls[-1],
                    (
                        "get_neighborhood",
                        {
                            "node_id": "run-1",
                            "labels": GRAPH_NEIGHBORHOOD_MODES[mode]["labels"],
                            "relationship_types": GRAPH_NEIGHBORHOOD_MODES[mode]["relationship_types"],
                            "depth": 2,
                            "limit": 25,
                            "include_deleted": False,
                        },
                    ),
                )

    def test_neighbors_endpoint_returns_grouped_neighbors(self) -> None:
        response = self.client.get(
            "/graph/read/nodes/workflow-1/neighbors",
            headers=self.headers,
            params={"preset": "workflow", "limit": 25},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["center"]["id"], "workflow-1")
        self.assertEqual(payload["meta"]["query"], "neighbors")
        self.assertEqual(payload["meta"]["neighbor_group_count"], 1)
        self.assertEqual(payload["groups"][0]["relationship_type"], "HAS_RUN")
        self.assertEqual(payload["groups"][0]["direction"], "outgoing")
        self.assertEqual(payload["groups"][0]["node_type"], "WorkflowRun")
        self.assertEqual(payload["groups"][0]["nodes"][0]["id"], "run-1")
        self.assertEqual(
            self.context.graph_read_service.calls[-1],
            (
                "get_neighborhood",
                {
                    "node_id": "workflow-1",
                    "labels": GRAPH_NEIGHBORHOOD_PRESETS["workflow"]["labels"],
                    "relationship_types": GRAPH_NEIGHBORHOOD_PRESETS["workflow"]["relationship_types"],
                    "depth": 1,
                    "limit": 25,
                    "include_deleted": False,
                },
            ),
        )

    def test_expand_endpoint_prefers_explicit_filters_then_preset_then_mode(self) -> None:
        explicit_response = self.client.get(
            "/graph/read/nodes/run-1/expand",
            headers=self.headers,
            params={
                "mode": "knowledge",
                "preset": "workflow_run",
                "labels": "WorkflowRun",
                "relationship_types": "HAS_RUN",
            },
        )
        preset_response = self.client.get(
            "/graph/read/nodes/run-1/expand",
            headers=self.headers,
            params={"mode": "knowledge", "preset": "workflow_run"},
        )

        self.assertEqual(explicit_response.status_code, 200)
        self.assertEqual(preset_response.status_code, 200)
        self.assertEqual(
            self.context.graph_read_service.calls[-2],
            (
                "get_neighborhood",
                {
                    "node_id": "run-1",
                    "labels": ["WorkflowRun"],
                    "relationship_types": ["HAS_RUN"],
                    "depth": 1,
                    "limit": 100,
                    "include_deleted": False,
                },
            ),
        )
        self.assertEqual(
            self.context.graph_read_service.calls[-1],
            (
                "get_neighborhood",
                {
                    "node_id": "run-1",
                    "labels": GRAPH_NEIGHBORHOOD_PRESETS["workflow_run"]["labels"],
                    "relationship_types": GRAPH_NEIGHBORHOOD_PRESETS["workflow_run"]["relationship_types"],
                    "depth": 1,
                    "limit": 100,
                    "include_deleted": False,
                },
            ),
        )

    def test_expand_endpoint_rejects_unknown_mode(self) -> None:
        response = self.client.get(
            "/graph/read/nodes/run-1/expand",
            headers=self.headers,
            params={"mode": "unknown"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown graph neighborhood mode", response.json()["detail"])

    def test_expand_endpoint_rejects_unknown_preset(self) -> None:
        response = self.client.get(
            "/graph/read/nodes/run-1/expand",
            headers=self.headers,
            params={"preset": "unknown"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown graph neighborhood preset", response.json()["detail"])

    def test_expand_endpoint_requires_identity_before_traversal(self) -> None:
        response = self.client.get(
            "/graph/read/nodes/run-1/expand",
            params={"preset": "workflow_run"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.context.graph_read_service.calls, [])

    def test_expand_endpoint_requires_executions_read_scope_before_traversal(self) -> None:
        raw_token = self._create_bearer_token(scopes=["workflows:read"], name="Workflow-only token")
        self.context.graph_read_service.calls.clear()

        response = self.client.get(
            "/graph/read/nodes/run-1/expand",
            headers={"authorization": f"Bearer {raw_token}"},
            params={"preset": "workflow_run"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("executions:read", response.json()["detail"]["missingScopes"])
        self.assertEqual(self.context.graph_read_service.calls, [])

    def test_domain_neighborhood_endpoint_applies_memory_preset(self) -> None:
        response = self.client.get(
            "/graph/read/memories/memory-1/neighborhood",
            headers=self.headers,
            params={"limit": 20},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["meta"]["query"], "preset_neighborhood")
        self.assertEqual(payload["meta"]["preset"], "memory")
        self.assertEqual(
            self.context.graph_read_service.calls[-1],
            (
                "get_neighborhood",
                {
                    "node_id": "memory-1",
                    "labels": ["Memory"],
                    "relationship_types": GRAPH_NEIGHBORHOOD_PRESETS["memory"]["relationship_types"],
                    "depth": 1,
                    "limit": 20,
                    "include_deleted": False,
                },
            ),
        )

    def test_memory_neighborhood_can_include_operational_coverage(self) -> None:
        now = utc_now()
        self._save_execution(
            Execution(
                id="run-failed-1",
                workflow_id="workflow-1",
                runtime_adapter="native",
                status=ExecutionStatus.FAILED,
                error="Coordinator failed for tenant 12345",
                created_at=now - timedelta(minutes=20),
                updated_at=now - timedelta(minutes=19),
            )
        )
        self._save_execution(
            Execution(
                id="run-completed-1",
                workflow_id="workflow-2",
                runtime_adapter="native",
                status=ExecutionStatus.COMPLETED,
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=9),
            )
        )

        response = self.client.get(
            "/graph/read/memories/memory-1/neighborhood",
            headers=self.headers,
            params={
                "include_operational_coverage": "true",
                "recent_run_limit": 40,
                "workflow_run_limit": 24,
                "incident_limit": 12,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        operational = payload["operational"]
        node_ids = {node["id"] for node in operational["nodes"]}
        edge_types = {edge["type"] for edge in operational["edges"]}
        self.assertIn("workflow-1", node_ids)
        self.assertIn("workflow-2", node_ids)
        self.assertIn("run-failed-1", node_ids)
        self.assertIn("run-completed-1", node_ids)
        self.assertIn("error:run-failed-1", node_ids)
        self.assertTrue(any(node_id.startswith("incident-cluster:workflow-1:") for node_id in node_ids))
        self.assertIn("HAS_RUN", edge_types)
        self.assertIn("STARTED", edge_types)
        self.assertIn("FAILED_WITH", edge_types)
        self.assertIn("HAS_INCIDENT", edge_types)
        self.assertEqual(operational["coverage"]["recent_run_count"], 2)
        self.assertEqual(operational["coverage"]["workflow_count"], 2)
        self.assertEqual(operational["coverage"]["failed_count"], 1)
        self.assertEqual(payload["meta"]["include_operational_coverage"], True)

    def test_workflow_neighborhood_operational_coverage_is_workflow_scoped(self) -> None:
        now = utc_now()
        self._save_execution(
            Execution(
                id="run-workflow-1",
                workflow_id="workflow-1",
                runtime_adapter="native",
                status=ExecutionStatus.RUNNING,
                created_at=now - timedelta(minutes=8),
                updated_at=now - timedelta(minutes=7),
            )
        )
        self._save_execution(
            Execution(
                id="run-workflow-2",
                workflow_id="workflow-2",
                runtime_adapter="native",
                status=ExecutionStatus.FAILED,
                error="Other workflow failed",
                created_at=now - timedelta(minutes=6),
                updated_at=now - timedelta(minutes=5),
            )
        )

        response = self.client.get(
            "/graph/read/workflows/workflow-1/neighborhood",
            headers=self.headers,
            params={"include_operational_coverage": "true", "workflow_run_limit": 24},
        )

        self.assertEqual(response.status_code, 200)
        operational = response.json()["operational"]
        node_ids = {node["id"] for node in operational["nodes"]}
        self.assertIn("workflow-1", node_ids)
        self.assertIn("run-workflow-1", node_ids)
        self.assertNotIn("workflow-2", node_ids)
        self.assertNotIn("run-workflow-2", node_ids)
        self.assertEqual(operational["coverage"]["root_type"], "workflow")
        self.assertEqual(operational["coverage"]["total_run_count"], 1)

    def test_operational_coverage_falls_back_when_projection_is_unavailable(self) -> None:
        self.context.graph_read_service = UnavailableGraphReadService()
        now = utc_now()
        self._save_execution(
            Execution(
                id="run-fallback-1",
                workflow_id="workflow-1",
                runtime_adapter="native",
                status=ExecutionStatus.FAILED,
                error="Projection fallback failed run",
                created_at=now - timedelta(minutes=4),
                updated_at=now - timedelta(minutes=3),
            )
        )

        response = self.client.get(
            "/graph/read/memories/memory-1/neighborhood",
            headers=self.headers,
            params={"include_operational_coverage": "true", "recent_run_limit": 40},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["nodes"], [])
        self.assertEqual(payload["edges"], [])
        self.assertFalse(payload["meta"]["projection_available"])
        self.assertEqual(payload["meta"]["projection_fallback"], "operational_coverage")
        self.assertIn("projection unavailable", payload["meta"]["projection_unavailable_reason"])
        node_ids = {node["id"] for node in payload["operational"]["nodes"]}
        self.assertIn("workflow-1", node_ids)
        self.assertIn("run-fallback-1", node_ids)

    def test_domain_neighborhood_endpoint_applies_entity_preset(self) -> None:
        response = self.client.get(
            "/graph/read/entities/entity:organization:acme-corp/neighborhood",
            headers=self.headers,
            params={"depth": 2, "limit": 40},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["meta"]["query"], "preset_neighborhood")
        self.assertEqual(payload["meta"]["preset"], "entity")
        self.assertEqual(payload["meta"]["node_id"], "entity:organization:acme-corp")
        self.assertEqual(
            self.context.graph_read_service.calls[-1],
            (
                "get_neighborhood",
                {
                    "node_id": "entity:organization:acme-corp",
                    "labels": ["Entity"],
                    "relationship_types": [
                        "MENTIONS",
                        "PART_OF_DOCUMENT",
                        "HAS_CHUNK",
                        "LINKS_MEMORY",
                        "HAS_MEMORY_LINK",
                        "AVAILABLE_TO",
                        "SOURCE_EXECUTION",
                        "SOURCE_CONVERSATION",
                        "SUPERSEDES",
                    ],
                    "depth": 2,
                    "limit": 40,
                    "include_deleted": False,
                },
            ),
        )

    def test_search_and_workflow_lineage_endpoints(self) -> None:
        search_response = self.client.get(
            "/graph/read/search",
            headers=self.headers,
            params={"q": "memory", "labels": "Memory", "limit": 10},
        )
        lineage_response = self.client.get(
            "/graph/read/workflows/workflow-1/lineage",
            headers=self.headers,
            params={"limit": 10},
        )

        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(search_response.json()["nodes"][0]["id"], "memory-1")
        self.assertEqual(
            self.context.graph_read_service.calls[-2],
            (
                "search_nodes",
                {
                    "query": "memory",
                    "labels": ["Memory"],
                    "node_types": [],
                    "workflow_id": None,
                    "agent_id": None,
                    "tool_id": None,
                    "document_id": None,
                    "entity_id": None,
                    "error_text": None,
                    "limit": 10,
                },
            ),
        )
        self.assertEqual(lineage_response.status_code, 200)
        self.assertEqual(lineage_response.json()["nodes"][0]["id"], "workflow-1")

    def test_search_endpoint_accepts_canonical_filters_without_text_query(self) -> None:
        response = self.client.get(
            "/graph/read/search",
            headers=self.headers,
            params={
                "node_types": "Error,ExecutionEvent",
                "workflow_id": "workflow-1",
                "agent_id": "agent-1",
                "tool_id": "tool-1",
                "document_id": "doc-1",
                "entity_id": "entity:organization:acme-corp",
                "error_text": "timeout",
                "limit": 25,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.context.graph_read_service.calls[-1],
            (
                "search_nodes",
                {
                    "query": None,
                    "labels": [],
                    "node_types": ["Error", "ExecutionEvent"],
                    "workflow_id": "workflow-1",
                    "agent_id": "agent-1",
                    "tool_id": "tool-1",
                    "document_id": "doc-1",
                    "entity_id": "entity:organization:acme-corp",
                    "error_text": "timeout",
                    "limit": 25,
                },
            ),
        )

    def test_routes_attach_current_user_to_graph_reader(self) -> None:
        response = self.client.get(
            "/graph/read/search",
            headers=self.headers,
            params={"q": "memory", "labels": "Memory"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(getattr(self.context.graph_read_service, "access_user_id"), "admin-graph-read")

    def test_graph_payload_enforces_edge_and_output_budgets(self) -> None:
        document = GraphReadDocument(
            nodes=[
                GraphReadNode(id="node-1", type="Memory", labels=["Memory"], properties={"summary": "x" * 600}),
                GraphReadNode(id="node-2", type="Memory", labels=["Memory"], properties={"summary": "y" * 600}),
                GraphReadNode(id="node-3", type="Memory", labels=["Memory"], properties={"summary": "z" * 600}),
            ],
            edges=[
                GraphReadEdge(id="edge-1", source="node-1", target="node-2", type="LINKS_MEMORY"),
                GraphReadEdge(id="edge-2", source="node-2", target="node-3", type="LINKS_MEMORY"),
            ],
        )

        payload = graph_document_payload(document, limit=2, max_edges=1, max_output_bytes=900)
        payload_size = len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))

        self.assertLessEqual(payload_size, 900)
        self.assertEqual(payload["meta"]["output_bytes"], payload_size)
        self.assertEqual(payload["meta"]["total_edge_count"], 2)
        self.assertEqual(payload["meta"]["edge_count"], 0)
        self.assertEqual(payload["meta"]["projection_mode"], "neo4j")
        self.assertTrue(payload["meta"]["projection_available"])
        self.assertTrue(payload["meta"]["edge_truncated"])
        self.assertTrue(payload["meta"]["output_truncated"])

    def test_path_endpoints_call_bounded_reader_methods(self) -> None:
        shortest_response = self.client.get(
            "/graph/read/paths/shortest",
            headers=self.headers,
            params={
                "source_id": "memory-1",
                "target_id": "run-1",
                "relationship_types": "SOURCE_EXECUTION,HAS_RUN",
                "max_depth": 4,
                "limit": 2,
            },
        )
        memory_response = self.client.get(
            "/graph/read/paths/memory-source-run",
            headers=self.headers,
            params={"memory_id": "memory-1", "run_id": "run-1", "max_depth": 4, "limit": 10},
        )
        failed_response = self.client.get(
            "/graph/read/paths/failed-run-root-cause",
            headers=self.headers,
            params={"run_id": "run-1", "max_depth": 3, "limit": 10},
        )
        influence_response = self.client.get(
            "/graph/read/paths/influence",
            headers=self.headers,
            params={"anchor_type": "entity", "anchor_id": "entity-1", "workflow_id": "workflow-1", "limit": 10},
        )
        agent_response = self.client.get(
            "/graph/read/paths/agent-prior-runs",
            headers=self.headers,
            params={"agent_id": "agent-1", "run_id": "run-1", "limit": 10},
        )

        self.assertEqual(shortest_response.status_code, 200)
        self.assertEqual(memory_response.status_code, 200)
        self.assertEqual(failed_response.status_code, 200)
        self.assertEqual(influence_response.status_code, 200)
        self.assertEqual(agent_response.status_code, 200)
        self.assertEqual(shortest_response.json()["meta"]["query"], "shortest_path")
        self.assertEqual(
            self.context.graph_read_service.calls[-5],
            (
                "get_shortest_path",
                {
                    "source_id": "memory-1",
                    "target_id": "run-1",
                    "relationship_types": ["SOURCE_EXECUTION", "HAS_RUN"],
                    "max_depth": 4,
                    "limit": 2,
                },
            ),
        )
        self.assertEqual(
            self.context.graph_read_service.calls[-4],
            (
                "get_memory_source_run_path",
                {"memory_id": "memory-1", "run_id": "run-1", "max_depth": 4, "limit": 10},
            ),
        )
        self.assertEqual(
            self.context.graph_read_service.calls[-3],
            ("get_failed_run_root_cause_path", {"run_id": "run-1", "max_depth": 3, "limit": 10}),
        )
        self.assertEqual(
            self.context.graph_read_service.calls[-2],
            (
                "get_influence_path",
                {
                    "anchor_id": "entity-1",
                    "anchor_type": "entity",
                    "workflow_id": "workflow-1",
                    "max_depth": 4,
                    "limit": 10,
                },
            ),
        )
        self.assertEqual(
            self.context.graph_read_service.calls[-1],
            ("get_agent_prior_runs_path", {"agent_id": "agent-1", "run_id": "run-1", "max_depth": 3, "limit": 10}),
        )

    def test_influence_path_endpoint_rejects_invalid_anchor_type(self) -> None:
        response = self.client.get(
            "/graph/read/paths/influence",
            headers=self.headers,
            params={"anchor_type": "memory", "anchor_id": "memory-1"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("anchor_type", response.json()["detail"])

    def test_graph_preset_endpoint_runs_named_templates(self) -> None:
        preset_params = {
            "recent_failures": {"workflow_id": "workflow-1"},
            "failed_run_root_cause": {"run_id": "run-1"},
            "workflow_lineage": {"workflow_id": "workflow-1"},
            "memory_provenance": {"memory_id": "memory-1"},
            "stale_context": {"workflow_id": "workflow-1"},
            "missing_embeddings": {"workflow_id": "workflow-1"},
            "high_cost_runs": {"workflow_id": "workflow-1"},
            "tool_failure_hotspots": {"tool_id": "tool-1"},
            "sub_agent_steering": {"agent_id": "agent-1"},
            "coding_agent_resume": {"workflow_id": "workflow-1", "agent_id": "agent-1"},
            "persona_lineage": {"persona_id": "persona-1"},
            "persona_capability_map": {"persona_id": "persona-1"},
            "physical_device_audit": {"device_id": "device-light-1"},
            "physical_room_context": {"room": "Living Room"},
        }

        for preset, params in preset_params.items():
            with self.subTest(preset=preset):
                response = self.client.get(
                    f"/graph/read/presets/{preset}",
                    headers=self.headers,
                    params={**params, "limit": 10},
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["meta"]["preset"], preset)
                self.assertEqual(self.context.graph_read_service.calls[-1][0], "get_graph_preset")
                self.assertEqual(self.context.graph_read_service.calls[-1][1]["preset"], preset)
                self.assertEqual(self.context.graph_read_service.calls[-1][1]["limit"], 10)

        self.assertEqual(
            self.context.graph_read_service.calls[-2][1]["device_id"],
            "device-light-1",
        )
        self.assertEqual(
            self.context.graph_read_service.calls[-1][1]["room"],
            "Living Room",
        )

    def test_physical_neighborhood_presets_are_registered(self) -> None:
        self.assertIn("physical_device", GRAPH_NEIGHBORHOOD_PRESETS)
        self.assertIn("physical_audit", GRAPH_NEIGHBORHOOD_PRESETS)
        self.assertIn("Device", GRAPH_NEIGHBORHOOD_PRESETS["physical_device"]["labels"])
        self.assertIn(
            "INFLUENCED_DEVICE_COMMAND",
            GRAPH_NEIGHBORHOOD_PRESETS["physical_audit"]["relationship_types"],
        )

    def test_graph_preset_endpoint_rejects_unknown_preset(self) -> None:
        response = self.client.get(
            "/graph/read/presets/unknown",
            headers=self.headers,
            params={"limit": 10},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown graph preset", response.json()["detail"])

    def test_node_endpoint_reports_disabled_without_reader(self) -> None:
        with patch.dict("os.environ", {"NEO4J_ENABLED": "false"}, clear=False):
            reset_settings_cache()
            context = create_test_api_context()
            client = TestClient(create_app(context=context))
            client.post(
                "/users/sync",
                json={"id": "admin-graph-read", "email": "admin-graph-read@example.com", "roles": ["admin"]},
            )

            response = client.get("/graph/read/nodes/workflow-1", headers=self.headers)

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
