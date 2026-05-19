from __future__ import annotations

import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.routes import create_api_router
from app.domain import AgentDefinition, TaskDefinition, UserDefinition, WorkflowDefinition, \
    WorkflowNodeDefinition
from app.domain.models import MCPExposureSettings, SecuritySettings, ToolImplementationReference
from app.protocols.a2a.agent_card import agent_definition_to_card


class A2AIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        app = FastAPI()
        app.include_router(create_api_router(self.context))
        self.client = TestClient(app)
        self.client.headers.update(
            {
                "x-agency-user-id": "user-a2a",
                "x-agency-user-email": "a2a@example.com",
            }
        )

        self.agent = AgentDefinition(
            id="agent-a2a",
            name="A2A Agent",
            description="Exposed over A2A",
            instructions="Respond over A2A",
            tool_ids=["tool-skill"],
        )
        self.task = TaskDefinition(
            id="task-a2a",
            name="A2A Task",
            description="A2A workflow task",
            agent_id=self.agent.id,
        )
        self.workflow = WorkflowDefinition(
            id="workflow-a2a",
            name="A2A Workflow",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-a2a",
                    name="A2A Node",
                    node_type="task",
                    task_id=self.task.id,
                    agent_id=self.agent.id,
                )
            ],
            edges=[],
            entrypoint="node-a2a",
            task_definitions=[self.task],
            agent_definitions=[self.agent],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )

        import asyncio

        asyncio.run(
            self.context.user_repo.create(
                UserDefinition(id="user-a2a", email="a2a@example.com", display_name="A2A User")
            )
        )
        asyncio.run(self.context.agent_repo.create(self.agent))
        asyncio.run(self.context.workflow_repo.create(self.workflow))

    def test_agent_card_generation(self):
        card = agent_definition_to_card(self.agent, base_url="http://testserver", endpoint_path="/a2a/tasks")
        self.assertEqual(card["name"], "A2A Agent")
        self.assertIn("tool-use", card["capabilities"])
        response = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "A2A Agent")

    def test_task_creation_and_lookup(self):
        response = self.client.post(
            "/a2a/tasks",
            json={
                "workflowId": "workflow-a2a",
                "input": {"topic": "hello"},
                "trigger": {"created_by": "tester"},
                "message": {"role": "user", "content": "start"},
            },
        )
        self.assertEqual(response.status_code, 200)
        task_id = response.json()["id"]

        lookup = self.client.get(f"/a2a/tasks/{task_id}")
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.json()["metadata"]["trigger"]["created_by"], "tester")

    def test_message_exchange_and_artifact_creation(self):
        create = self.client.post("/a2a/tasks", json={"workflowId": "workflow-a2a", "input": {}})
        task_id = create.json()["id"]

        message = self.client.post(
            f"/a2a/tasks/{task_id}/messages",
            json={
                "role": "assistant",
                "content": "generated report",
                "metadata": {"channel": "a2a"},
                "artifact": {
                    "name": "report.json",
                    "type": "json",
                    "uri": "memory://report.json",
                    "metadata": {"source": "test"},
                },
            },
        )
        self.assertEqual(message.status_code, 200)
        self.assertEqual(message.json()["message"]["content"], "generated report")
        self.assertEqual(message.json()["artifact"]["name"], "report.json")

        artifacts = self.client.get(f"/a2a/tasks/{task_id}/artifacts")
        self.assertEqual(artifacts.status_code, 200)
        self.assertEqual(len(artifacts.json()["items"]), 1)

    def test_remote_agent_tool_execution_stub(self):
        payload = {
            "id": "tool-a2a-remote",
            "name": "Remote Agent Tool",
            "description": "Calls a remote A2A agent",
            "tool_type": "a2a_remote_agent",
            "input_schema": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
            "output_schema": {"type": "object"},
            "implementation": {
                "implementation_type": "a2a_remote_agent",
                "target": "http://remote.example/a2a",
                "config": {
                    "stub_response": {
                        "id": "remote-task-1",
                        "status": "completed",
                        "output": {"message": "stubbed"},
                    }
                },
            },
            "security": {
                "requires_approval": False,
                "sandbox_required": False,
                "allow_shell": False,
                "allow_browser": False,
                "allow_filesystem": False,
                "allow_network": True,
                "allowlisted_domains": ["remote.example"],
                "allowlisted_mcp_servers": [],
                "module_allowlist": [],
                "function_allowlist": [],
                "read_only_sql": True,
                "approval_on_rejection": "fail",
                "credential_references": [],
                "redaction_enabled": False,
                "redaction_rules": [],
            },
            "mcp_exposure": {
                "expose_as_mcp_tool": False,
                "expose_as_mcp_resource": False,
                "expose_as_mcp_prompt": False,
                "name_override": None,
                "tags": [],
            },
            "tags": ["a2a"],
            "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
        }
        create = self.client.post("/tools", json=payload)
        self.assertEqual(create.status_code, 200)

        test = self.client.post("/tools/tool-a2a-remote/test", json={"input": {"content": "hello remote"}})
        self.assertEqual(test.status_code, 200)
        self.assertEqual(test.json()["output"]["id"], "remote-task-1")


if __name__ == "__main__":
    unittest.main()
