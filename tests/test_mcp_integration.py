from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pathlib import Path
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.api.routes.mcp import create_mcp_router
from app.api.routes.tools import create_tools_router
from app.domain import AgentDefinition, MCPServerDefinition, MCPTransportType, ModelProfileDefinition, TaskDefinition, \
    UserDefinition, WorkflowDefinition, WorkflowNodeDefinition
from app.llm.base import ModelResponse, ModelToolCall
from app.protocols.mcp.computer_use_adapter import adapt_computer_use_arguments, normalize_computer_use_response
from app.protocols.mcp.client import build_mcp_process_environment, resolve_mcp_command
from app.protocols.mcp.registry import MCPClientRegistry, MCPRegistryError
from app.protocols.mcp.schemas import MCPToolDescriptor
from app.protocols.mcp.tool_adapter import mcp_tool_to_definition


class MCPModelClient:
    provider_key = "fake"

    def __init__(self, profile, env, *, tool_name: str, arguments: dict[str, str]):
        self.profile = profile
        self.tool_name = tool_name
        self.arguments = arguments
        self.calls = 0

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="Use the MCP tool",
                tool_calls=[
                    ModelToolCall(id=f"mcp-tool-{self.tool_name}", name=self.tool_name, arguments=self.arguments)],
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        return ModelResponse(
            content="Finished",
            provider=self.profile.provider,
            model=self.profile.model,
            latency_ms=1,
        )

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content={"ok": True}, provider=self.profile.provider, model=self.profile.model,
                             latency_ms=1)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "chunk"

    def count_tokens(self, messages, **kwargs):
        return 1

    def health_check(self):
        return {"ok": True}


class MCPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        self.server_definition = MCPServerDefinition(
            id="mcp-mock",
            name="Mock MCP",
            transport=MCPTransportType.STDIO,
            command=sys.executable,
            args=[str(Path(__file__).with_name("mock_mcp_server.py"))],
            enabled=True,
            allowlisted_command=Path(sys.executable).name,
        )
        asyncio.run(self.context.mcp_server_repo.create(self.server_definition))
        asyncio.run(
            self.context.user_repo.create(
                UserDefinition(id="user-mcp", email="mcp@example.com", display_name="MCP User")
            )
        )

    def test_discovery_syncs_tools_resources_and_prompts(self):
        asyncio.run(self._assert_discovery_syncs_tools_resources_and_prompts())

    def test_user_added_stdio_server_authorizes_its_saved_command(self):
        registry = MCPClientRegistry()
        definition = MCPServerDefinition(
            id="mcp-user-added",
            name="User Added MCP",
            transport=MCPTransportType.STDIO,
            command=sys.executable,
            args=[str(Path(__file__).with_name("mock_mcp_server.py"))],
            enabled=True,
        )

        registry.register(definition)
        discovered = registry.discover("mcp-user-added")

        self.assertEqual(definition.allowlisted_command, Path(sys.executable).name)
        self.assertTrue(discovered["mcp-user-added"].tools)

    def test_server_record_rejects_mismatched_allowlisted_command(self):
        registry = MCPClientRegistry()
        definition = MCPServerDefinition(
            id="mcp-mismatch",
            name="Mismatched MCP",
            transport=MCPTransportType.STDIO,
            command=sys.executable,
            args=[str(Path(__file__).with_name("mock_mcp_server.py"))],
            enabled=True,
            allowlisted_command="different-command",
        )

        registry.register(definition)

        with self.assertRaisesRegex(MCPRegistryError, "does not match command"):
            registry.discover("mcp-mismatch")

    async def _assert_discovery_syncs_tools_resources_and_prompts(self):
        sync = await self.context.sync_mcp_catalog(server_id="mcp-mock")
        self.assertTrue(sync["tools"])
        self.assertTrue(sync["resources"])
        self.assertTrue(sync["prompts"])

        saved_tool = await self.context.tool_repo.get("mcp:mcp-mock:echo_context")
        self.assertIsNotNone(saved_tool)
        self.assertEqual(saved_tool.tool_type.value, "mcp_tool")

        risky_tool = await self.context.tool_repo.get("mcp:mcp-mock:shell_access")
        self.assertTrue(risky_tool.security.requires_approval)
        self.assertEqual(risky_tool.security.allowlisted_mcp_servers, ["mcp-mock"])

    def test_builtin_computer_use_server_seeds_include_macos_and_windows(self):
        first, second = asyncio.run(self._seed_builtin_computer_use_servers())

        self.assertEqual(set(first), {self.context.COMPUTER_USE_MACOS_MCP_SERVER_ID,
                                      self.context.COMPUTER_USE_WINDOWS_MCP_SERVER_ID})
        self.assertEqual(set(second), set(first))

        macos_server = first[self.context.COMPUTER_USE_MACOS_MCP_SERVER_ID]
        windows_server = first[self.context.COMPUTER_USE_WINDOWS_MCP_SERVER_ID]

        self.assertEqual(macos_server.command, "uvx")
        self.assertEqual(macos_server.args, ["macos-mcp"])
        self.assertEqual(macos_server.metadata["platform"], "macos")
        self.assertEqual(macos_server.allowlisted_command, "uvx")
        self.assertEqual(windows_server.command, "uvx")
        self.assertEqual(windows_server.args, ["windows-mcp"])
        self.assertEqual(windows_server.metadata["platform"], "windows")
        self.assertEqual(windows_server.allowlisted_command, "uvx")

    def test_builtin_firecrawl_server_seed_uses_env_ref_and_npx(self):
        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}, clear=False):
            seeded = asyncio.run(self.context.ensure_builtin_research_mcp_server_seed_data())

        server = seeded[self.context.FIRECRAWL_MCP_SERVER_ID]
        self.assertTrue(server.enabled)
        self.assertEqual(server.command, "npx")
        self.assertEqual(server.args, ["-y", "firecrawl-mcp"])
        self.assertEqual(server.allowlisted_command, "npx")
        self.assertEqual(server.env_refs[0].ref, "env://FIRECRAWL_API_KEY")
        self.assertEqual(server.env_refs[0].key, "FIRECRAWL_API_KEY")

    def test_builtin_context7_server_seed_uses_npx_and_optional_env_ref(self):
        with patch.dict(os.environ, {"CONTEXT7_API_KEY": "ctx7-test"}, clear=False):
            seeded = asyncio.run(self.context.ensure_builtin_research_mcp_server_seed_data())

        server = seeded[self.context.CONTEXT7_MCP_SERVER_ID]
        self.assertTrue(server.enabled)
        self.assertEqual(server.command, "npx")
        self.assertEqual(server.args, ["-y", "@upstash/context7-mcp"])
        self.assertEqual(server.allowlisted_command, "npx")
        self.assertEqual(server.env_refs[0].ref, "env://CONTEXT7_API_KEY")
        self.assertEqual(server.env_refs[0].key, "CONTEXT7_API_KEY")

    def test_builtin_context7_can_run_without_api_key_when_explicitly_enabled(self):
        with patch.dict(os.environ, {"CONTEXT7_MCP_ENABLED": "true"}, clear=False):
            os.environ.pop("CONTEXT7_API_KEY", None)
            seeded = asyncio.run(self.context.ensure_builtin_research_mcp_server_seed_data())

        server = seeded[self.context.CONTEXT7_MCP_SERVER_ID]
        self.assertTrue(server.enabled)
        self.assertEqual(server.env_refs, [])

    def test_stdio_mcp_env_refs_resolve_into_process_env_without_parent_path(self):
        server = MCPServerDefinition(
            id="mcp-env",
            name="Env MCP",
            transport=MCPTransportType.STDIO,
            command="npx",
            env_refs=[
                {
                    "ref": "env://FIRECRAWL_API_KEY",
                    "key": "FIRECRAWL_API_KEY",
                    "source": "env",
                }
            ],
            enabled=True,
            allowlisted_command="npx",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            npx_path = Path(temp_dir) / "npx"
            npx_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            npx_path.chmod(0o755)
            with patch.dict(
                    os.environ,
                    {"FIRECRAWL_API_KEY": "fc-secret", "PATH": "/usr/bin", "MCP_SERVER_EXTRA_PATHS": temp_dir},
                    clear=True,
            ):
                env = build_mcp_process_environment(server)
                resolved_command = resolve_mcp_command("npx", env)

        self.assertEqual(env["FIRECRAWL_API_KEY"], "fc-secret")
        self.assertEqual(env["PATH"].split(os.pathsep)[0], temp_dir)
        self.assertIn("/opt/homebrew/bin", env["PATH"].split(os.pathsep))
        self.assertEqual(resolved_command, str(npx_path))

    async def _seed_builtin_computer_use_servers(self) -> tuple[
        dict[str, MCPServerDefinition], dict[str, MCPServerDefinition]]:
        first = await self.context.ensure_builtin_computer_use_mcp_server_seed_data()
        second = await self.context.ensure_builtin_computer_use_mcp_server_seed_data()
        return first, second

    def test_builtin_computer_use_server_ids_for_host_prefers_current_platform(self):
        self.assertEqual(self.context.builtin_computer_use_server_ids_for_host(),
                         [self.context.COMPUTER_USE_MACOS_MCP_SERVER_ID])

    def test_computer_use_tools_are_normalized_to_agency_names(self):
        server = MCPServerDefinition(
            id=self.context.COMPUTER_USE_MACOS_MCP_SERVER_ID,
            name="Computer Use macOS MCP",
            transport=MCPTransportType.STDIO,
            command="uvx",
            args=["macos-mcp"],
            enabled=True,
            allowlisted_command="uvx",
            metadata={"family": "computer_use", "platform": "macos"},
        )
        shortcut_tool = MCPToolDescriptor(
            name="Shortcut",
            description="Press keyboard shortcuts",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": False},
            metadata={},
        )
        snapshot_tool = MCPToolDescriptor(
            name="Snapshot",
            description="Capture desktop state",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True},
            metadata={},
        )

        shortcut_definition = mcp_tool_to_definition(server, shortcut_tool)
        snapshot_definition = mcp_tool_to_definition(server, snapshot_tool)

        self.assertEqual(shortcut_definition.id, "mcp:computer-use-macos:press_key")
        self.assertEqual(shortcut_definition.name, "press_key")
        self.assertEqual(shortcut_definition.display_name, "Press Key")
        self.assertEqual(shortcut_definition.implementation.config["mcp_tool_name"], "Shortcut")
        self.assertEqual(shortcut_definition.implementation.config["canonical_tool_name"], "press_key")
        self.assertEqual(shortcut_definition.input_schema["required"], ["keys"])
        self.assertEqual(snapshot_definition.id, "mcp:computer-use-macos:snapshot")
        self.assertEqual(snapshot_definition.name, "snapshot")
        self.assertEqual(snapshot_definition.display_name, "Snapshot")
        self.assertEqual(snapshot_definition.framework_hints.metadata["remote_tool_name"], "Snapshot")

    def test_computer_use_argument_adapter_uses_remote_schema_hints(self):
        server = MCPServerDefinition(
            id=self.context.COMPUTER_USE_WINDOWS_MCP_SERVER_ID,
            name="Computer Use Windows MCP",
            transport=MCPTransportType.STDIO,
            command="uvx",
            args=["windows-mcp"],
            enabled=True,
            allowlisted_command="uvx",
            metadata={"family": "computer_use", "platform": "windows"},
        )
        shortcut_tool = MCPToolDescriptor(
            name="Shortcut",
            description="Press keyboard shortcuts",
            input_schema={
                "type": "object",
                "properties": {
                    "shortcut": {"type": "string"},
                },
                "required": ["shortcut"],
            },
            annotations={"readOnlyHint": False},
            metadata={},
        )
        click_tool = MCPToolDescriptor(
            name="Click",
            description="Click by coordinates",
            input_schema={
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "click_type": {"type": "string"},
                },
                "required": ["x", "y"],
            },
            annotations={"readOnlyHint": False},
            metadata={},
        )

        shortcut_definition = mcp_tool_to_definition(server, shortcut_tool)
        click_definition = mcp_tool_to_definition(server, click_tool)

        adapted_shortcut = adapt_computer_use_arguments(shortcut_definition, {"keys": "Ctrl+C"})
        adapted_click = adapt_computer_use_arguments(click_definition, {"x": 10, "y": 20, "double_click": True})

        self.assertEqual(adapted_shortcut, {"shortcut": "Ctrl+C"})
        self.assertEqual(adapted_click["x"], 10)
        self.assertEqual(adapted_click["y"], 20)
        self.assertEqual(adapted_click["click_type"], "double")

    def test_computer_use_response_normalizer_wraps_snapshot_results(self):
        server = MCPServerDefinition(
            id=self.context.COMPUTER_USE_MACOS_MCP_SERVER_ID,
            name="Computer Use macOS MCP",
            transport=MCPTransportType.STDIO,
            command="uvx",
            args=["macos-mcp"],
            enabled=True,
            allowlisted_command="uvx",
            metadata={"family": "computer_use", "platform": "macos"},
        )
        snapshot_tool = MCPToolDescriptor(
            name="Snapshot",
            description="Capture desktop state",
            input_schema={"type": "object", "properties": {"display": {"type": "array"}}},
            annotations={"readOnlyHint": True},
            metadata={},
        )
        definition = mcp_tool_to_definition(server, snapshot_tool)
        raw_result = {
            "content": [{"type": "text", "text": "{\"title\":\"Desktop\",\"text\":\"Visible UI text\"}"}],
            "result": {
                "image": "base64-image",
                "windows": [{"title": "Desktop"}],
            },
        }

        normalized = normalize_computer_use_response(
            definition,
            {"display": [1], "use_vision": True},
            {"display": [1], "use_vision": True},
            raw_result,
        )

        self.assertEqual(normalized["tool"], "snapshot")
        self.assertEqual(normalized["platform"], "macos")
        self.assertEqual(normalized["data"]["image"], "base64-image")
        self.assertEqual(normalized["data"]["windows"], [{"title": "Desktop"}])
        self.assertEqual(normalized["artifact_uri"], "data:image/png;base64,base64-image")
        self.assertEqual(normalized["artifact_media_type"], "image/png")
        self.assertEqual(normalized["request"]["display"], [1])
        self.assertEqual(normalized["raw_result"], raw_result)

    def test_computer_use_response_normalizer_uses_file_backed_screenshot_artifact(self):
        server = MCPServerDefinition(
            id=self.context.COMPUTER_USE_WINDOWS_MCP_SERVER_ID,
            name="Computer Use Windows MCP",
            transport=MCPTransportType.STDIO,
            command="uvx",
            args=["windows-mcp"],
            enabled=True,
            allowlisted_command="uvx",
            metadata={"family": "computer_use", "platform": "windows"},
        )
        screenshot_tool = MCPToolDescriptor(
            name="Screenshot",
            description="Capture a screenshot",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True},
            metadata={},
        )
        definition = mcp_tool_to_definition(server, screenshot_tool)
        raw_result = {
            "result": {
                "screenshot_path": "/tmp/computer-use/capture.webp",
                "windows": [{"title": "Desktop"}],
            }
        }

        normalized = normalize_computer_use_response(
            definition,
            {"display": [0]},
            {"display": [0]},
            raw_result,
        )

        self.assertEqual(normalized["tool"], "screenshot")
        self.assertEqual(normalized["artifact_uri"], "/tmp/computer-use/capture.webp")
        self.assertEqual(normalized["artifact_name"], "capture.webp")
        self.assertEqual(normalized["artifact_media_type"], "image/webp")

    def test_computer_use_response_normalizer_wraps_shell_results(self):
        server = MCPServerDefinition(
            id=self.context.COMPUTER_USE_WINDOWS_MCP_SERVER_ID,
            name="Computer Use Windows MCP",
            transport=MCPTransportType.STDIO,
            command="uvx",
            args=["windows-mcp"],
            enabled=True,
            allowlisted_command="uvx",
            metadata={"family": "computer_use", "platform": "windows"},
        )
        shell_tool = MCPToolDescriptor(
            name="Shell",
            description="Run a shell command",
            input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
            annotations={"readOnlyHint": False},
            metadata={},
        )
        definition = mcp_tool_to_definition(server, shell_tool)
        raw_result = {
            "result": {
                "stdout": "ok",
                "stderr": "",
                "exit_code": 0,
            }
        }

        normalized = normalize_computer_use_response(
            definition,
            {"command": "dir"},
            {"command": "dir"},
            raw_result,
        )

        self.assertEqual(normalized["tool"], "shell")
        self.assertEqual(normalized["status"], "ok")
        self.assertEqual(normalized["data"]["stdout"], "ok")
        self.assertEqual(normalized["data"]["exit_code"], 0)

    def test_workflow_can_execute_mcp_tool_with_redacted_approval_logs(self):
        asyncio.run(self._assert_workflow_can_execute_mcp_tool_with_redacted_approval_logs())

    async def _assert_workflow_can_execute_mcp_tool_with_redacted_approval_logs(self):
        await self.context.sync_mcp_catalog(server_id="mcp-mock")
        risky_tool = await self.context.tool_repo.get("mcp:mcp-mock:shell_access")
        profile = ModelProfileDefinition(
            id="profile-mcp",
            name="MCP Profile",
            provider="fake",
            model="fake-model",
            supports_tools=True,
        )
        await self.context.runtime_registry.register_model_profile(profile)
        self.context.llm_provider_registry.register(
            "fake",
            lambda profile, env: MCPModelClient(profile, env, tool_name="shell_access",
                                                arguments={"command": "ls", "token": "top-secret"}),
        )

        agent = AgentDefinition(
            id="agent-mcp",
            name="MCP Agent",
            instructions="Use the MCP tool.",
            model_profile_id=profile.id,
            tool_ids=[risky_tool.id],
        )
        task = TaskDefinition(
            id="task-mcp",
            name="Task MCP",
            description="Use MCP",
            agent_id=agent.id,
            tool_ids=[risky_tool.id],
        )
        node = WorkflowNodeDefinition(
            id="node-mcp",
            name="Node MCP",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-mcp",
            name="Workflow MCP",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[risky_tool],
            default_runtime_adapter_id="native",
        )
        await self.context.runtime_registry.register_workflow(workflow)
        execution = await self.context.runtime_registry.create_execution(workflow.id, {}, {"created_by": "tester"},
                                                                         runtime_adapter_id="native")
        await self.context.control_plane.queue_start(execution.id)
        await asyncio.sleep(0.05)

        events = await self.context.execution_store.list_events(execution.id)
        approval_events = [event for event in events if event.event_type.value == "approval.requested"]
        self.assertTrue(approval_events)
        self.assertEqual(approval_events[-1].payload["arguments"]["token"], "[REDACTED]")

        approved = await self.context.control_plane.approve(execution.id, risky_tool.id, "approved")
        self.assertTrue(approved)
        await asyncio.sleep(0.05)

        final = await self.context.execution_store.get_execution(execution.id)
        self.assertEqual(final.status.value, "completed")
        tool_completed = [event for event in await self.context.execution_store.list_events(execution.id) if
                          event.event_type.value == "tool.call.completed"]
        self.assertTrue(tool_completed)


class MCPCatalogApiTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        asyncio.run(
            self.context.user_repo.create(
                UserDefinition(id="user-mcp-catalog", email="mcp-catalog@example.com", display_name="MCP Catalog User")
            )
        )
        app = FastAPI()
        app.include_router(create_mcp_router(self.context))
        app.include_router(create_tools_router(self.context))
        self.client = TestClient(app)
        self.client.headers.update(
            {
                "x-agency-user-id": "user-mcp-catalog",
                "x-agency-user-email": "mcp-catalog@example.com",
            }
        )

    def test_mcp_server_crud_and_discovery_endpoint(self):
        payload = {
            "id": "mcp-mock",
            "name": "Mock MCP",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(Path(__file__).with_name("mock_mcp_server.py"))],
            "url": None,
            "env_refs": [],
            "enabled": True,
            "metadata": {},
        }
        create = self.client.post("/mcp-servers", json=payload)
        self.assertEqual(create.status_code, 200)
        self.assertEqual(create.json()["allowlisted_command"], Path(sys.executable).name)

        discover = self.client.post("/mcp-servers/discover", json={"serverId": "mcp-mock"})
        self.assertEqual(discover.status_code, 200)
        self.assertTrue(discover.json()["tools"])

        tools = self.client.get("/tools")
        ids = {item["id"] for item in tools.json()["items"]}
        self.assertIn("mcp:mcp-mock:echo_context", ids)

    def test_builtin_computer_use_server_is_seeded_on_startup(self):
        with TestClient(create_app(context=self.context)):
            pass

        macos_seeded = asyncio.run(
            self.context.mcp_server_repo.get(self.context.COMPUTER_USE_MACOS_MCP_SERVER_ID, include_deleted=True))
        windows_seeded = asyncio.run(
            self.context.mcp_server_repo.get(self.context.COMPUTER_USE_WINDOWS_MCP_SERVER_ID, include_deleted=True))
        self.assertIsNotNone(macos_seeded)
        self.assertIsNotNone(windows_seeded)
        self.assertEqual(macos_seeded.id, self.context.COMPUTER_USE_MACOS_MCP_SERVER_ID)
        self.assertEqual(windows_seeded.id, self.context.COMPUTER_USE_WINDOWS_MCP_SERVER_ID)


if __name__ == "__main__":
    unittest.main()
