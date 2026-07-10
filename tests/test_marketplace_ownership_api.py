from __future__ import annotations

import asyncio
import json
import unittest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.domain import WorkflowDefinition


def workflow_payload(workflow_id: str, owner_id: str, marketplace_status: str = "draft") -> dict:
    return {
        "id": workflow_id,
        "name": f"Workflow {workflow_id}",
        "description": "A workflow for ownership tests",
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
            "is_published": marketplace_status == "approved",
            "labels": [],
        },
        "metadata": {
            "created_by": owner_id,
            "owner_ids": [owner_id],
            "marketplace_status": marketplace_status,
        },
    }


def importable_workflow_payload(workflow_id: str = "marketplace-source-workflow") -> dict:
    payload = workflow_payload(workflow_id, "marketplace-author", marketplace_status="approved")
    payload["entrypoint"] = "node-task-1"
    payload["metadata"]["source_catalog"] = "test-marketplace"
    return payload


def high_risk_workflow_payload(workflow_id: str = "marketplace-high-risk-workflow") -> dict:
    payload = importable_workflow_payload(workflow_id)
    payload["tool_definitions"] = [
        {
            "id": "tool-shell",
            "name": "Shell Tool",
            "description": "Runs a shell command",
            "tool_type": "shell_command",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            "output_schema": {"type": "object"},
            "implementation": {
                "implementation_type": "shell_command",
                "target": "echo ok",
                "config": {},
            },
            "security": {
                "requires_approval": True,
                "sandbox_required": True,
                "allow_shell": True,
                "allow_browser": False,
                "allow_filesystem": False,
                "allow_network": False,
                "allowlisted_domains": [],
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
            "tags": [],
            "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
        }
    ]
    payload["task_definitions"][0]["tool_ids"] = ["tool-shell"]
    return payload


class FakeMarketplaceResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload, sort_keys=True).encode("utf-8")

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class FakeMarketplaceClient:
    def __init__(self, response: FakeMarketplaceResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, dict | None]] = []

    async def __aenter__(self) -> "FakeMarketplaceClient":
        return self

    async def __aexit__(self, _exc_type, exc, _tb) -> None:
        return None

    async def get(self, url: str, headers: dict | None = None) -> FakeMarketplaceResponse:
        self.requests.append((url, headers))
        return self.response


class MarketplaceOwnershipApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.owner_headers = {
            "x-agency-user-id": "user-owner",
            "x-agency-user-email": "owner@example.com",
        }
        self.other_headers = {
            "x-agency-user-id": "user-other",
            "x-agency-user-email": "other@example.com",
        }
        self.admin_headers = {
            "x-agency-user-id": "user-admin",
            "x-agency-user-email": "admin@example.com",
        }
        self.client.post(
            "/users/sync",
            json={"id": "user-owner", "email": "owner@example.com", "display_name": "Owner"},
        )
        self.client.post(
            "/users/sync",
            json={"id": "user-other", "email": "other@example.com", "display_name": "Other"},
        )
        self.client.post(
            "/users/sync",
            json={
                "id": "user-admin",
                "email": "admin@example.com",
                "display_name": "Admin",
                "roles": ["admin"],
            },
        )

    def test_marketplace_submit_review_and_clone_are_owner_aware(self) -> None:
        create_response = self.client.post(
            "/workflows",
            headers=self.owner_headers,
            json=workflow_payload("workflow-owned", "user-owner"),
        )
        self.assertEqual(create_response.status_code, 200)

        cross_owner_submit = self.client.post(
            "/marketplace/workflows/workflow-owned/submit",
            headers=self.other_headers,
        )
        self.assertEqual(cross_owner_submit.status_code, 403)

        submit_response = self.client.post(
            "/marketplace/workflows/workflow-owned/submit",
            headers=self.owner_headers,
        )
        self.assertEqual(submit_response.status_code, 200)
        self.assertEqual(submit_response.json()["metadata"]["marketplace_status"], "pending")
        self.assertEqual(submit_response.json()["metadata"]["marketplace_submitted_by"], "user-owner")

        non_admin_approve = self.client.post(
            "/marketplace/workflows/workflow-owned/approve",
            headers=self.owner_headers,
        )
        self.assertEqual(non_admin_approve.status_code, 403)

        approve_response = self.client.post(
            "/marketplace/workflows/workflow-owned/approve",
            headers=self.admin_headers,
        )
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(approve_response.json()["metadata"]["marketplace_status"], "approved")

        clone_response = self.client.post(
            "/marketplace/workflows/workflow-owned/clone",
            headers=self.other_headers,
        )
        self.assertEqual(clone_response.status_code, 200)
        clone = clone_response.json()
        self.assertNotEqual(clone["id"], "workflow-owned")
        self.assertFalse(clone["versioning"]["is_published"])
        self.assertEqual(clone["metadata"]["owner_ids"], ["user-other"])
        self.assertEqual(clone["metadata"]["created_by"], "user-other")
        self.assertEqual(clone["metadata"]["cloned_by"], "user-other")
        self.assertEqual(clone["metadata"]["cloned_from_workflow_id"], "workflow-owned")
        self.assertEqual(clone["metadata"]["marketplace_status"], "draft")

    def test_workflow_owner_mutation_routes_enforce_owner_access(self) -> None:
        create_response = self.client.post(
            "/workflows",
            headers=self.owner_headers,
            json=workflow_payload("workflow-sharing", "user-owner"),
        )
        self.assertEqual(create_response.status_code, 200)

        cross_owner_add = self.client.post(
            "/workflows/workflow-sharing/owners",
            headers=self.other_headers,
            json={"owner_ids": ["user-other"]},
        )
        self.assertEqual(cross_owner_add.status_code, 403)

        add_response = self.client.post(
            "/workflows/workflow-sharing/owners",
            headers=self.owner_headers,
            json={"owner_ids": ["user-other"]},
        )
        self.assertEqual(add_response.status_code, 200)
        self.assertEqual(add_response.json()["owner_ids"], ["user-owner", "user-other"])

        list_response = self.client.get(
            "/workflows/workflow-sharing/owners",
            headers=self.other_headers,
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(
            {item["id"] for item in list_response.json()["items"]},
            {"user-owner", "user-other"},
        )

        remove_response = self.client.request(
            "DELETE",
            "/workflows/workflow-sharing/owners",
            headers=self.other_headers,
            json={"owner_id": "user-owner"},
        )
        self.assertEqual(remove_response.status_code, 200)
        self.assertEqual(remove_response.json()["owner_ids"], ["user-other"])

    def test_workflow_update_delete_publish_and_clone_require_owner_access(self) -> None:
        create_response = self.client.post(
            "/workflows",
            headers=self.owner_headers,
            json=workflow_payload("workflow-ops", "user-owner"),
        )
        self.assertEqual(create_response.status_code, 200)

        cross_owner_update = self.client.put(
            "/workflows/workflow-ops",
            headers=self.other_headers,
            json={"description": "Cross-owner update"},
        )
        self.assertEqual(cross_owner_update.status_code, 403)

        update_response = self.client.put(
            "/workflows/workflow-ops",
            headers=self.owner_headers,
            json={"description": "Owner update"},
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["description"], "Owner update")

        cross_owner_publish = self.client.post(
            "/workflows/workflow-ops/publish",
            headers=self.other_headers,
            json={"version": "1.1.0"},
        )
        self.assertEqual(cross_owner_publish.status_code, 403)

        publish_response = self.client.post(
            "/workflows/workflow-ops/publish",
            headers=self.owner_headers,
            json={"version": "1.1.0"},
        )
        self.assertEqual(publish_response.status_code, 200)
        self.assertTrue(publish_response.json()["versioning"]["is_published"])
        self.assertEqual(publish_response.json()["versioning"]["version"], "1.1.0")

        cross_owner_unpublish = self.client.post(
            "/workflows/workflow-ops/unpublish",
            headers=self.other_headers,
        )
        self.assertEqual(cross_owner_unpublish.status_code, 403)

        unpublish_response = self.client.post(
            "/workflows/workflow-ops/unpublish",
            headers=self.owner_headers,
        )
        self.assertEqual(unpublish_response.status_code, 200)
        self.assertFalse(unpublish_response.json()["versioning"]["is_published"])
        self.assertEqual(unpublish_response.json()["versioning"]["version"], "1.1.0")

        cross_owner_clone = self.client.post(
            "/workflows/workflow-ops/clone",
            headers=self.other_headers,
        )
        self.assertEqual(cross_owner_clone.status_code, 403)

        clone_response = self.client.post(
            "/workflows/workflow-ops/clone",
            headers=self.owner_headers,
        )
        self.assertEqual(clone_response.status_code, 200)
        clone = clone_response.json()
        self.assertNotEqual(clone["id"], "workflow-ops")
        self.assertEqual(clone["metadata"]["owner_ids"], ["user-owner"])
        self.assertEqual(clone["metadata"]["cloned_from_workflow_id"], "workflow-ops")
        self.assertFalse(clone["versioning"]["is_published"])

        cross_owner_delete = self.client.delete(
            "/workflows/workflow-ops",
            headers=self.other_headers,
        )
        self.assertEqual(cross_owner_delete.status_code, 403)

        delete_response = self.client.delete(
            "/workflows/workflow-ops",
            headers=self.owner_headers,
        )
        self.assertEqual(delete_response.status_code, 200)

    def test_workflow_create_requires_authenticated_owner(self) -> None:
        create_response = self.client.post(
            "/workflows",
            json=workflow_payload("workflow-no-auth", "user-owner"),
        )
        self.assertEqual(create_response.status_code, 401)

    def test_marketplace_import_installs_untrusted_workflow_with_owner_and_provenance(self) -> None:
        import_response = self.client.post(
            "/marketplace/workflows/import",
            headers=self.owner_headers,
            json={
                "workflow": importable_workflow_payload(),
                "source_url": "https://marketplace.example/workflows/source.json",
                "source_id": "catalog-source",
                "source_version": "2.1.0",
            },
        )
        self.assertEqual(import_response.status_code, 200)
        body = import_response.json()
        workflow = body["workflow"]
        self.assertNotEqual(workflow["id"], "marketplace-source-workflow")
        self.assertEqual(workflow["metadata"]["created_by"], "user-owner")
        self.assertEqual(workflow["metadata"]["owner_ids"], ["user-owner"])
        self.assertEqual(workflow["metadata"]["marketplace_status"], "draft")
        self.assertTrue(workflow["metadata"]["marketplace_untrusted_until_reviewed"])
        self.assertEqual(workflow["metadata"]["marketplace_source_workflow_id"], "marketplace-source-workflow")
        self.assertEqual(workflow["metadata"]["marketplace_source_url"], "https://marketplace.example/workflows/source.json")
        self.assertEqual(workflow["metadata"]["marketplace_source_id"], "catalog-source")
        self.assertEqual(workflow["metadata"]["marketplace_source_version"], "2.1.0")
        self.assertFalse(workflow["versioning"]["is_published"])

        fetch_response = self.client.get(f"/workflows/{workflow['id']}", headers=self.owner_headers)
        self.assertEqual(fetch_response.status_code, 200)

    def test_marketplace_import_rejects_invalid_workflow_before_installing(self) -> None:
        invalid = importable_workflow_payload("invalid-marketplace-workflow")
        invalid["entrypoint"] = "missing-node"

        import_response = self.client.post(
            "/marketplace/workflows/import",
            headers=self.owner_headers,
            json={"workflow": invalid, "source_id": "invalid-source"},
        )

        self.assertEqual(import_response.status_code, 400)
        self.assertEqual(import_response.json()["detail"]["message"], "Marketplace workflow failed validation")
        list_response = self.client.get("/workflows", headers=self.owner_headers)
        self.assertEqual(list_response.status_code, 200)
        self.assertNotIn(
            "invalid-marketplace-workflow",
            {item["metadata"].get("marketplace_source_workflow_id") for item in list_response.json()["items"]},
        )

    def test_marketplace_import_can_pull_remote_workflow_document(self) -> None:
        remote_payload = {
            "source": {"id": "remote-catalog-item", "version": "3.0.0"},
            "workflow": importable_workflow_payload("remote-marketplace-workflow"),
        }
        fake_response = FakeMarketplaceResponse(remote_payload)
        fake_client = FakeMarketplaceClient(fake_response)

        with patch(
                "app.api.routes.marketplace.httpx.AsyncClient",
                return_value=fake_client,
        ):
            import_response = self.client.post(
                "/marketplace/workflows/import",
                headers=self.owner_headers,
                json={"source_url": "https://marketplace.example/workflows/remote.json"},
            )

        self.assertEqual(import_response.status_code, 200)
        workflow = import_response.json()["workflow"]
        self.assertEqual(workflow["metadata"]["marketplace_source_workflow_id"], "remote-marketplace-workflow")
        self.assertEqual(workflow["metadata"]["marketplace_source_id"], "remote-catalog-item")
        self.assertEqual(workflow["metadata"]["marketplace_source_version"], "3.0.0")
        self.assertEqual(
            workflow["metadata"]["marketplace_source_sha256"],
            __import__("hashlib").sha256(fake_response.content).hexdigest(),
        )
        self.assertEqual(fake_client.requests[0][0], "https://marketplace.example/workflows/remote.json")

    def test_marketplace_preview_shows_risk_labels_before_import(self) -> None:
        preview_response = self.client.post(
            "/marketplace/workflows/preview",
            headers=self.owner_headers,
            json={"workflow": high_risk_workflow_payload()},
        )

        self.assertEqual(preview_response.status_code, 200)
        body = preview_response.json()
        self.assertEqual(body["validation_errors"], [])
        self.assertTrue(body["high_risk"])
        self.assertTrue(body["requires_import_approval"])
        self.assertIn("shell", body["risk_labels"])
        self.assertIn("local_privileged_execution", body["high_risk_labels"])

    def test_marketplace_import_requires_explicit_approval_for_high_risk_workflow(self) -> None:
        blocked_response = self.client.post(
            "/marketplace/workflows/import",
            headers=self.owner_headers,
            json={"workflow": high_risk_workflow_payload()},
        )
        self.assertEqual(blocked_response.status_code, 409)
        self.assertEqual(
            blocked_response.json()["detail"]["message"],
            "Marketplace workflow requires explicit high-risk import approval",
        )
        self.assertIn("shell", blocked_response.json()["detail"]["risk_labels"])

        approved_response = self.client.post(
            "/marketplace/workflows/import",
            headers=self.owner_headers,
            json={"workflow": high_risk_workflow_payload(), "approve_high_risk": True},
        )
        self.assertEqual(approved_response.status_code, 200)
        body = approved_response.json()
        self.assertTrue(body["high_risk"])
        self.assertIn("shell", body["risk_labels"])
        self.assertTrue(body["workflow"]["metadata"]["marketplace_untrusted_until_reviewed"])

    def test_ownerless_workflow_is_claimed_by_first_authenticated_mutation(self) -> None:
        payload = workflow_payload("workflow-ownerless", "user-owner")
        payload["metadata"] = {}
        create_response = self.client.post(
            "/workflows",
            headers=self.owner_headers,
            json=payload,
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(create_response.json()["metadata"]["created_by"], "user-owner")
        self.assertEqual(create_response.json()["metadata"]["owner_ids"], ["user-owner"])

        delete_response = self.client.delete(
            "/workflows/workflow-ownerless",
            headers=self.owner_headers,
        )
        self.assertEqual(delete_response.status_code, 200)

    def test_preexisting_ownerless_workflow_can_be_claimed_for_protected_mutation(self) -> None:
        payload = workflow_payload("workflow-legacy-ownerless", "user-owner")
        payload["metadata"] = {}
        asyncio.run(self.context.workflow_repo.create(WorkflowDefinition.model_validate(payload)))

        update_response = self.client.put(
            "/workflows/workflow-legacy-ownerless",
            headers=self.owner_headers,
            json={"description": "Claimed by owner"},
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["metadata"]["created_by"], "user-owner")
        self.assertEqual(update_response.json()["metadata"]["owner_ids"], ["user-owner"])

        cross_owner_delete = self.client.delete(
            "/workflows/workflow-legacy-ownerless",
            headers=self.other_headers,
        )
        self.assertEqual(cross_owner_delete.status_code, 403)

    def test_workflow_execution_creation_requires_owner_access_for_native_and_crewai(self) -> None:
        create_response = self.client.post(
            "/workflows",
            headers=self.owner_headers,
            json={
                **workflow_payload("workflow-runtime-ownership", "user-owner"),
                "agent_definitions": [
                    {
                        "id": "agent-1",
                        "name": "Runtime Agent",
                        "description": "Can run on multiple adapters",
                        "instructions": "Execute the workflow carefully.",
                        "role": "operator",
                        "backstory": "runtime-test",
                        "tool_ids": [],
                        "handoff_agent_ids": [],
                        "guardrails": [],
                        "memory": {
                            "enabled": False,
                            "strategy": None,
                            "scope": None,
                            "backend_ref": None,
                            "max_entries": None,
                            "ttl_seconds": None,
                            "config": {},
                        },
                        "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
                        "metadata": {},
                    }
                ],
                "allowed_runtime_adapter_ids": ["native", "crewai"],
                "default_runtime_adapter_id": "native",
            },
        )
        self.assertEqual(create_response.status_code, 200)

        for runtime_adapter_id in ("native", "crewai"):
            owner_response = self.client.post(
                "/workflows/workflow-runtime-ownership/executions",
                headers=self.owner_headers,
                json={
                    "input": {"topic": "ownership"},
                    "trigger": {"type": "manual", "created_by": "user-owner"},
                    "runtimeAdapterId": runtime_adapter_id,
                },
            )
            self.assertEqual(owner_response.status_code, 200)
            self.assertEqual(owner_response.json()["runtime_adapter_id"], runtime_adapter_id)

            cross_owner_response = self.client.post(
                "/workflows/workflow-runtime-ownership/executions",
                headers=self.other_headers,
                json={
                    "input": {"topic": "ownership"},
                    "trigger": {"type": "manual", "created_by": "user-other"},
                    "runtimeAdapterId": runtime_adapter_id,
                },
            )
            self.assertEqual(cross_owner_response.status_code, 403)

            direct_route_response = self.client.post(
                "/executions",
                headers=self.other_headers,
                json={
                    "workflowId": "workflow-runtime-ownership",
                    "input": {"topic": "ownership"},
                    "trigger": {"type": "manual", "created_by": "user-other"},
                    "runtimeAdapterId": runtime_adapter_id,
                },
            )
            self.assertEqual(direct_route_response.status_code, 403)

    def test_ownerless_workflow_execution_is_claimed_by_first_runner(self) -> None:
        payload = workflow_payload("workflow-runtime-ownerless", "user-owner")
        payload["metadata"] = {}
        payload["allowed_runtime_adapter_ids"] = ["native", "crewai"]
        payload["default_runtime_adapter_id"] = "native"
        asyncio.run(self.context.workflow_repo.create(WorkflowDefinition.model_validate(payload)))

        owner_response = self.client.post(
            "/workflows/workflow-runtime-ownerless/executions",
            headers=self.owner_headers,
            json={
                "input": {"topic": "claim"},
                "trigger": {"type": "manual", "created_by": "user-owner"},
                "runtimeAdapterId": "native",
            },
        )
        self.assertEqual(owner_response.status_code, 200)

        fetch_response = self.client.get("/workflows/workflow-runtime-ownerless")
        self.assertEqual(fetch_response.status_code, 200)
        self.assertEqual(fetch_response.json()["metadata"]["created_by"], "user-owner")
        self.assertEqual(fetch_response.json()["metadata"]["owner_ids"], ["user-owner"])

        cross_owner_response = self.client.post(
            "/workflows/workflow-runtime-ownerless/executions",
            headers=self.other_headers,
            json={
                "input": {"topic": "claim"},
                "trigger": {"type": "manual", "created_by": "user-other"},
                "runtimeAdapterId": "native",
            },
        )
        self.assertEqual(cross_owner_response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
