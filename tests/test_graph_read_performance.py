from __future__ import annotations

import time
import unittest

from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.graph.neo4j_read import GraphReadDocument, GraphReadEdge, GraphReadNode
from app.graph.service import GRAPH_NEIGHBORHOOD_PRESETS


class LargeGraphReadService:
    def __init__(self):
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
        nodes = [
            GraphReadNode(
                id=f"{node_id}-node-{index}",
                type="StepRun" if index else "WorkflowRun",
                labels=["StepRun"] if index else ["WorkflowRun"],
                properties={"name": f"Node {index}", "sequence": index},
            )
            for index in range(limit)
        ]
        edges = [
            GraphReadEdge(
                id=f"{node_id}-edge-{index}",
                source=nodes[index].id,
                target=nodes[(index + 1) % len(nodes)].id,
                type="HAS_STEP_RUN",
                properties={"sequence": index},
            )
            for index in range(limit)
        ]
        return GraphReadDocument(nodes=nodes, edges=edges, meta={"source": "large-fake"})


class GraphReadPerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_settings_cache()
        self.context = create_test_api_context()
        self.context.graph_read_service = LargeGraphReadService()
        self.client = TestClient(create_app(context=self.context))
        self.headers = {
            "x-agency-user-id": "graph-performance-user",
            "x-agency-user-email": "graph-performance@example.com",
        }
        self.client.post(
            "/users/sync",
            json={"id": "graph-performance-user", "email": "graph-performance@example.com"},
        )

    def tearDown(self) -> None:
        reset_settings_cache()

    def test_progressive_expansion_payload_stays_within_response_budget(self) -> None:
        started_at = time.perf_counter()

        response = self.client.get(
            "/graph/read/nodes/run-performance/expand",
            headers=self.headers,
            params={"preset": "workflow_run", "depth": 2, "limit": 250},
        )

        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["meta"]["node_count"], 250)
        self.assertEqual(body["meta"]["edge_count"], 250)
        self.assertTrue(body["meta"]["truncated"])
        self.assertLess(elapsed_ms, 1500)
        self.assertEqual(
            self.context.graph_read_service.calls[-1][1],
            {
                "node_id": "run-performance",
                "labels": ["WorkflowRun"],
                "relationship_types": GRAPH_NEIGHBORHOOD_PRESETS["workflow_run"]["relationship_types"],
                "depth": 2,
                "limit": 250,
                "include_deleted": False,
            },
        )

    def test_progressive_expansion_rejects_over_budget_limit_before_traversal(self) -> None:
        response = self.client.get(
            "/graph/read/nodes/run-performance/expand",
            headers=self.headers,
            params={"preset": "workflow_run", "limit": 251},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.context.graph_read_service.calls, [])


if __name__ == "__main__":
    unittest.main()
