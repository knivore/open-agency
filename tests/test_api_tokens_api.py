from __future__ import annotations

import unittest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from app.api.main import create_app


class ApiTokensApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.owner_headers = {
            "x-agency-user-id": "user-1",
            "x-agency-user-email": "owner@example.com",
        }
        self.other_owner_headers = {
            "x-agency-user-id": "user-2",
            "x-agency-user-email": "other@example.com",
        }
        self.client.post(
            "/users/sync",
            json={"id": "user-1", "email": "owner@example.com", "display_name": "Owner One"},
        )
        self.client.post(
            "/users/sync",
            json={"id": "user-2", "email": "other@example.com", "display_name": "Owner Two"},
        )

    def _create_bearer_token(self, *, scopes: list[str], name: str = "Scoped token") -> str:
        response = self.client.post(
            "/api-tokens",
            headers=self.owner_headers,
            json={"name": name, "scopes": scopes},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["token"]

    def _workflow_payload(self, workflow_id: str = "workflow-scope-test") -> dict:
        return {
            "id": workflow_id,
            "name": "Scoped Workflow",
            "description": "Workflow for scope tests",
            "entrypoint": "task-1",
            "nodes": [
                {
                    "id": "node-task-1",
                    "name": "Task 1",
                    "node_type": "task",
                    "task_id": "task-1",
                }
            ],
            "edges": [],
            "task_definitions": [
                {
                    "id": "task-1",
                    "name": "Task 1",
                    "description": "Do the thing",
                    "expected_output": "Done",
                }
            ],
            "agent_definitions": [],
            "tool_definitions": [],
            "versioning": {
                "version": "1.0.0",
                "revision": 1,
                "parent_version": None,
                "is_published": False,
                "labels": [],
            },
            "metadata": {
                "created_by": "user-1",
                "owner_ids": ["user-1"],
                "marketplace_status": "draft",
            },
        }

    def test_api_token_actions_are_audited(self) -> None:
        create_response = self.client.post(
            "/api-tokens",
            headers=self.owner_headers,
            json={"name": "Audited token", "scopes": ["workflows:read"]},
        )
        self.assertEqual(create_response.status_code, 200)
        created = create_response.json()

        snapshot = self.context.runtime_operations.snapshot_dict()
        created_actions = [item for item in snapshot["recent_actions"] if item["action"] == "api_token.created"]
        self.assertTrue(created_actions)
        self.assertEqual(created_actions[-1]["token_id"], created["id"])
        self.assertEqual(created_actions[-1]["owner_user_id"], "user-1")
        self.assertEqual(created_actions[-1]["scopes"], ["workflows:read"])

        raw_token = created["token"]
        me_response = self.client.get("/me", headers={"authorization": f"Bearer {raw_token}"})
        self.assertEqual(me_response.status_code, 200)

        snapshot = self.context.runtime_operations.snapshot_dict()
        used_actions = [item for item in snapshot["recent_actions"] if item["action"] == "api_token.used"]
        self.assertTrue(used_actions)
        self.assertEqual(used_actions[-1]["token_id"], created["id"])
        self.assertEqual(used_actions[-1]["path"], "/me")
        self.assertEqual(used_actions[-1]["method"], "GET")

        revoke_response = self.client.post(f"/api-tokens/{created['id']}/revoke", headers=self.owner_headers)
        self.assertEqual(revoke_response.status_code, 200)

        snapshot = self.context.runtime_operations.snapshot_dict()
        revoked_actions = [item for item in snapshot["recent_actions"] if item["action"] == "api_token.revoked"]
        self.assertTrue(revoked_actions)
        self.assertEqual(revoked_actions[-1]["token_id"], created["id"])
        self.assertEqual(revoked_actions[-1]["owner_user_id"], "user-1")

    def test_api_token_create_list_and_revoke_round_trip(self) -> None:
        create_response = self.client.post(
            "/api-tokens",
            headers=self.owner_headers,
            json={"name": "Local automation", "scopes": ["workflows:run"]},
        )
        self.assertEqual(create_response.status_code, 200)
        created = create_response.json()
        self.assertEqual(created["owner_user_id"], "user-1")
        self.assertEqual(created["name"], "Local automation")
        self.assertEqual(created["scopes"], ["workflows:run"])
        self.assertIn("token", created)
        self.assertNotIn("token_hash", created)

        list_response = self.client.get("/api-tokens", headers=self.owner_headers)
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()["items"]), 1)
        self.assertNotIn("token", list_response.json()["items"][0])
        self.assertNotIn("token_hash", list_response.json()["items"][0])

        other_owner_list_response = self.client.get("/api-tokens", headers=self.other_owner_headers)
        self.assertEqual(other_owner_list_response.status_code, 200)
        self.assertEqual(other_owner_list_response.json()["items"], [])

        cross_owner_revoke_response = self.client.post(
            f"/api-tokens/{created['id']}/revoke",
            headers=self.other_owner_headers,
        )
        self.assertEqual(cross_owner_revoke_response.status_code, 404)

        revoke_response = self.client.post(f"/api-tokens/{created['id']}/revoke", headers=self.owner_headers)
        self.assertEqual(revoke_response.status_code, 200)
        self.assertIsNotNone(revoke_response.json()["revoked_at"])
        self.assertNotIn("token_hash", revoke_response.json())

    def test_api_token_scope_catalog_is_listed(self) -> None:
        response = self.client.get("/api-tokens/scopes", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        scope_ids = {item["id"] for item in payload["items"]}
        self.assertIn("agents:write", scope_ids)
        self.assertIn("conversations:read", scope_ids)
        self.assertIn("conversations:write", scope_ids)
        self.assertIn("workflows:run", scope_ids)
        self.assertNotIn("profile:read", scope_ids)

    def test_api_token_authenticates_current_user(self) -> None:
        create_response = self.client.post(
            "/api-tokens",
            headers=self.owner_headers,
            json={"name": "External client", "scopes": ["workflows:read"]},
        )
        raw_token = create_response.json()["token"]

        me_response = self.client.get("/me", headers={"authorization": f"Bearer {raw_token}"})
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.json()["id"], "user-1")

        list_response = self.client.get("/api-tokens", headers=self.owner_headers)
        self.assertEqual(list_response.status_code, 200)
        self.assertIsNotNone(list_response.json()["items"][0]["last_used_at"])

    def test_api_token_usage_write_is_throttled(self) -> None:
        raw_token = self._create_bearer_token(scopes=["workflows:read"])
        headers = {"authorization": f"Bearer {raw_token}"}
        self.assertEqual(self.client.get("/me", headers=headers).status_code, 200)

        with patch.object(self.context.api_token_repo, "update", new_callable=AsyncMock) as update:
            self.assertEqual(self.client.get("/me", headers=headers).status_code, 200)

        update.assert_not_awaited()

    def test_api_token_usage_write_failure_does_not_reject_valid_token(self) -> None:
        raw_token = self._create_bearer_token(scopes=["workflows:read"])
        headers = {"authorization": f"Bearer {raw_token}"}

        with (
            patch.object(
                self.context.api_token_repo,
                "update",
                AsyncMock(side_effect=RuntimeError("database lock table exhausted")),
            ),
            patch("app.api.identity.logger.warning") as warning,
        ):
            response = self.client.get("/me", headers=headers)

        self.assertEqual(response.status_code, 200)
        warning.assert_called_once()

    def test_api_tokens_reject_unknown_scopes(self) -> None:
        response = self.client.post(
            "/api-tokens",
            headers=self.owner_headers,
            json={"name": "Unknown scope token", "scopes": ["profile:read"]},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("invalidScopes", response.json()["detail"])

    def test_api_token_cannot_mint_broader_scopes(self) -> None:
        raw_token = self._create_bearer_token(scopes=["workflows:read"], name="Delegator")
        bearer_headers = {"authorization": f"Bearer {raw_token}"}

        allowed = self.client.post(
            "/api-tokens",
            headers=bearer_headers,
            json={"name": "Read-only child", "scopes": ["workflows:read"]},
        )
        self.assertEqual(allowed.status_code, 200)

        escalated = self.client.post(
            "/api-tokens",
            headers=bearer_headers,
            json={"name": "Escalated child", "scopes": ["workflows:write"]},
        )
        self.assertEqual(escalated.status_code, 403)
        self.assertIn("only delegate", escalated.json()["detail"])

    def test_api_tokens_require_current_user(self) -> None:
        response = self.client.get("/api-tokens")
        self.assertEqual(response.status_code, 401)

    def test_revoked_api_token_cannot_authenticate(self) -> None:
        raw_token = self._create_bearer_token(scopes=["workflows:read"], name="Revoked token")

        list_response = self.client.get("/api-tokens", headers=self.owner_headers)
        self.assertEqual(list_response.status_code, 200)
        token_id = list_response.json()["items"][0]["id"]

        revoke_response = self.client.post(f"/api-tokens/{token_id}/revoke", headers=self.owner_headers)
        self.assertEqual(revoke_response.status_code, 200)

        me_response = self.client.get("/me", headers={"authorization": f"Bearer {raw_token}"})
        self.assertEqual(me_response.status_code, 401)
        self.assertEqual(me_response.json()["detail"], "Invalid or revoked API token")

    def test_disabled_user_api_token_is_forbidden(self) -> None:
        disabled_sync = self.client.post(
            "/users/sync",
            json={
                "id": "user-disabled",
                "email": "disabled@example.com",
                "display_name": "Disabled User",
                "status": "disabled",
            },
        )
        self.assertEqual(disabled_sync.status_code, 200)

        disabled_headers = {
            "x-agency-user-id": "user-disabled",
            "x-agency-user-email": "disabled@example.com",
        }
        create_response = self.client.post(
            "/api-tokens",
            headers=disabled_headers,
            json={"name": "Disabled token", "scopes": ["workflows:read"]},
        )
        self.assertEqual(create_response.status_code, 403)
        self.assertEqual(create_response.json()["detail"], "Current user is disabled")

    def test_api_token_activity_endpoint_is_owner_scoped(self) -> None:
        owner_token = self._create_bearer_token(scopes=["workflows:read"], name="Owner activity token")
        self.client.get("/me", headers={"authorization": f"Bearer {owner_token}"})

        other_create = self.client.post(
            "/api-tokens",
            headers=self.other_owner_headers,
            json={"name": "Other owner token", "scopes": ["workflows:read"]},
        )
        self.assertEqual(other_create.status_code, 200)

        response = self.client.get(
            "/observability/api-tokens/activity",
            headers=self.owner_headers,
            params={"limit": 10},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(payload["total"], 2)
        self.assertTrue(payload["items"])
        self.assertTrue(all(item["owner_user_id"] == "user-1" for item in payload["items"]))
        self.assertTrue(any(item["action"] == "api_token.created" for item in payload["items"]))
        self.assertTrue(any(item["action"] == "api_token.used" for item in payload["items"]))

    def test_workflows_run_scope_can_start_execution_but_cannot_mutate_workflow(self) -> None:
        create_workflow = self.client.post(
            "/workflows",
            headers=self.owner_headers,
            json=self._workflow_payload(),
        )
        self.assertEqual(create_workflow.status_code, 200)

        raw_token = self._create_bearer_token(scopes=["workflows:run"], name="Workflow runner")
        bearer_headers = {"authorization": f"Bearer {raw_token}"}

        start_execution = self.client.post(
            "/workflows/workflow-scope-test/executions",
            headers=bearer_headers,
            json={"input": {}, "trigger": {}},
        )
        self.assertEqual(start_execution.status_code, 200)

        update_workflow = self.client.put(
            "/workflows/workflow-scope-test",
            headers=bearer_headers,
            json={"description": "Should fail"},
        )
        self.assertEqual(update_workflow.status_code, 403)
        self.assertIn("missingScopes", update_workflow.json()["detail"])

    def test_integrations_read_scope_cannot_create_credentials(self) -> None:
        raw_token = self._create_bearer_token(scopes=["integrations:read"], name="Integrations reader")
        bearer_headers = {"authorization": f"Bearer {raw_token}"}

        list_response = self.client.get("/integrations/categories", headers=bearer_headers)
        self.assertEqual(list_response.status_code, 200)

        create_response = self.client.post(
            "/credentials",
            headers=bearer_headers,
            json={
                "id": "credential-read-only",
                "name": "Read Only Credential",
                "provider": "telegram",
                "secret_ref": "env://TELEGRAM_BOT_TOKEN",
                "metadata": {"channel": "ops"},
            },
        )
        self.assertEqual(create_response.status_code, 403)
        self.assertIn("integrations:write", create_response.json()["detail"]["missingScopes"])

    def test_integrations_write_scope_can_create_credentials_but_not_list_without_read(self) -> None:
        raw_token = self._create_bearer_token(scopes=["integrations:write"], name="Integrations writer")
        bearer_headers = {"authorization": f"Bearer {raw_token}"}

        create_response = self.client.post(
            "/credentials",
            headers=bearer_headers,
            json={
                "id": "credential-write-only",
                "name": "Write Only Credential",
                "provider": "telegram",
                "secret_ref": "env://TELEGRAM_BOT_TOKEN",
                "metadata": {"channel": "ops"},
            },
        )
        self.assertEqual(create_response.status_code, 200)

        list_response = self.client.get("/credentials", headers=bearer_headers)
        self.assertEqual(list_response.status_code, 403)
        self.assertIn("integrations:read", list_response.json()["detail"]["missingScopes"])

    def test_executions_read_scope_cannot_pause_execution(self) -> None:
        create_workflow = self.client.post(
            "/workflows",
            headers=self.owner_headers,
            json=self._workflow_payload(workflow_id="workflow-execution-scope-test"),
        )
        self.assertEqual(create_workflow.status_code, 200)

        writer_token = self._create_bearer_token(scopes=["workflows:run"], name="Execution creator")
        create_execution = self.client.post(
            "/workflows/workflow-execution-scope-test/executions",
            headers={"authorization": f"Bearer {writer_token}"},
            json={"input": {}, "trigger": {}},
        )
        self.assertEqual(create_execution.status_code, 200)
        execution_id = create_execution.json()["id"]

        reader_token = self._create_bearer_token(scopes=["executions:read"], name="Execution reader")
        pause_response = self.client.post(
            f"/executions/{execution_id}/pause",
            headers={"authorization": f"Bearer {reader_token}"},
        )
        self.assertEqual(pause_response.status_code, 403)
        self.assertIn("executions:write", pause_response.json()["detail"]["missingScopes"])


if __name__ == "__main__":
    unittest.main()
