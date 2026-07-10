from __future__ import annotations

import asyncio
import unittest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import get_settings, reset_settings_cache


class ApiMainTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.owner_headers = {
            "x-agency-user-id": "user-main",
            "x-agency-user-email": "main@example.com",
        }
        self.client.post(
            "/users/sync",
            json={"id": "user-main", "email": "main@example.com", "display_name": "Main User"},
        )

    def tearDown(self):
        reset_settings_cache()

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
        self.assertIn("/integrations/connectors/{credential_id}/health", schema["paths"])
        self.assertIn("/health/onecli", schema["paths"])
        self.assertIn("/setup/status", schema["paths"])
        self.assertIn("/auth/bootstrap", schema["paths"])
        self.assertIn("/auth/login", schema["paths"])
        self.assertIn("/auth/me", schema["paths"])
        self.assertIn("/setup/model-profile", schema["paths"])
        self.assertIn("/setup/main-agent", schema["paths"])
        self.assertIn("/setup/recommended-agents", schema["paths"])
        self.assertIn("/onecli/rule-profiles/default", schema["paths"])
        self.assertIn("/onecli/admin/rule-profiles/default", schema["paths"])
        self.assertIn("/integrations/connectors/{credential_id}/history", schema["paths"])
        self.assertIn("/observability/connectors/history", schema["paths"])
        self.assertIn("/observability/connectors/retention", schema["paths"])
        self.assertIn("/integrations/conversations/channels/{channel_type}/messages", schema["paths"])
        self.assertIn("/tools/contracts", schema["paths"])
        self.assertIn("/tools/contracts/{tool_name}", schema["paths"])
        self.assertIn("/tools/{tool_name}/run", schema["paths"])
        self.assertIn("/persona", schema["paths"])
        self.assertIn("/persona/{persona_id}", schema["paths"])
        self.assertIn("/persona-factory/governance-labels", schema["paths"])
        self.assertIn("/persona-factory/item-types", schema["paths"])
        self.assertIn("/persona-factory/distill", schema["paths"])
        self.assertIn("/persona-factory/runs", schema["paths"])
        self.assertIn("/persona-factory/runs/{run_id}", schema["paths"])
        self.assertIn("/persona-factory/runs/{run_id}/items", schema["paths"])
        self.assertIn("/persona-factory/items/{item_id}", schema["paths"])
        self.assertIn("/persona-factory/items/{item_id}/approve", schema["paths"])
        self.assertIn("/persona-factory/items/{item_id}/reject", schema["paths"])
        self.assertIn("/persona-factory/runs/{run_id}/synthesize-package", schema["paths"])
        self.assertIn("/persona-factory/runs/{run_id}/publish", schema["paths"])
        self.assertNotIn("/personas", schema["paths"])
        self.assertNotIn("/skills", schema["paths"])
        self.assertNotIn("/skill-factory/distill", schema["paths"])
        self.assertNotIn("/api/tools/", schema["paths"])
        self.assertNotIn("/api/tools/list", schema["paths"])
        self.assertNotIn("/api/tools/{tool_id}", schema["paths"])
        self.assertIn("/executions", schema["paths"])
        self.assertIn("/goals", schema["paths"])
        self.assertIn("/goals/{goal_id}/complete", schema["paths"])
        self.assertIn("/workflows/{workflow_id}/versions", schema["paths"])
        self.assertIn("/workflows/{workflow_id}/versions/{revision}", schema["paths"])
        self.assertIn("/workflows/{workflow_id}/shared-memory", schema["paths"])
        self.assertIn("/workflows/{workflow_id}/agents/{agent_id}/promote", schema["paths"])
        self.assertIn("/workflows/{workflow_id}/executions", schema["paths"])
        self.assertIn("/workflows/{workflow_id}/executions/start", schema["paths"])
        self.assertIn("/executions/{execution_id}/artifacts/images/stream", schema["paths"])
        self.assertIn("/executions/{execution_id}/hitl/stream", schema["paths"])
        self.assertIn("/executions/{execution_id}/hitl/reply", schema["paths"])
        retired_prefixes = ("/api/crew", "/api/history", "/api/artifacts", "/api/hitl")
        for path in schema["paths"]:
            self.assertFalse(path.startswith(retired_prefixes), path)

    def test_disabled_optional_modules_are_omitted_from_openapi(self):
        with patch.dict(
            "os.environ",
            {
                "SMART_HOME_MODULE_ENABLED": "false",
                "PHYSICAL_DEVICES_MODULE_ENABLED": "false",
            },
            clear=False,
        ):
            reset_settings_cache()
            client = TestClient(create_app(context=self.context))
            response = client.get("/openapi.json")
        reset_settings_cache()

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        self.assertNotIn("/api/smart-home/module", paths)
        self.assertNotIn("/api/devices", paths)
        self.assertNotIn("/api/device-events", paths)
        self.assertNotIn("/api/physical/events/health", paths)
        self.assertIn("/capabilities", paths)

    def test_uninstalled_optional_module_packages_do_not_block_startup(self):
        with patch("app.modules.registry.entry_points", return_value=[]):
            client = TestClient(create_app(context=create_test_api_context()))
            openapi_response = client.get("/openapi.json")
            capabilities_response = client.get("/capabilities")

        self.assertEqual(openapi_response.status_code, 200)
        paths = openapi_response.json()["paths"]
        self.assertIn("/health", paths)
        self.assertIn("/capabilities", paths)
        self.assertNotIn("/api/smart-home/module", paths)
        self.assertNotIn("/api/devices", paths)
        self.assertNotIn("/api/device-events", paths)
        self.assertNotIn("/api/physical/events/health", paths)

        self.assertEqual(capabilities_response.status_code, 200)
        modules = capabilities_response.json()["modules"]
        self.assertNotIn("smart_home", modules)
        self.assertNotIn("physical_devices", modules)

    def test_core_only_mode_omits_builtin_optional_module_specs(self):
        with patch.dict("os.environ", {"AGENCY_BUILTIN_OPTIONAL_MODULES": ""}, clear=False), patch(
            "app.modules.registry.entry_points", return_value=[]
        ):
            reset_settings_cache()
            client = TestClient(create_app(context=create_test_api_context()))
            health_response = client.get("/health")
            openapi_response = client.get("/openapi.json")
            capabilities_response = client.get("/capabilities")
        reset_settings_cache()

        self.assertEqual(health_response.status_code, 200)
        self.assertEqual(openapi_response.status_code, 200)
        paths = openapi_response.json()["paths"]
        self.assertNotIn("/api/smart-home/module", paths)
        self.assertNotIn("/api/devices", paths)
        self.assertNotIn("/api/device-events", paths)
        self.assertNotIn("/api/physical/events/health", paths)
        self.assertEqual(capabilities_response.status_code, 200)
        self.assertNotIn("smart_home", capabilities_response.json()["modules"])
        self.assertNotIn("physical_devices", capabilities_response.json()["modules"])

    def test_capability_execution_metadata_uses_active_optional_module_specs(self):
        from app.api.routes.capabilities import _execution_metadata

        with patch.dict(
            "os.environ",
            {
                "AGENCY_BUILTIN_OPTIONAL_MODULES": "",
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:external_home_pack_specs",
            },
            clear=False,
        ):
            reset_settings_cache()
            metadata = _execution_metadata("agency.device.command")
        reset_settings_cache()

        self.assertTrue(metadata["supportsApprovalRequest"])
        self.assertIn("optional_module:physical_devices", metadata["sideEffects"])
        self.assertIn("module_mutation", metadata["sideEffects"])
        self.assertTrue(metadata["policyNotes"])

    def test_startup_accepts_configured_expected_optional_modules(self):
        with patch.dict(
            "os.environ",
            {
                "AGENCY_EXPECTED_OPTIONAL_MODULES": "smart_home,physical_devices",
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:external_home_pack_specs",
            },
            clear=False,
        ):
            reset_settings_cache()
            with TestClient(create_app(context=self.context)) as client:
                response = client.get("/health")
        reset_settings_cache()

        self.assertEqual(response.status_code, 200)

    def test_startup_fails_when_expected_optional_module_is_missing(self):
        with patch.dict("os.environ", {"AGENCY_EXPECTED_OPTIONAL_MODULES": "missing_pack"}, clear=False):
            reset_settings_cache()
            with self.assertRaisesRegex(RuntimeError, "expected optional module 'missing_pack' is not registered"):
                with TestClient(create_app(context=self.context)):
                    pass
        reset_settings_cache()

    def test_startup_fails_when_expected_optional_module_is_disabled(self):
        with patch.dict(
            "os.environ",
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:external_home_pack_specs",
                "AGENCY_EXPECTED_OPTIONAL_MODULES": "smart_home",
                "SMART_HOME_MODULE_ENABLED": "false",
            },
            clear=False,
        ):
            reset_settings_cache()
            with self.assertRaisesRegex(RuntimeError, "expected optional module 'smart_home' is registered but unavailable"):
                with TestClient(create_app(context=self.context)):
                    pass
        reset_settings_cache()

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

    def test_development_cors_allows_localhost_origin_by_default(self):
        with patch.dict(
                "os.environ",
                {
                    "APP_ENV": "development",
                    "AGENCY_ALLOWED_ORIGINS": "",
                },
                clear=False,
        ):
            reset_settings_cache()
            client = TestClient(create_app(context=self.context))
            response = client.options(
                "/health",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "GET",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:3000")

    def test_production_cors_allows_configured_frontend_origin(self):
        with patch.dict(
                "os.environ",
                {
                    "APP_ENV": "production",
                    "AGENCY_ALLOWED_ORIGINS": "https://agency-fe.example.com",
                },
                clear=False,
        ):
            reset_settings_cache()
            client = TestClient(create_app(context=self.context))
            response = client.options(
                "/health",
                headers={
                    "Origin": "https://agency-fe.example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "https://agency-fe.example.com")

    def test_production_cors_rejects_unknown_browser_origin(self):
        with patch.dict(
                "os.environ",
                {
                    "APP_ENV": "production",
                    "AGENCY_ALLOWED_ORIGINS": "https://agency-fe.example.com",
                },
                clear=False,
        ):
            reset_settings_cache()
            client = TestClient(create_app(context=self.context))
            response = client.options(
                "/health",
                headers={
                    "Origin": "https://unknown.example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(response.headers.get("access-control-allow-origin"), "https://unknown.example.com")

    def test_cors_rejects_wildcard_origins_when_credentials_are_enabled(self):
        with patch.dict(
                "os.environ",
                {
                    "AGENCY_ALLOWED_ORIGINS": "*",
                    "AGENCY_CORS_ALLOW_CREDENTIALS": "true",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaisesRegex(RuntimeError, "AGENCY_ALLOWED_ORIGINS cannot include"):
                get_settings().ensure_runtime_requirements()

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
            "/integrations/connectors/does-not-exist/health",
            "/integrations/connectors/does-not-exist/history",
            "/integrations/conversations/channels/web/resolve",
            "/tools",
            "/model-providers",
            "/model-profiles",
            "/mcp-servers",
            "/schedules",
            "/runtime-adapters",
            "/workflows",
            "/executions",
            "/goals",
            "/observability/connectors/history",
            "/observability/connectors/retention",
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
        self.assertEqual(statuses["/integrations/connectors/does-not-exist/health"], 404)
        self.assertEqual(statuses["/integrations/connectors/does-not-exist/history"], 404)
        self.assertEqual(statuses["/integrations/conversations/channels/web/resolve"], 405)
        self.assertEqual(statuses["/tools"], 200)
        self.assertEqual(statuses["/model-providers"], 200)
        self.assertEqual(statuses["/model-profiles"], 200)
        self.assertEqual(statuses["/mcp-servers"], 200)
        self.assertEqual(statuses["/schedules"], 200)
        self.assertEqual(statuses["/runtime-adapters"], 200)
        self.assertEqual(statuses["/workflows"], 200)
        self.assertEqual(statuses["/executions"], 200)
        self.assertEqual(statuses["/goals"], 200)
        self.assertEqual(statuses["/observability/connectors/history"], 200)
        self.assertEqual(statuses["/observability/connectors/retention"], 200)
        self.assertEqual(statuses["/observability/models/usage"], 200)
        self.assertEqual(statuses["/.well-known/agent-card.json"], 200)
        self.assertEqual(statuses["/a2a/tasks/does-not-exist"], 404)

    def test_onecli_health_disabled_returns_sanitized_diagnostics(self):
        with patch.dict(
                "os.environ",
                {
                    "ONECLI_ENABLED": "false",
                    "ONECLI_AGENT_TOKEN_SECRET_REF": "env://ONECLI_AGENT_TOKEN",
                    "ONECLI_EXTERNAL_CALLS_DISABLED": "true",
                },
                clear=False,
        ):
            reset_settings_cache()
            response = self.client.get("/health/onecli")
        reset_settings_cache()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["configured"])
        self.assertFalse(payload["enabled"])
        self.assertTrue(payload["external_calls_disabled"])
        self.assertFalse(payload["allow_global_agent_token_fallback"])
        self.assertFalse(payload["multi_user_mode"])
        self.assertTrue(payload["agent_token_secret_ref_configured"])
        self.assertNotIn("env://ONECLI_AGENT_TOKEN", str(payload))

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

    def test_management_console_write_request_is_audited(self):
        workflow = {
            "id": "workflow-management-console-audit",
            "name": "Management Console Audit",
            "description": "A minimal workflow for management-console audit tests",
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
            "/workflows",
            headers={**self.owner_headers, "x-agency-client": "agency-fe"},
            json=workflow,
        )

        self.assertEqual(response.status_code, 200)
        actions = self.context.runtime_operations.snapshot_dict()["recent_actions"]
        management_actions = [
            action for action in actions if action["action"] == "management_console.privileged_request"
        ]
        self.assertEqual(len(management_actions), 1)
        self.assertEqual(management_actions[0]["actor_user_id"], "user-main")
        self.assertEqual(management_actions[0]["client"], "agency-fe")
        self.assertEqual(management_actions[0]["identity_mode"], "trusted_identity_headers")
        self.assertEqual(management_actions[0]["method"], "POST")
        self.assertEqual(management_actions[0]["path"], "/workflows")
        self.assertEqual(management_actions[0]["required_scopes"], ["workflows:write"])

    def test_authorization_failure_is_audited_without_secret_material(self):
        response = self.client.get(
            "/agents/agent-missing/executions",
            headers={"authorization": "Bearer sk-secret-1234567890"},
        )

        self.assertEqual(response.status_code, 401)
        actions = self.context.runtime_operations.snapshot_dict()["recent_actions"]
        auth_failures = [action for action in actions if action["action"] == "authorization.failure"]
        self.assertEqual(len(auth_failures), 1)
        failure = auth_failures[0]
        self.assertEqual(failure["reason"], "invalid_or_revoked_api_token")
        self.assertEqual(failure["status_code"], 401)
        self.assertEqual(failure["method"], "GET")
        self.assertEqual(failure["path"], "/agents/agent-missing/executions")
        self.assertEqual(failure["required_scopes"], [])
        self.assertTrue(failure["has_bearer_token"])
        self.assertNotIn("sk-secret", str(failure))

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
            upload = self.client.put(
                "/api/local-storage/upload",
                headers=self.owner_headers,
                params={"file": "demo.txt"},
                content=b"hello",
            )
            self.assertEqual(upload.status_code, 200)
            mock_upload.assert_called_once()

        with patch("app.api.routes.health.get_local_file_path", side_effect=lambda filename: f"/tmp/{filename}"), patch(
                "app.api.routes.health.os.path.exists",
                side_effect=lambda path: path == "/tmp/demo.txt",
        ), patch(
                "app.api.routes.health.FileResponse",
                side_effect=lambda **kwargs: {"path": kwargs["path"], "filename": kwargs["filename"]},
        ):
            download = self.client.get(
                "/api/local-storage/download",
                headers=self.owner_headers,
                params={"file": "demo.txt"},
            )
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.json()["path"], "/tmp/demo.txt")


if __name__ == "__main__":
    unittest.main()
