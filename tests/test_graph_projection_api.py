from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.domain import GraphProjectionEvent


class GraphProjectionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_settings_cache()
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.headers = {
            "x-agency-user-id": "admin-graph",
            "x-agency-user-email": "admin-graph@example.com",
        }
        self.client.post(
            "/users/sync",
            json={"id": "admin-graph", "email": "admin-graph@example.com", "roles": ["admin"]},
        )

    def tearDown(self) -> None:
        reset_settings_cache()

    def test_status_and_replay_endpoints(self) -> None:
        self.client.post(
            "/memories",
            headers=self.headers,
            json={"memory": {"id": "graph-memory-1", "scope": "user", "content": "Status route memory."}},
        )
        status_response = self.client.get("/graph/projection/status", headers=self.headers)
        self.assertEqual(status_response.status_code, 200)
        status_payload = status_response.json()
        self.assertTrue(status_payload["enabled"])
        self.assertGreaterEqual(status_payload["pending_count"], 1)
        self.assertIn("oldest_pending_age_seconds", status_payload)

        replay_response = self.client.post("/graph/projection/replay", headers=self.headers, json={"run": True})
        self.assertEqual(replay_response.status_code, 200)
        self.assertGreaterEqual(replay_response.json()["processed"], 1)

    def test_status_reports_recent_failures(self) -> None:
        event = GraphProjectionEvent(
            event_type="memory.created",
            aggregate_type="memory",
            aggregate_id="memory-failed",
            payload={"memory_id": "memory-failed"},
        )
        self._run(self.context.graph_projection_event_repo.append(event))
        self._run(self.context.graph_projection_event_repo.mark_failed(event.event_id, "projection failed"))

        status_response = self.client.get("/graph/projection/status", headers=self.headers)
        self.assertEqual(status_response.status_code, 200)
        payload = status_response.json()
        self.assertEqual(payload["failed_count"], 1)
        self.assertEqual(payload["recent_failures"][0]["event_id"], event.event_id)

    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
