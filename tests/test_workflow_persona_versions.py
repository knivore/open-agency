from __future__ import annotations

import unittest
from asyncio import run
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.domain import AgentDefinition, PersonaDefinition, PersonaVersion


class WorkflowPersonaVersionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.headers = {
            "x-agency-user-id": "persona-workflow-user",
            "x-agency-user-email": "persona-workflow@example.com",
        }
        self.client.post(
            "/users/sync",
            json={"id": "persona-workflow-user", "email": "persona-workflow@example.com"},
        )
        run(self._seed_persona_runtime())
        self._create_workflow_with_old_persona_snapshot()

    async def _seed_persona_runtime(self) -> None:
        await self.context.persona_version_repo.create(
            PersonaVersion(
                id="persona-version-v1",
                persona_id="persona-alpha",
                version="1.0.0",
                status="published",
                package={"persona": {"summary": "First version."}},
            )
        )
        await self.context.persona_version_repo.create(
            PersonaVersion(
                id="persona-version-v2",
                persona_id="persona-alpha",
                version="1.1.0",
                status="published",
                package={"persona": {"summary": "Second version."}},
            )
        )
        await self.context.persona_repo.create(
            PersonaDefinition(
                id="persona-alpha",
                slug="alpha",
                name="Alpha Persona",
                status="published",
                current_version_id="persona-version-v2",
                published_agent_id="persona-agent-alpha",
            )
        )
        await self.context.agent_repo.create(
            AgentDefinition(
                id="persona-agent-alpha",
                name="alpha",
                display_name="Alpha Persona",
                instructions="Use the latest Alpha instructions.",
                metadata={
                    "persona_id": "persona-alpha",
                    "persona_slug": "alpha",
                    "persona_version_id": "persona-version-v2",
                    "generated_from_persona_factory": True,
                },
            )
        )

    def _create_workflow_with_old_persona_snapshot(self) -> None:
        response = self.client.post(
            "/workflows",
            headers=self.headers,
            json={
                "id": "workflow-persona-version",
                "name": "Persona Version Workflow",
                "entrypoint": "node-1",
                "nodes": [
                    {
                        "id": "node-1",
                        "name": "Persona node",
                        "node_type": "agent",
                        "agent_id": "persona-agent-alpha",
                    }
                ],
                "agent_definitions": [
                    {
                        "id": "persona-agent-alpha",
                        "name": "alpha",
                        "display_name": "Alpha Persona",
                        "instructions": "Use the original Alpha instructions.",
                        "metadata": {
                            "persona_id": "persona-alpha",
                            "persona_slug": "alpha",
                            "persona_version_id": "persona-version-v1",
                            "generated_from_persona_factory": True,
                            "workflow_graph_position": {"x": 10, "y": 20},
                        },
                    }
                ],
                "task_definitions": [],
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_workflow_and_persona_usage_endpoints_report_outdated_snapshots(self) -> None:
        notices = self.client.get(
            "/workflows/workflow-persona-version/persona-version-notices",
            headers=self.headers,
        )
        self.assertEqual(notices.status_code, 200)
        self.assertEqual(notices.json()["count"], 1)
        notice = notices.json()["items"][0]
        self.assertEqual(notice["status"], "outdated")
        self.assertEqual(notice["persona_version"], "1.0.0")
        self.assertEqual(notice["current_persona_version"], "1.1.0")

        usages = self.client.get("/persona/persona-alpha/workflow-usages", headers=self.headers)
        self.assertEqual(usages.status_code, 200)
        self.assertEqual(usages.json()["count"], 1)
        self.assertEqual(usages.json()["outdated_count"], 1)
        self.assertEqual(usages.json()["items"][0]["workflow_id"], "workflow-persona-version")

    def test_can_keep_snapshot_until_a_newer_persona_version_exists(self) -> None:
        keep = self.client.post(
            "/workflows/workflow-persona-version/persona-agents/persona-agent-alpha/keep-current",
            headers=self.headers,
        )
        self.assertEqual(keep.status_code, 200)
        self.assertEqual(keep.json()["usage"]["status"], "pinned")
        self.assertEqual(keep.json()["persona_version_notices"], [])

        quiet_notice = self.client.get(
            "/workflows/workflow-persona-version/persona-version-notices",
            headers=self.headers,
        )
        self.assertEqual(quiet_notice.status_code, 200)
        self.assertEqual(quiet_notice.json()["items"], [])

        run(self._publish_new_persona_version())
        new_notice = self.client.get(
            "/workflows/workflow-persona-version/persona-version-notices",
            headers=self.headers,
        )
        self.assertEqual(new_notice.status_code, 200)
        self.assertEqual(new_notice.json()["items"][0]["status"], "outdated")
        self.assertEqual(new_notice.json()["items"][0]["current_persona_version"], "1.2.0")

    def test_can_replace_embedded_agent_with_latest_persona_agent(self) -> None:
        response = self.client.post(
            "/workflows/workflow-persona-version/persona-agents/persona-agent-alpha/use-latest",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        agent = payload["workflow"]["agent_definitions"][0]
        self.assertEqual(agent["instructions"], "Use the latest Alpha instructions.")
        self.assertEqual(agent["metadata"]["persona_version_id"], "persona-version-v2")
        self.assertEqual(agent["metadata"]["workflow_graph_position"], {"x": 10, "y": 20})
        self.assertEqual(payload["persona_version_notices"], [])

    async def _publish_new_persona_version(self) -> None:
        await self.context.persona_version_repo.create(
            PersonaVersion(
                id="persona-version-v3",
                persona_id="persona-alpha",
                version="1.2.0",
                status="published",
                package={"persona": {"summary": "Third version."}},
            )
        )
        await self.context.persona_repo.update("persona-alpha", {"current_version_id": "persona-version-v3"})


if __name__ == "__main__":
    unittest.main()
