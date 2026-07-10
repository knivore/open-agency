from __future__ import annotations

import os
import unittest
from asyncio import run
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache


class WorkflowMemoryLinkApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_settings_cache()
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.user_headers = {
            "x-agency-user-id": "user-1",
            "x-agency-user-email": "user1@example.com",
        }
        self.other_headers = {
            "x-agency-user-id": "user-2",
            "x-agency-user-email": "user2@example.com",
        }
        self.client.post("/users/sync", json={"id": "user-1", "email": "user1@example.com"})
        self.client.post("/users/sync", json={"id": "user-2", "email": "user2@example.com"})

    def tearDown(self) -> None:
        os.environ.pop("MEMORY_EMBEDDING_MODEL_PROFILE_ID", None)
        reset_settings_cache()

    def _create_workflow(self) -> None:
        response = self.client.post(
            "/workflows",
            headers=self.user_headers,
            json={
                "id": "workflow-memory-links",
                "name": "Memory Link Workflow",
                "entrypoint": "task-1",
                "agent_definitions": [{"id": "agent-1", "name": "Memory Agent"}],
                "task_definitions": [
                    {
                        "id": "task-1",
                        "name": "Use memory",
                        "description": "Use linked memory.",
                        "agent_id": "agent-1",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200)

    def _create_memory(self) -> None:
        response = self.client.post(
            "/memories",
            headers=self.user_headers,
            json={
                "memory": {
                    "id": "memory-link-1",
                    "scope": "user",
                    "content": "Workflow should reuse this preference.",
                    "summary": "Reusable workflow preference.",
                    "memory_type": "preference",
                }
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_workflow_memory_link_crud_for_single_memory(self) -> None:
        self._create_workflow()
        self._create_memory()

        create_response = self.client.post(
            "/workflows/workflow-memory-links/memory-links",
            headers=self.user_headers,
            json={
                "targetType": "task",
                "targetId": "task-1",
                "refType": "memory",
                "refId": "memory-link-1",
                "accessMode": "read",
                "label": "Task preference",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        payload = create_response.json()
        link = payload["link"]
        self.assertEqual(link["targetType"], "task")
        self.assertEqual(link["targetId"], "task-1")
        self.assertEqual(link["refType"], "memory")
        self.assertEqual(link["refId"], "memory-link-1")
        self.assertEqual(link["memoryIds"], ["memory-link-1"])
        self.assertEqual(link["label"], "Task preference")
        projection_events = run(self.context.graph_projection_event_repo.list_events())
        memory_link_events = [event for event in projection_events if event.event_type.startswith("workflow_memory_link.")]
        self.assertEqual([event.event_type for event in memory_link_events], ["workflow_memory_link.created"])
        self.assertEqual(memory_link_events[0].payload["link"]["id"], link["id"])

        list_response = self.client.get(
            "/workflows/workflow-memory-links/memory-links",
            headers=self.user_headers,
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual([item["id"] for item in list_response.json()["items"]], [link["id"]])

        workflow_response = self.client.get("/workflows/workflow-memory-links", headers=self.user_headers)
        self.assertEqual(workflow_response.status_code, 200)
        self.assertEqual(workflow_response.json()["metadata"]["memory_links"][0]["ref_id"], "memory-link-1")

        delete_response = self.client.delete(
            f"/workflows/workflow-memory-links/memory-links/{link['id']}",
            headers=self.user_headers,
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertTrue(delete_response.json()["deleted"])
        projection_events = run(self.context.graph_projection_event_repo.list_events())
        memory_link_events = [event for event in projection_events if event.event_type.startswith("workflow_memory_link.")]
        self.assertEqual(
            [event.event_type for event in memory_link_events],
            ["workflow_memory_link.created", "workflow_memory_link.deleted"],
        )

        empty_response = self.client.get(
            "/workflows/workflow-memory-links/memory-links",
            headers=self.user_headers,
        )
        self.assertEqual(empty_response.status_code, 200)
        self.assertEqual(empty_response.json()["items"], [])

    def test_workflow_memory_link_supports_document_collection_refs(self) -> None:
        self._create_workflow()
        for memory_id in ("doc-link-chunk-1", "doc-link-chunk-2"):
            response = self.client.post(
                "/memories",
                headers=self.user_headers,
                json={
                    "memory": {
                        "id": memory_id,
                        "scope": "user",
                        "source": "document_upload",
                        "content": f"Document chunk {memory_id}",
                        "summary": f"Document chunk {memory_id}",
                        "memory_type": "archive",
                        "metadata": {"document_id": "document-link-1", "filename": "linked-doc.md"},
                    }
                },
            )
            self.assertEqual(response.status_code, 200)

        create_response = self.client.post(
            "/workflows/workflow-memory-links/memory-links",
            headers=self.user_headers,
            json={
                "target_type": "workflow",
                "ref_type": "memory_collection",
                "ref_id": "document-link-1",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        link = create_response.json()["link"]
        self.assertEqual(link["targetType"], "workflow")
        self.assertIsNone(link["targetId"])
        self.assertEqual(link["refType"], "memory_collection")
        self.assertEqual(link["refId"], "document-link-1")
        self.assertEqual(set(link["memoryIds"]), {"doc-link-chunk-1", "doc-link-chunk-2"})
        self.assertEqual(link["label"], "linked-doc.md")
        projection_events = run(self.context.graph_projection_event_repo.list_events())
        memory_link_events = [event for event in projection_events if event.event_type == "workflow_memory_link.created"]
        self.assertEqual(len(memory_link_events), 1)
        projected_link = memory_link_events[0].payload["link"]
        self.assertEqual(projected_link["targetType"], "workflow")
        self.assertEqual(projected_link["refType"], "memory_collection")
        self.assertEqual(projected_link["refId"], "document-link-1")
        self.assertEqual(set(projected_link["memoryIds"]), {"doc-link-chunk-1", "doc-link-chunk-2"})
        self.assertNotIn("content", projected_link)
        self.assertNotIn("embedding", projected_link)

    def test_workflow_memory_link_rejects_non_owner_and_invalid_targets(self) -> None:
        self._create_workflow()
        self._create_memory()

        blocked = self.client.post(
            "/workflows/workflow-memory-links/memory-links",
            headers=self.other_headers,
            json={"targetType": "workflow", "refType": "memory", "refId": "memory-link-1"},
        )
        self.assertEqual(blocked.status_code, 403)

        invalid_target = self.client.post(
            "/workflows/workflow-memory-links/memory-links",
            headers=self.user_headers,
            json={
                "targetType": "task",
                "targetId": "missing-task",
                "refType": "memory",
                "refId": "memory-link-1",
            },
        )
        self.assertEqual(invalid_target.status_code, 404)
        self.assertIn("Task 'missing-task' not found", invalid_target.json()["detail"])


if __name__ == "__main__":
    unittest.main()
