from __future__ import annotations

import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.routes.tools import create_tools_router
from app.domain import UserDefinition


class ToolRegistryApiTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        app = FastAPI()
        app.include_router(create_tools_router(self.context))
        self.client = TestClient(app)
        self.client.headers.update(
            {
                "x-agency-user-id": "user-tools",
                "x-agency-user-email": "tools@example.com",
            }
        )
        import asyncio

        asyncio.run(
            self.context.user_repo.create(
                UserDefinition(id="user-tools", email="tools@example.com", display_name="Tools User")
            )
        )

    def _python_tool_payload(self, *, tool_id: str = "tool-echo", module: str = "tests.native_test_tools"):
        return {
            "id": tool_id,
            "name": "Echo Tool",
            "description": "Echoes text",
            "tool_type": "python_function",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "output_schema": {"type": "object"},
            "implementation": {
                "implementation_type": "python_function",
                "target": module,
                "callable_name": "echo_tool",
                "config": {},
            },
            "security": {
                "requires_approval": False,
                "sandbox_required": False,
                "allow_shell": False,
                "allow_browser": False,
                "allow_filesystem": False,
                "allow_network": False,
                "allowlisted_domains": [],
                "allowlisted_mcp_servers": [],
                "module_allowlist": [module],
                "function_allowlist": ["echo_tool"],
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

    def test_tool_validate_rejects_embedded_secret(self):
        payload = self._python_tool_payload(tool_id="tool-secret", module="external.module")
        payload["implementation"]["config"] = {"api_key": "plain-text-secret"}
        payload["security"]["module_allowlist"] = ["external.module"]

        response = self.client.post("/tools/validate", json={"toolDefinition": payload})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["valid"])
        codes = {item["code"] for item in body["validation_errors"]}
        self.assertIn("tool.credentials.embedded", codes)

    def test_shell_tool_requires_approval_and_sandbox(self):
        payload = self._python_tool_payload(tool_id="tool-shell")
        payload["tool_type"] = "shell_command"
        payload["implementation"] = {
            "implementation_type": "shell_command",
            "target": "echo hi",
            "config": {},
        }
        payload["security"]["allow_shell"] = False

        response = self.client.post("/tools", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_http_tool_validation_requires_domain_allowlist(self):
        payload = self._python_tool_payload(tool_id="tool-http", module="external.http")
        payload["tool_type"] = "http_request"
        payload["implementation"] = {
            "implementation_type": "http_request",
            "target": "https://example.com/api",
            "config": {"method": "GET"},
        }
        payload["security"]["module_allowlist"] = []
        payload["security"]["allow_network"] = True

        response = self.client.post("/tools/validate", json={"toolDefinition": payload})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["valid"])
        codes = {item["code"] for item in body["validation_errors"]}
        self.assertIn("tool.http.allowlist.missing", codes)

    def test_tool_test_endpoint_executes_and_audits_python_tool(self):
        payload = self._python_tool_payload()
        create = self.client.post("/tools", json=payload)
        self.assertEqual(create.status_code, 200)

        response = self.client.post("/tools/tool-echo/test", json={"input": {"text": "hello"}})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["output"]["echo"], "hello")
        event_types = [item["event_type"] for item in body["events"]]
        self.assertIn("tool.call.started", event_types)
        self.assertIn("tool.call.completed", event_types)


if __name__ == "__main__":
    unittest.main()
