from __future__ import annotations

import unittest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.api.main import create_app


class ApiMainTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.owner_headers = {
            "x-agency-user-id": "user-main",
            "x-agency-user-email": "main@example.com",
        }

    def test_app_starts_and_openapi_generates(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(schema["info"]["title"], "Agency API")
        self.assertIn("/agents", schema["paths"])
        self.assertIn("/conversations", schema["paths"])
        self.assertIn("/conversations/main-agent-profile", schema["paths"])
        self.assertIn("/conversations/approval-requests/{approval_request_id}/approve", schema["paths"])
        self.assertIn("/conversations/{conversation_id}/stream", schema["paths"])
        self.assertIn("/credentials/connectors/{provider_key}/schema", schema["paths"])
        self.assertIn("/credentials/connectors/{provider_key}/validate", schema["paths"])
        self.assertIn("/credentials/{credential_id}/connector", schema["paths"])
        self.assertIn("/integrations/categories", schema["paths"])
        self.assertIn("/capabilities", schema["paths"])
        self.assertIn("/integrations/connectors/capabilities", schema["paths"])
        self.assertNotIn("/integrations/connectors/{credential_id}/health", schema["paths"])
        self.assertNotIn("/integrations/connectors/{credential_id}/history", schema["paths"])
        self.assertNotIn("/observability/connectors/history", schema["paths"])
        self.assertNotIn("/observability/connectors/retention", schema["paths"])
        self.assertNotIn("/users", schema["paths"])
        self.assertNotIn("/users/sync", schema["paths"])
        self.assertNotIn("/me", schema["paths"])
        self.assertNotIn("/api-tokens", schema["paths"])
        self.assertNotIn("/marketplace/workflows", schema["paths"])
        self.assertNotIn("/memories", schema["paths"])
        self.assertIn("/integrations/conversations/channels/{channel_type}/messages", schema["paths"])
        self.assertIn("/tools/contracts", schema["paths"])
        self.assertIn("/tools/contracts/{tool_name}", schema["paths"])
        self.assertIn("/tools/{tool_name}/run", schema["paths"])
        self.assertNotIn("/api/tools/", schema["paths"])
        self.assertNotIn("/api/tools/list", schema["paths"])
        self.assertNotIn("/api/tools/{tool_id}", schema["paths"])
        self.assertIn("/executions", schema["paths"])
        self.assertIn("/workflows/{workflow_id}/versions", schema["paths"])
        self.assertIn("/workflows/{workflow_id}/versions/{revision}", schema["paths"])
        self.assertIn("/workflows/{workflow_id}/shared-memory", schema["paths"])
        self.assertIn("/workflows/{workflow_id}/executions", schema["paths"])
        self.assertIn("/workflows/{workflow_id}/executions/start", schema["paths"])
        self.assertIn("/executions/{execution_id}/artifacts/images/stream", schema["paths"])
        self.assertIn("/executions/{execution_id}/hitl/stream", schema["paths"])
        self.assertIn("/executions/{execution_id}/hitl/reply", schema["paths"])
        retired_prefixes = ("/api/crew", "/api/history", "/api/artifacts", "/api/hitl")
        for path in schema["paths"]:
            self.assertFalse(path.startswith(retired_prefixes), path)

    def test_route_table_has_no_duplicate_method_path_pairs(self):
        app = create_app(context=self.context)
        seen: set[tuple[str, str]] = set()
        duplicates: list[tuple[str, str]] = []
        for route in app.routes:
            methods = getattr(route, "methods", set())
            path = getattr(route, "path", None)
            if not path:
                continue
            for method in methods:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                key = (method, path)
                if key in seen:
                    duplicates.append(key)
                seen.add(key)
        self.assertEqual(duplicates, [])

    def test_core_route_groups_are_reachable(self):
        expected_ok = [
            "/",
            "/health",
            "/agents",
            "/conversations",
            "/conversations/main-agent-profile",
            "/credentials/connectors/telegram/schema",
            "/integrations/categories",
            "/integrations/connectors/capabilities",
            "/integrations/conversations/channels/web/resolve",
            "/tools",
            "/model-providers",
            "/model-profiles",
            "/mcp-servers",
            "/schedules",
            "/runtime-adapters",
            "/workflows",
            "/executions",
            "/observability/models/usage",
            "/.well-known/agent-card.json",
            "/a2a/tasks/does-not-exist",
        ]
        statuses = {}
        for path in expected_ok:
            response = self.client.get(path, headers=self.owner_headers)
            statuses[path] = response.status_code

        self.assertEqual(statuses["/"], 200)
        self.assertEqual(statuses["/health"], 200)
        self.assertEqual(statuses["/agents"], 200)
        self.assertEqual(statuses["/conversations"], 200)
        self.assertEqual(statuses["/conversations/main-agent-profile"], 404)
        self.assertEqual(statuses["/credentials/connectors/telegram/schema"], 200)
        self.assertEqual(statuses["/integrations/categories"], 200)
        self.assertEqual(statuses["/integrations/connectors/capabilities"], 200)
        self.assertEqual(statuses["/integrations/conversations/channels/web/resolve"], 405)
        self.assertEqual(statuses["/tools"], 200)
        self.assertEqual(statuses["/model-providers"], 200)
        self.assertEqual(statuses["/model-profiles"], 200)
        self.assertEqual(statuses["/mcp-servers"], 200)
        self.assertEqual(statuses["/schedules"], 200)
        self.assertEqual(statuses["/runtime-adapters"], 200)
        self.assertEqual(statuses["/workflows"], 200)
        self.assertEqual(statuses["/executions"], 200)
        self.assertEqual(statuses["/observability/models/usage"], 200)
        self.assertEqual(statuses["/.well-known/agent-card.json"], 200)
        self.assertEqual(statuses["/a2a/tasks/does-not-exist"], 404)

    def test_tools_route_seeds_builtin_assignable_tools(self):
        response = self.client.get("/tools", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)

        tool_ids = {item["id"] for item in response.json()["items"]}
        self.assertIn("agency.command.run", tool_ids)
        self.assertIn("agency.workflow.list", tool_ids)
        self.assertIn("agency.http.request", tool_ids)

    def test_docs_uis_are_reachable(self):
        docs_response = self.client.get("/docs")
        self.assertEqual(docs_response.status_code, 200)
        self.assertIn("Swagger UI", docs_response.text)

        redoc_response = self.client.get("/redoc")
        self.assertEqual(redoc_response.status_code, 200)
        self.assertIn("ReDoc", redoc_response.text)

    def test_root_payload_includes_api_docs_links(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "message": "Welcome to Agency",
                "docs": "/docs",
                "openapi": "/openapi.json",
                "redoc": "/redoc",
            },
        )

    def test_workflow_scoped_execution_start_is_reachable(self):
        workflow = {
            "id": "workflow-runtime-route",
            "name": "Runtime Route Workflow",
            "description": "A minimal workflow for execution route tests",
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
            "metadata": {},
        }

        response = self.client.post(
            "/workflows/workflow-runtime-route/executions/start",
            headers=self.owner_headers,
            json={
                "input": {"inputs": {"topic": "routing"}},
                "trigger": {"type": "manual"},
                "runtimeAdapterId": "native",
                "workflow_definition": workflow,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["process_id"], payload["execution"]["id"])
        self.assertEqual(payload["execution"]["workflow_id"], "workflow-runtime-route")

    def test_existing_frontend_paths_remain_reachable(self):
        with patch("app.api.routes.storage.generate_presigned_url", return_value="http://example.test/upload"):
            tools = self.client.get("/tools", headers=self.owner_headers)
            self.assertEqual(tools.status_code, 200)
            self.assertIn("agency.file.write-text", {tool["id"] for tool in tools.json()["items"]})

            contracts = self.client.get("/tools/contracts", headers=self.owner_headers)
            self.assertEqual(contracts.status_code, 200)
            self.assertIn("sandbox-edit", {contract["name"] for contract in contracts.json()["items"]})

            presigned = self.client.post(
                "/storage/presigned",
                json={"operation": "upload", "filename": "demo.txt", "content_type": "text/plain"},
            )
            self.assertEqual(presigned.status_code, 200)
            self.assertEqual(presigned.json()["url"], "http://example.test/upload")

    def test_local_storage_upload_and_download_paths_work(self):
        with patch("app.api.routes.health.get_local_file_path", side_effect=lambda filename: f"/tmp/{filename}"), patch(
                "app.api.routes.health.mock_upload_to_local"
        ) as mock_upload:
            upload = self.client.put("/api/local-storage/upload", params={"file": "demo.txt"}, content=b"hello")
            self.assertEqual(upload.status_code, 200)
            mock_upload.assert_called_once()

        with patch("app.api.routes.health.os.path.exists", side_effect=lambda path: path == "/tmp/demo.txt"), patch(
                "app.api.routes.health.FileResponse",
                side_effect=lambda **kwargs: {"path": kwargs["path"], "filename": kwargs["filename"]},
        ):
            download = self.client.get("/api/local-storage/download", params={"file": "/tmp/demo.txt"})
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.json()["path"], "/tmp/demo.txt")


if __name__ == "__main__":
    unittest.main()
