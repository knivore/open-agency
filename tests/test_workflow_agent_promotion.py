from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app


class WorkflowAgentPromotionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.owner_headers = {
            "x-agency-user-id": "workflow-agent-owner",
            "x-agency-user-email": "owner@example.com",
        }
        self.other_headers = {
            "x-agency-user-id": "workflow-agent-other",
            "x-agency-user-email": "other@example.com",
        }
        self.client.post(
            "/users/sync",
            json={"id": "workflow-agent-owner", "email": "owner@example.com"},
        )
        self.client.post(
            "/users/sync",
            json={"id": "workflow-agent-other", "email": "other@example.com"},
        )
        self._create_workflow()

    def _create_workflow(self) -> None:
        response = self.client.post(
            "/workflows",
            headers=self.owner_headers,
            json={
                "id": "workflow-agent-promotion",
                "name": "Workflow Agent Promotion",
                "entrypoint": "node-1",
                "nodes": [
                    {
                        "id": "node-1",
                        "name": "Initial Node",
                        "node_type": "task",
                        "agent_id": "workflow-agent-1",
                        "task_id": "task-1",
                    }
                ],
                "agent_definitions": [
                    {
                        "id": "workflow-agent-1",
                        "name": "Workflow Agent",
                        "instructions": "Use the local workflow behavior.",
                        "metadata": {"workflow_graph_position": {"x": 10, "y": 20}},
                    }
                ],
                "task_definitions": [
                    {
                        "id": "task-1",
                        "name": "Local Task",
                        "description": "Execute the workflow task.",
                        "agent_id": "workflow-agent-1",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_promote_workflow_agent_creates_global_copy_with_provenance(self) -> None:
        response = self.client.post(
            "/workflows/workflow-agent-promotion/agents/workflow-agent-1/promote",
            headers=self.owner_headers,
            json={},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["workflow_updated"])
        self.assertEqual(payload["promotion"]["global_agent_id"], "workflow-agent-1")
        self.assertEqual(payload["agent"]["metadata"]["promoted_from_workflow_id"], "workflow-agent-promotion")
        self.assertEqual(payload["agent"]["metadata"]["promoted_from_workflow_agent_id"], "workflow-agent-1")
        self.assertEqual(payload["agent"]["metadata"]["promoted_to_global_by"], "workflow-agent-owner")

        agent_response = self.client.get("/agents/workflow-agent-1", headers=self.owner_headers)
        self.assertEqual(agent_response.status_code, 200)
        self.assertEqual(agent_response.json()["instructions"], "Use the local workflow behavior.")

        workflow_response = self.client.get("/workflows/workflow-agent-promotion", headers=self.owner_headers)
        self.assertEqual(workflow_response.status_code, 200)
        self.assertEqual(workflow_response.json()["agent_definitions"][0]["id"], "workflow-agent-1")

    def test_promote_workflow_agent_can_rebind_workflow_to_new_global_agent_id(self) -> None:
        response = self.client.post(
            "/workflows/workflow-agent-promotion/agents/workflow-agent-1/promote",
            headers=self.owner_headers,
            json={
                "globalAgentId": "catalog-agent-1",
                "replaceWorkflowAgent": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["workflow_updated"])
        self.assertEqual(payload["agent"]["id"], "catalog-agent-1")
        self.assertEqual(payload["workflow"]["agent_definitions"][0]["id"], "catalog-agent-1")
        self.assertEqual(payload["workflow"]["nodes"][0]["agent_id"], "catalog-agent-1")
        self.assertEqual(payload["workflow"]["task_definitions"][0]["agent_id"], "catalog-agent-1")

    def test_promote_workflow_agent_rejects_catalog_id_conflicts(self) -> None:
        create_agent = self.client.post(
            "/agents",
            headers=self.owner_headers,
            json={
                "id": "catalog-agent-1",
                "name": "Existing Catalog Agent",
                "instructions": "Already in the global catalog.",
            },
        )
        self.assertEqual(create_agent.status_code, 200)

        response = self.client.post(
            "/workflows/workflow-agent-promotion/agents/workflow-agent-1/promote",
            headers=self.owner_headers,
            json={"globalAgentId": "catalog-agent-1"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("already exists", response.json()["detail"])

    def test_promote_workflow_agent_requires_workflow_owner_access(self) -> None:
        response = self.client.post(
            "/workflows/workflow-agent-promotion/agents/workflow-agent-1/promote",
            headers=self.other_headers,
            json={},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
