from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from app.core.config import reset_settings_cache
from app.domain import (
    ChannelIdentityMapping,
    MCPExposureSettings,
    ExecutionStatus,
    MemoryRecord,
    ModelProfileDefinition,
    SecuritySettings,
    ToolDefinition,
    ToolImplementationReference,
    ToolType,
    WorkflowDefinition,
)
from app.llm.base import ModelResponse, ModelToolCall
from app.llm.registry import LLMEnvironmentConfig
from app.services.conversations import ConversationService
from app.services.main_agent_setup import (
    MainAgentSetupConfig,
    MainAgentSetupRequiredError,
    MainAgentSetupService,
)
from app.services.workflow_builder import WorkflowBuilderService


class _FakeModelClient:
    provider_key = "fake"
    last_system_message: str | None = None
    responses: list[ModelResponse] = []
    seen_messages: list[list[tuple[str, object, str | None]]] = []
    seen_tools: list[list[dict] | None] = []

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        _FakeModelClient.seen_messages.append([(item.role, item.content, item.name) for item in messages])
        _FakeModelClient.seen_tools.append(kwargs.get("tools"))
        _FakeModelClient.last_system_message = next((item.content for item in messages if item.role == "system"), None)
        if _FakeModelClient.responses:
            return _FakeModelClient.responses.pop(0)
        latest_user = next((item.content for item in reversed(messages) if item.role == "user"), "hello")
        return ModelResponse(content=f"Generated: {latest_user}", provider="fake", model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        schema_name = kwargs.get("schema_name")
        if schema_name == "workflow_builder_task_list":
            return ModelResponse(
                content={
                    "assistant_message": "I drafted the workflow.",
                    "tasks": [
                        {
                            "name": "Draft Launch Plan",
                            "description": "Create a concise launch plan.",
                            "expected_output": "A launch plan.",
                        }
                    ],
                },
                provider="fake",
                model=self.profile.model,
            )
        if schema_name == "workflow_builder_agent_list":
            return ModelResponse(
                content={
                    "agents": [
                        {
                            "name": "Launch Planner",
                            "role": "Plans launches",
                            "instructions": "Create practical launch plans.",
                            "backstory": "Experienced in structured launch planning.",
                        }
                    ]
                },
                provider="fake",
                model=self.profile.model,
            )
        if schema_name == "workflow_builder_workflow_summary":
            return ModelResponse(
                content={
                    "workflow": {
                        "name": "Launch Planning Workflow",
                        "description": "Creates a concise launch plan from a user request.",
                    }
                },
                provider="fake",
                model=self.profile.model,
            )
        if schema_name == "workflow_builder_repair_workflow":
            return ModelResponse(
                content={
                    "workflow": {
                        "id": "model-repaired-workflow",
                        "name": "Model Repaired Workflow",
                        "description": "Repaired by the workflow builder.",
                        "entrypoint": "node-1",
                        "nodes": [
                            {
                                "id": "node-1",
                                "name": "Entry",
                                "node_type": "task",
                                "task_id": "task-1",
                                "config": {},
                                "metadata": {},
                            }
                        ],
                        "task_definitions": [
                            {
                                "id": "task-1",
                                "name": "Task One",
                                "description": "Do the repaired work",
                                "agent_id": "repair-agent",
                                "tool_ids": [],
                                "depends_on_task_ids": [],
                                "input_schema": {},
                                "output_schema": {},
                                "human_approval_required": False,
                                "framework_hints": {
                                    "preferred_adapter": None,
                                    "adapter_config": {},
                                    "metadata": {},
                                },
                                "metadata": {},
                            }
                        ],
                        "agent_definitions": [
                            {
                                "id": "repair-agent",
                                "name": "Repair Agent",
                                "instructions": "Execute the repaired workflow.",
                                "model_profile_id": "profile-fake",
                                "tool_ids": [],
                            }
                        ],
                        "versioning": {
                            "version": "1.0.0",
                            "revision": 1,
                            "parent_version": None,
                            "is_published": False,
                            "labels": [],
                        },
                        "metadata": {
                            "visible_to_main_agent": True,
                            "mutable_by_main_agent": True,
                        },
                    }
                },
                provider="fake",
                model=self.profile.model,
            )
        if schema_name == "workflow_builder_update_workflow":
            return ModelResponse(
                content={
                    "workflow": {
                        "id": "model-updated-workflow",
                        "name": "Tool Update Workflow",
                        "description": "Updated from a natural-language goal.",
                        "entrypoint": "node-1",
                        "nodes": [
                            {
                                "id": "node-1",
                                "name": "Entry",
                                "node_type": "task",
                                "task_id": "task-1",
                                "config": {},
                                "metadata": {},
                            }
                        ],
                        "task_definitions": [
                            {
                                "id": "task-1",
                                "name": "Task One",
                                "description": "Do the updated work",
                                "tool_ids": [],
                                "depends_on_task_ids": [],
                                "input_schema": {},
                                "output_schema": {},
                                "human_approval_required": False,
                                "framework_hints": {
                                    "preferred_adapter": None,
                                    "adapter_config": {},
                                    "metadata": {},
                                },
                                "metadata": {},
                            }
                        ],
                        "versioning": {
                            "version": "1.0.0",
                            "revision": 1,
                            "parent_version": None,
                            "is_published": False,
                            "labels": [],
                        },
                        "metadata": {
                            "visible_to_main_agent": True,
                            "mutable_by_main_agent": True,
                        },
                    }
                },
                provider="fake",
                model=self.profile.model,
            )
        return ModelResponse(content={"ok": True}, provider="fake", model=self.profile.model)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "unused"

    def count_tokens(self, messages, **kwargs):
        return 0

    def health_check(self):
        return {"ok": True}


class _CodexAuthRequiredModelClient:
    provider_key = "openai_codex"
    generate_calls = 0

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        type(self).generate_calls += 1
        raise AssertionError("Codex model call should be skipped when auth preflight fails")

    def health_check(self):
        return {
            "ok": False,
            "provider": "openai_codex",
            "model": self.profile.model,
            "status_code": 403,
            "error_code": "missing_model_request_scope",
            "auth_status": "missing_scope",
            "auth_required": True,
            "reauthorization_required": True,
            "auth_mode": "chatgpt",
            "auth_action": "device_authorize",
            "auth_endpoint": "/model-providers/openai-codex/device-authorize",
            "auth_profile_id": "default",
            "provider_id": "openai-codex",
        }


class MainAgentConversationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake", lambda profile, env: _FakeModelClient(profile, env))
        self.context.llm_provider_registry.register(
            "openai_codex",
            lambda profile, env: _CodexAuthRequiredModelClient(profile, env),
        )
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
        )
        self.service = ConversationService(self.context)
        self.setup_service = MainAgentSetupService(self.context)
        _FakeModelClient.last_system_message = None
        _FakeModelClient.responses = []
        _FakeModelClient.seen_messages = []
        _FakeModelClient.seen_tools = []
        _CodexAuthRequiredModelClient.generate_calls = 0

    async def asyncTearDown(self) -> None:
        reset_settings_cache()

    async def _create_computer_use_tool(self, tool_id: str, canonical_name: str) -> None:
        await self.context.tool_repo.create(
            ToolDefinition(
                id=tool_id,
                name=canonical_name,
                description=f"{canonical_name} computer use tool",
                tool_type=ToolType.MCP_TOOL,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                implementation=ToolImplementationReference(
                    implementation_type="mcp_tool",
                    target="computer-use-macos",
                    callable_name=canonical_name,
                    config={
                        "mcp_tool_name": canonical_name.title(),
                        "canonical_tool_name": canonical_name,
                        "tool_family": "computer_use",
                        "tool_platform": "macos",
                    },
                ),
                security=SecuritySettings(allowlisted_mcp_servers=["computer-use-macos"]),
                mcp_exposure=MCPExposureSettings(),
                tags=["mcp", "computer-use-macos", "computer_use", "macos"],
            )
        )

    async def _assign_agent_tool_ids(self, *tool_ids: str) -> None:
        profile = await self.context.main_agent_profile_repo.get("main-agent-profile")
        assert profile is not None
        agent = await self.context.agent_repo.get(profile.agent_id)
        assert agent is not None
        updated = list(agent.tool_ids)
        for tool_id in tool_ids:
            if tool_id not in updated:
                updated.append(tool_id)
        await self.context.agent_repo.update(agent.id, {"tool_ids": updated})

    def _workflow_payload(self, *, workflow_id: str, name: str, description: str = "Workflow description") -> dict:
        return {
            "id": workflow_id,
            "name": name,
            "description": description,
            "entrypoint": "node-1",
            "nodes": [
                {
                    "id": "node-1",
                    "name": "Entry",
                    "node_type": "task",
                    "task_id": "task-1",
                    "config": {},
                    "metadata": {},
                }
            ],
            "task_definitions": [
                {
                    "id": "task-1",
                    "name": "Task One",
                    "description": "Do the work",
                    "tool_ids": [],
                    "depends_on_task_ids": [],
                    "input_schema": {},
                    "output_schema": {},
                    "human_approval_required": False,
                    "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
                    "metadata": {},
                }
            ],
            "versioning": {
                "version": "1.0.0",
                "revision": 1,
                "parent_version": None,
                "is_published": False,
                "labels": [],
            },
            "metadata": {
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
            },
        }

    def _tool_payload(self, *, tool_id: str, name: str, description: str = "Tool description") -> dict:
        return {
            "id": tool_id,
            "name": name,
            "description": description,
            "tool_type": "python_function",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            "output_schema": {"type": "object"},
            "implementation": {
                "implementation_type": "python_function",
                "target": "tests.native_test_tools",
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
                "allowed_paths": [],
                "allowlisted_mcp_servers": [],
                "module_allowlist": ["tests.native_test_tools"],
                "function_allowlist": ["echo_tool"],
                "read_only_sql": True,
                "read_only": False,
                "dangerous": False,
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
            "tags": ["chat-created"],
            "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
        }

    async def test_create_main_agent_persists_agent_workflow_and_profile(self) -> None:
        first = await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )

        self.assertEqual(first.id, "main-agent-profile")
        profiles = await self.context.main_agent_profile_repo.list()
        agents = await self.context.agent_repo.list()
        workflows = await self.context.workflow_repo.list()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(len([item for item in agents if item.id == "main-agent"]), 1)
        self.assertEqual(len([item for item in workflows if item.id == "main-workflow"]), 1)

    async def test_missing_main_agent_setup_raises_clear_error(self) -> None:
        with self.assertRaises(MainAgentSetupRequiredError):
            await self.service.create_conversation(
                {
                    "id": "conversation-missing-setup",
                    "created_by_user_id": "user-1",
                    "channel_type": "api",
                }
            )

    async def test_post_message_generates_assistant_reply_and_title(self) -> None:
        await self._create_computer_use_tool("mcp:computer-use-macos:snapshot", "snapshot")
        await self._create_computer_use_tool("mcp:computer-use-macos:click", "click")
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Summarize my day",
                    "content": {"text": "Summarize my day"},
                },
                "response_mode": "sync",
            },
        )

        self.assertEqual(result["message"]["id"], "message-1")
        self.assertEqual(result["assistant_message"]["plain_text"], "Generated: Summarize my day")
        refreshed = await self.service.get_conversation(conversation.id)
        assert refreshed is not None
        self.assertEqual(refreshed.title, "Summarize my day")
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual([item["role"] for item in messages["items"]], ["user", "assistant"])
        self.assertIsNotNone(_FakeModelClient.last_system_message)
        self.assertIn("Computer Use Contract:", _FakeModelClient.last_system_message)
        self.assertIn("click, snapshot", _FakeModelClient.last_system_message)

    async def test_post_message_async_returns_before_main_agent_reply(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-async",
                "created_by_user_id": "user-1",
                "channel_type": "web",
            }
        )
        started = asyncio.Event()

        async def slow_complete(**kwargs):
            started.set()
            await asyncio.sleep(0.01)
            return {
                "message": kwargs["origin_message"].model_dump(mode="json"),
                "assistant_message": {"id": "assistant-later"},
            }

        with patch.object(ConversationService, "_complete_user_text_response", new=AsyncMock(side_effect=slow_complete)):
            result = await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-async",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Run the slow main agent path",
                        "content": {"text": "Run the slow main agent path"},
                    },
                    "response_mode": "async",
                },
            )

            self.assertEqual(result["message"]["id"], "message-async")
            self.assertNotIn("assistant_message", result)
            self.assertEqual(
                result["stream_url"],
                "/conversations/conversation-async/stream?after=message-async",
            )
            await asyncio.wait_for(started.wait(), timeout=1)

    async def test_main_agent_can_list_visible_workflows_through_assigned_tool(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-visible",
                name="Visible Workflow",
                description="Can be routed by the main agent.",
                entrypoint="manual",
                metadata={"visible_to_main_agent": True, "inputs": ["topic"]},
            )
        )
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-hidden",
                name="Hidden Workflow",
                description="Must not appear in the catalog.",
                entrypoint="manual",
                metadata={"visible_to_main_agent": False},
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[ModelToolCall(id="tool-call-list-workflows", name="ListWorkflows", arguments={})],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="I can run the visible workflow.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-catalog-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-catalog-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "What can you run?",
                    "content": {"text": "What can you run?"},
                },
            },
        )

        assert _FakeModelClient.last_system_message is not None
        self.assertNotIn("Main Agent Capability Catalog:", _FakeModelClient.last_system_message)
        tool_names = [
            tool["function"]["name"]
            for tool in (_FakeModelClient.seen_tools[-1] or [])
        ]
        self.assertIn("get_workflow", tool_names)
        self.assertIn("list_workflows", tool_names)
        self.assertIn("propose_workflow_create", tool_names)
        self.assertIn("propose_workflow_update", tool_names)
        self.assertIn("run_workflow", tool_names)
        self.assertIn("list_tools", tool_names)
        self.assertIn("get_tool", tool_names)
        self.assertIn("propose_tool_create", tool_names)
        self.assertIn("propose_tool_update", tool_names)
        self.assertIn("list_memories", tool_names)
        self.assertIn("remember_memory", tool_names)
        self.assertIn("update_memory", tool_names)
        self.assertIn("delete_memory", tool_names)
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            ["user_text", "tool_call", "tool_result", "assistant_text"],
        )
        result = messages["items"][2]["content"]["result"]
        workflow_ids = [item["id"] for item in result["workflows"]]
        self.assertIn("workflow-visible", workflow_ids)
        self.assertNotIn("workflow-hidden", workflow_ids)

    async def test_main_agent_injects_user_scoped_durable_memories(self) -> None:
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-user-1",
                scope="user",
                created_by_user_id="user-1",
                content="The user's timezone preference is Asia/Singapore.",
                summary="Timezone preference is Asia/Singapore.",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-user-2",
                scope="user",
                created_by_user_id="user-2",
                content="The user's timezone preference is America/New_York.",
                summary="Timezone preference is America/New_York.",
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-memory-user-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-memory-user-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "What timezone should you remember?",
                    "content": {"text": "What timezone should you remember?"},
                },
            },
        )

        assert _FakeModelClient.last_system_message is not None
        self.assertIn("Relevant durable memories", _FakeModelClient.last_system_message)
        self.assertIn("Asia/Singapore", _FakeModelClient.last_system_message)
        self.assertNotIn("America/New_York", _FakeModelClient.last_system_message)

    async def test_external_unmapped_channel_does_not_read_user_memory(self) -> None:
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-external-user",
                scope="user",
                created_by_user_id="user-1",
                content="The user's timezone preference is Asia/Singapore.",
                summary="Timezone preference is Asia/Singapore.",
            )
        )
        await self.context.channel_identity_mapping_repo.create(
            ChannelIdentityMapping(
                id="discord-untrusted",
                channel_type="discord",
                channel_user_id="discord-untrusted-user",
                internal_user_id="user-1",
                trusted=False,
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-memory-external-1",
                "channel_type": "discord",
                "channel_user_id": "discord-untrusted-user",
            }
        )

        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-memory-external-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "What timezone should you remember?",
                    "content": {"text": "What timezone should you remember?"},
                },
            },
        )

        assert _FakeModelClient.last_system_message is not None
        self.assertNotIn("Asia/Singapore", _FakeModelClient.last_system_message)

    async def test_main_agent_memory_tool_requires_confirmation_for_sensitive_write(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-remember-sensitive",
                        name="RememberMemory",
                        arguments={
                            "scope": "user",
                            "content": "The user's API key is sk-test",
                            "sensitive": True,
                            "confirmed": False,
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="I need confirmation before storing that.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-memory-tool-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-memory-tool-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Remember this API key.",
                    "content": {"text": "Remember this API key."},
                },
            },
        )

        messages = await self.service.list_messages(conversation.id)
        result = next(item for item in messages["items"] if item["message_type"] == "tool_result")
        self.assertEqual(result["content"]["result"]["status"], "needs_confirmation")
        self.assertEqual(await self.context.memory_repo.list(), [])

    async def test_main_agent_retrieval_v2_injects_layered_memory_and_excludes_sensitive(self) -> None:
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-decision-1",
                scope="conversation",
                conversation_id="conversation-memory-v2-1",
                content="Use the DB-backed memory system as the source of truth.",
                memory_kind="decision",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-commitment-1",
                scope="conversation",
                conversation_id="conversation-memory-v2-1",
                content="Finish the implementation spec before changing runtime behavior.",
                memory_kind="task_commitment",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-preference-1",
                scope="user",
                created_by_user_id="user-1",
                content="The user's timezone preference is Asia/Singapore.",
                summary="Timezone preference is Asia/Singapore.",
                memory_kind="preference",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-summary-1",
                scope="conversation",
                conversation_id="conversation-memory-v2-1",
                source_conversation_id="conversation-memory-v2-1",
                content="The day focused on designing the DB-first memory architecture.",
                summary="Locked the DB-first memory design.",
                memory_kind="daily_summary",
                summary_date="2026-05-08",
                archived_window_start="2026-05-08T00:00:00Z",
                archived_window_end="2026-05-08T23:59:59Z",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-sensitive-1",
                scope="user",
                created_by_user_id="user-1",
                content="The user's API key is sk-secret.",
                sensitive=True,
                memory_kind="fact",
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-memory-v2-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        with patch.dict(os.environ, {"MEMORY_RETRIEVAL_V2_ENABLED": "true"}, clear=False):
            reset_settings_cache()
            await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-memory-v2-1",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "What should you remember about the memory plan?",
                        "content": {"text": "What should you remember about the memory plan?"},
                    },
                },
            )
            reset_settings_cache()

        assert _FakeModelClient.last_system_message is not None
        self.assertIn("Relevant operational memory", _FakeModelClient.last_system_message)
        self.assertIn("Active decisions", _FakeModelClient.last_system_message)
        self.assertIn("Open commitments", _FakeModelClient.last_system_message)
        self.assertIn("Facts and preferences", _FakeModelClient.last_system_message)
        self.assertIn("Recent summaries", _FakeModelClient.last_system_message)
        self.assertIn("[decision:memory-decision-1]", _FakeModelClient.last_system_message)
        self.assertIn("[task_commitment:memory-commitment-1]", _FakeModelClient.last_system_message)
        self.assertIn("[preference:memory-preference-1]", _FakeModelClient.last_system_message)
        self.assertIn("[daily_summary:memory-summary-1][2026-05-08]", _FakeModelClient.last_system_message)
        self.assertNotIn("sk-secret", _FakeModelClient.last_system_message)

    async def test_main_agent_policy_hides_denied_workflows_and_tools(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-denied",
                name="Denied Workflow",
                description="Visible flag is overridden by deny metadata.",
                entrypoint="manual",
                metadata={"visible_to_main_agent": True, "hidden_from_main_agent": True},
            )
        )
        hidden_tool_payload = self._tool_payload(tool_id="tool-hidden", name="hidden_tool")
        hidden_tool_payload["framework_hints"]["metadata"] = {"hidden_from_main_agent": True}
        await self.context.tool_repo.create(ToolDefinition.model_validate(hidden_tool_payload))
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        await self._assign_agent_tool_ids("tool-hidden")
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[ModelToolCall(id="tool-call-list-workflows-denied", name="ListWorkflows", arguments={})],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="Denied items are hidden.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-policy-hidden-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-policy-hidden-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "What can you access?",
                    "content": {"text": "What can you access?"},
                },
            },
        )

        exposed_tool_names = [
            tool["function"]["name"]
            for tool in (_FakeModelClient.seen_tools[0] or [])
        ]
        self.assertNotIn("hidden_tool", exposed_tool_names)
        messages = await self.service.list_messages(conversation.id)
        result = next(item for item in messages["items"] if item["message_type"] == "tool_result")
        workflow_ids = [item["id"] for item in result["content"]["result"]["workflows"]]
        self.assertNotIn("workflow-denied", workflow_ids)

    async def test_create_main_agent_includes_visible_computer_use_tool_ids(self) -> None:
        await self._create_computer_use_tool("mcp:computer-use-macos:snapshot", "snapshot")
        await self._create_computer_use_tool("mcp:computer-use-macos:press_key", "press_key")
        created = await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        agent = await self.context.agent_repo.get(created.agent_id)
        assert agent is not None
        self.assertEqual(
            sorted(agent.tool_ids),
            [
                "agency.command.run",
                "agency.memory.delete",
                "agency.memory.list",
                "agency.memory.remember",
                "agency.memory.update",
                "agency.tool.get",
                "agency.tool.list",
                "agency.tool.propose-create",
                "agency.tool.propose-update",
                "agency.workflow.get",
                "agency.workflow.list",
                "agency.workflow.propose-create",
                "agency.workflow.propose-update",
                "agency.workflow.run",
                "mcp:computer-use-macos:press_key",
                "mcp:computer-use-macos:snapshot",
            ],
        )

    async def test_post_message_executes_safe_tool_calls_and_persists_tool_messages(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition(
                id="tool-echo",
                name="echo_tool",
                description="Echo text",
                tool_type=ToolType.PYTHON_FUNCTION,
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                output_schema={"type": "object"},
                implementation=ToolImplementationReference(
                    implementation_type="python_function",
                    target="tests.native_test_tools",
                    callable_name="echo_tool",
                ),
                security=SecuritySettings(requires_approval=False),
                mcp_exposure=MCPExposureSettings(),
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use tools when helpful.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        await self._assign_agent_tool_ids("tool-echo")
        _FakeModelClient.responses = [
            ModelResponse(
                content="Let me check that.",
                tool_calls=[ModelToolCall(id="tool-call-1", name="echo_tool", arguments={"text": "hello"})],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="Tool says hello.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Use the echo tool",
                    "content": {"text": "Use the echo tool"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["plain_text"], "Tool says hello.")
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            ["user_text", "tool_call", "tool_result", "assistant_text"],
        )
        self.assertEqual(messages["items"][1]["content"]["tool_name"], "echo_tool")
        self.assertEqual(messages["items"][2]["content"]["result"], {"echo": "hello"})
        self.assertEqual(
            _FakeModelClient.seen_messages[1][-1],
            ("tool", "{\"echo\":\"hello\"}", "echo_tool"),
        )

    async def test_main_agent_can_trigger_visible_workflow_from_tool_call(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-visible-run",
                name="Visible Run Workflow",
                description="Runnable workflow",
                entrypoint="manual",
                metadata={"visible_to_main_agent": True, "protected_execution": False},
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use workflows when helpful.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )

        async def fake_queue_start(execution_id: str):
            execution = await self.context.execution_store.get_execution(execution_id)
            assert execution is not None
            execution.status = ExecutionStatus.COMPLETED
            execution.output_payload = {"ok": True}
            await self.context.execution_store.update_execution(execution)
            return execution

        self.context.control_plane.queue_start = fake_queue_start
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-run-workflow",
                        name="RunWorkflow",
                        arguments={"workflow_id": "workflow-visible-run", "input_payload": {"topic": "demo"}},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="Workflow started.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-tool-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-tool-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run the visible workflow for demo",
                    "content": {"text": "Run the visible workflow for demo"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["plain_text"], "Workflow started.")
        executions = await self.context.execution_store.list_executions()
        linked = [item for item in executions if item.metadata.get("conversation_id") == conversation.id]
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0].input_payload, {"topic": "demo"})
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            [
                "user_text",
                "tool_call",
                "execution_started",
                "execution_completed",
                "tool_result",
                "assistant_text",
            ],
        )
        self.assertEqual(messages["items"][1]["content"]["tool_name"], "run_workflow")

    async def test_protected_workflow_tool_call_requests_approval_each_time(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-protected-tool",
                name="Protected Tool Workflow",
                description="Requires approval from workflow tool calls.",
                entrypoint="manual",
                metadata={"visible_to_main_agent": True, "protected_execution": True},
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use workflows when helpful.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-tool-protected-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-run-workflow-protected-1",
                        name="RunWorkflow",
                        arguments={"workflow_id": "workflow-protected-tool", "input_payload": {"topic": "one"}},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
        ]
        first = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-tool-protected-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run the protected workflow once",
                    "content": {"text": "Run the protected workflow once"},
                },
            },
        )
        self.assertEqual(first["approval_request"]["status"], "pending")

        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-run-workflow-protected-2",
                        name="RunWorkflow",
                        arguments={"workflow_id": "workflow-protected-tool", "input_payload": {"topic": "two"}},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
        ]
        second = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-tool-protected-2",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run the protected workflow again",
                    "content": {"text": "Run the protected workflow again"},
                },
            },
        )
        self.assertEqual(second["approval_request"]["status"], "pending")
        self.assertNotEqual(first["approval_request"]["id"], second["approval_request"]["id"])
        approvals = await self.service.list_approval_requests(conversation.id)
        self.assertEqual(len(approvals["items"]), 2)

    async def test_main_agent_can_get_visible_workflow_from_tool_call(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-visible-get",
                name="Visible Get Workflow",
                description="Visible workflow details",
                entrypoint="manual",
                metadata={"visible_to_main_agent": True},
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use workflows when helpful.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-get-workflow",
                        name="GetWorkflow",
                        arguments={"workflow_id": "workflow-visible-get"},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="I found the workflow.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-get-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-get-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Inspect workflow",
                    "content": {"text": "Inspect workflow"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["plain_text"], "I found the workflow.")
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(messages["items"][1]["content"]["tool_name"], "get_workflow")
        self.assertEqual(messages["items"][2]["content"]["result"]["status"], "ok")
        self.assertEqual(messages["items"][2]["content"]["result"]["workflow"]["id"], "workflow-visible-get")

    async def test_main_agent_can_propose_workflow_create_from_tool_call(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Create workflows when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-create",
                        name="ProposeNewWorkflow",
                        arguments={
                            "summary": "Create workflow 'Tool Created Workflow'.",
                            "workflow": self._workflow_payload(
                                workflow_id="workflow-tool-create",
                                name="Tool Created Workflow",
                            ),
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-create-tool-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-create-tool-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create a workflow",
                    "content": {"text": "Create a workflow"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["message_type"], "workflow_proposal")
        self.assertEqual(result["approval_request"]["approval_type"], "workflow_create")
        self.assertIsNone(await self.context.workflow_repo.get("workflow-tool-create"))
        approved = await self.service.approve_request(
            result["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Create it",
        )
        self.assertEqual(approved["workflow"]["id"], "workflow-tool-create")
        persisted = await self.context.workflow_repo.get("workflow-tool-create")
        assert persisted is not None
        self.assertEqual(persisted.metadata["created_by"], "user-1")
        self.assertEqual(persisted.metadata["owner_ids"], ["user-1"])
        self.assertEqual(persisted.metadata["provenance"]["approval_request_id"], result["approval_request"]["id"])
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            ["user_text", "tool_call", "workflow_proposal", "approval_result"],
        )

    async def test_workflow_mutation_kill_switch_blocks_main_agent_proposals(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Create workflows when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-kill-switch-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        with patch.dict("os.environ", {"MAIN_AGENT_WORKFLOW_MUTATION_ENABLED": "false"}):
            reset_settings_cache()
            result = await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-workflow-kill-switch-1",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Create a workflow",
                        "content": {
                            "workflow_proposal": {
                                "workflow": self._workflow_payload(
                                    workflow_id="workflow-kill-switch",
                                    name="Blocked Workflow",
                                )
                            }
                        },
                    },
                },
            )

        self.assertEqual(result["assistant_message"]["plain_text"], "Main-agent workflow mutation is disabled by policy.")
        self.assertNotIn("approval_request", result)
        self.assertIsNone(await self.context.workflow_repo.get("workflow-kill-switch"))

    async def test_main_agent_can_propose_workflow_create_from_goal_tool_call(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Create workflows when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-create-goal",
                        name="ProposeNewWorkflow",
                        arguments={
                            "summary": "Create a launch planning workflow.",
                            "goal": "Create a workflow that drafts a concise product launch plan.",
                            "conversation_history": "The user wants reusable launch planning.",
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-create-goal-tool-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-create-goal-tool-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create a launch planning workflow",
                    "content": {"text": "Create a launch planning workflow"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["message_type"], "workflow_proposal")
        proposed = result["approval_request"]["proposed_payload"]["workflow"]
        self.assertEqual(proposed["name"], "Launch Planning Workflow")
        self.assertEqual(proposed["agent_definitions"][0]["model_profile_id"], "profile-fake")
        self.assertEqual(proposed["task_definitions"][0]["agent_id"], proposed["agent_definitions"][0]["id"])
        self.assertEqual(proposed["metadata"]["generated_by"], "workflow_builder")

    async def test_main_agent_repairs_generated_workflow_create_payload_before_approval(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Create workflows when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        workflow_payload = self._workflow_payload(
            workflow_id="workflow-tool-repair",
            name="Repairable Workflow",
        )
        workflow_payload["entrypoint"] = "missing-node"
        workflow_payload["nodes"] = []
        workflow_payload["agent_definitions"] = [
            {
                "id": "workflow-tool-repair-agent-1",
                "name": "Repair Agent",
                "instructions": "Repair and execute.",
                "model_profile_id": "profile-fake",
                "tool_ids": [],
            }
        ]
        workflow_payload["task_definitions"][0]["agent_id"] = None
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-create-repair",
                        name="ProposeNewWorkflow",
                        arguments={"workflow": workflow_payload},
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-create-repair-tool-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-create-repair-tool-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create a repairable workflow",
                    "content": {"text": "Create a repairable workflow"},
                },
            },
        )

        approval = result["approval_request"]
        proposed = approval["proposed_payload"]["workflow"]
        self.assertEqual(result["assistant_message"]["message_type"], "workflow_proposal")
        self.assertTrue(approval["metadata"]["validation_repair"]["attempted"])
        self.assertEqual(approval["metadata"]["validation_repair"]["remaining_errors"], [])
        self.assertEqual(proposed["entrypoint"], "task-1-node")
        self.assertEqual(proposed["nodes"][0]["task_id"], "task-1")
        self.assertEqual(proposed["task_definitions"][0]["agent_id"], "workflow-tool-repair-agent-1")

    async def test_main_agent_uses_model_assisted_repair_when_structural_repair_is_not_enough(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Create workflows when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        workflow_payload = self._workflow_payload(
            workflow_id="workflow-model-repair",
            name="Model Repair Workflow",
        )
        workflow_payload["task_definitions"][0]["agent_id"] = "missing-agent"
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-create-model-repair",
                        name="ProposeNewWorkflow",
                        arguments={
                            "goal": "Create a workflow and repair missing agent references.",
                            "workflow": workflow_payload,
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-create-model-repair-tool-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-create-model-repair-tool-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create a workflow and fix any missing agent references",
                    "content": {"text": "Create a workflow and fix any missing agent references"},
                },
            },
        )

        approval = result["approval_request"]
        proposed = approval["proposed_payload"]["workflow"]
        self.assertEqual(result["assistant_message"]["message_type"], "workflow_proposal")
        self.assertTrue(approval["metadata"]["validation_repair"]["attempted"])
        self.assertTrue(approval["metadata"]["validation_repair"]["model_assisted"])
        self.assertEqual(approval["metadata"]["validation_repair"]["remaining_errors"], [])
        self.assertEqual(proposed["id"], "workflow-model-repair")
        self.assertEqual(proposed["task_definitions"][0]["agent_id"], "repair-agent")
        self.assertEqual(proposed["agent_definitions"][0]["id"], "repair-agent")

    async def test_main_agent_can_propose_workflow_update_from_tool_call(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-tool-update",
                name="Tool Update Workflow",
                description="Original description",
                entrypoint="node-1",
                metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
                versioning={
                    "version": "1.0.0",
                    "revision": 1,
                    "parent_version": None,
                    "is_published": True,
                    "labels": [],
                },
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Update workflows when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-update",
                        name="ProposeWorkflowUpdate",
                        arguments={
                            "workflow_id": "workflow-tool-update",
                            "summary": "Update workflow 'Tool Update Workflow'.",
                            "restart_active_executions": True,
                            "workflow": self._workflow_payload(
                                workflow_id="workflow-tool-update",
                                name="Tool Update Workflow",
                                description="Updated by tool",
                            ),
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-update-tool-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-update-tool-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update workflow",
                    "content": {"text": "Update workflow"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["message_type"], "workflow_update_proposal")
        self.assertEqual(result["approval_request"]["approval_type"], "workflow_update")
        self.assertTrue(result["approval_request"]["proposed_payload"]["restart_active_executions"])
        self.assertTrue(result["assistant_message"]["content"]["restart_active_executions"])
        self.assertIn("description changed", result["approval_request"]["diff_summary"])
        workflow = await self.context.workflow_repo.get("workflow-tool-update")
        assert workflow is not None
        self.assertEqual(workflow.description, "Original description")
        replace_mock = AsyncMock(return_value=["execution-replacement"])
        self.context.control_plane.replace_active_executions_for_workflow_revision = replace_mock
        approved = await self.service.approve_request(
            result["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Update it",
        )
        self.assertEqual(approved["workflow"]["versioning"]["revision"], 2)
        workflow = await self.context.workflow_repo.get("workflow-tool-update")
        assert workflow is not None
        self.assertEqual(workflow.description, "Updated by tool")
        self.assertEqual(workflow.metadata["created_by"], "user-1")
        self.assertEqual(workflow.metadata["owner_ids"], ["user-1"])
        self.assertEqual(workflow.metadata["provenance"]["approval_request_id"], result["approval_request"]["id"])
        replace_mock.assert_awaited_once_with(
            workflow_id="workflow-tool-update",
            previous_revision=1,
            replacement_revision=2,
            source="main_agent_workflow_update",
        )
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            ["user_text", "tool_call", "workflow_update_proposal", "approval_result"],
        )

    async def test_main_agent_can_propose_workflow_update_from_goal_tool_call(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-tool-update-goal",
                name="Tool Update Workflow",
                description="Original description",
                entrypoint="node-1",
                metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
                versioning={
                    "version": "1.0.0",
                    "revision": 1,
                    "parent_version": None,
                    "is_published": True,
                    "labels": [],
                },
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Update workflows when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-update-goal",
                        name="ProposeWorkflowUpdate",
                        arguments={
                            "workflow_id": "workflow-tool-update-goal",
                            "summary": "Update workflow 'Tool Update Workflow'.",
                            "goal": "Change the workflow description and task details for the new process.",
                            "conversation_history": "The user wants a more concrete workflow update.",
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-update-goal-tool-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-update-goal-tool-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update workflow from a goal",
                    "content": {"text": "Update workflow from a goal"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["message_type"], "workflow_update_proposal")
        self.assertEqual(result["approval_request"]["approval_type"], "workflow_update")
        self.assertIn("description changed", result["approval_request"]["diff_summary"])
        proposed = result["approval_request"]["proposed_payload"]["workflow"]
        self.assertEqual(proposed["id"], "workflow-tool-update-goal")
        self.assertEqual(proposed["description"], "Updated from a natural-language goal.")
        workflow = await self.context.workflow_repo.get("workflow-tool-update-goal")
        assert workflow is not None
        self.assertEqual(workflow.description, "Original description")

    async def test_workflow_update_goal_can_add_recommendation_to_code_pipeline(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-repo-improvements",
                name="Repo Improvements Workflow",
                description="Original description",
                entrypoint="node-1",
                nodes=[
                    {
                        "id": "node-1",
                        "name": "Review recommendations",
                        "node_type": "task",
                        "task_id": "task-1",
                    }
                ],
                task_definitions=[
                    {
                        "id": "task-1",
                        "name": "Review recommendations",
                        "description": "Collect improvement ideas from the repo scan.",
                        "agent_id": "agent-1",
                    }
                ],
                agent_definitions=[
                    {
                        "id": "agent-1",
                        "name": "Recommendation Agent",
                        "instructions": "Review the repository and propose improvements.",
                        "model_profile_id": "profile-fake",
                    }
                ],
                metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
                versioning={
                    "version": "1.0.0",
                    "revision": 1,
                    "parent_version": None,
                    "is_published": True,
                    "labels": [],
                },
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Update workflows when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-update-repo-improvements",
                        name="ProposeWorkflowUpdate",
                        arguments={
                            "workflow_id": "workflow-repo-improvements",
                            "summary": "Upgrade repo improvement workflow to apply coding fixes.",
                            "goal": (
                                "Enhance this workflow so recommendations for agency and agency-fe are implemented "
                                "as coding improvements, then verify the patch with tests."
                            ),
                            "conversation_history": "User wants recommendation output to drive actual code changes.",
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-update-repo-improvements",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-update-repo-improvements",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Enhance workflow",
                    "content": {"text": "Enhance workflow"},
                },
            },
        )

        proposed = result["approval_request"]["proposed_payload"]["workflow"]
        self.assertEqual(result["assistant_message"]["message_type"], "workflow_update_proposal")
        self.assertEqual(proposed["metadata"]["workflow_builder_enhancement"], "recommendation_to_code_pipeline")
        self.assertIn("agency.command.run", [item["id"] for item in proposed["tool_definitions"]])
        task_names = [item["name"].lower() for item in proposed["task_definitions"]]
        self.assertTrue(any("implement" in name for name in task_names))
        self.assertTrue(any("verify" in name for name in task_names))

    async def test_plain_text_workflow_update_goes_to_main_agent_llm(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-repo-improvements",
                name="Agency Repo Improvement Review",
                description="Looks for ideas and improvements in agency and agency-fe.",
                entrypoint="node-1",
                nodes=[
                    {
                        "id": "node-1",
                        "name": "Review recommendations",
                        "node_type": "task",
                        "task_id": "task-1",
                    }
                ],
                task_definitions=[
                    {
                        "id": "task-1",
                        "name": "Review recommendations",
                        "description": "Collect improvement ideas from the repo scan.",
                        "agent_id": "agent-1",
                    }
                ],
                agent_definitions=[
                    {
                        "id": "agent-1",
                        "name": "Recommendation Agent",
                        "instructions": "Review the repository and propose improvements.",
                        "model_profile_id": "profile-fake",
                    }
                ],
                metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
                versioning={
                    "version": "1.0.0",
                    "revision": 1,
                    "parent_version": None,
                    "is_published": True,
                    "labels": [],
                },
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Update workflows when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-plain-text-workflow-update",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        request_text = (
            "I have a workflow that is to look for ideas and improvements to the repo agency and agency-fe. "
            "I want to tap on the workflow and enhance the workflow to perform the coding improvements "
            "based on the recommendations output from the agent."
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-plain-text-workflow-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": request_text,
                    "content": {"text": request_text},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["message_type"], "assistant_text")
        self.assertTrue(result["assistant_message"]["plain_text"].startswith("Generated:"))
        self.assertNotIn("approval_request", result)
        self.assertEqual(len(_FakeModelClient.seen_messages), 1)
        self.assertEqual(_FakeModelClient.seen_messages[0][-1][1], request_text)

    async def test_plain_text_workflow_update_does_not_use_builder_before_llm(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-repo-improvements",
                name="Agency Repo Improvement Review",
                description="Looks for ideas and improvements in agency and agency-fe.",
                entrypoint="node-1",
                nodes=[
                    {
                        "id": "node-1",
                        "name": "Review recommendations",
                        "node_type": "task",
                        "task_id": "task-1",
                    }
                ],
                task_definitions=[
                    {
                        "id": "task-1",
                        "name": "Review recommendations",
                        "description": "Collect improvement ideas from the repo scan.",
                        "agent_id": "agent-1",
                    }
                ],
                agent_definitions=[
                    {
                        "id": "agent-1",
                        "name": "Recommendation Agent",
                        "instructions": "Review the repository and propose improvements.",
                        "model_profile_id": "profile-fake",
                    }
                ],
                metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
                versioning={
                    "version": "1.0.0",
                    "revision": 1,
                    "parent_version": None,
                    "is_published": True,
                    "labels": [],
                },
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Update workflows when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-plain-text-workflow-update-fallback",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        request_text = (
            "I have a workflow that is to look for ideas and improvements to the repo agency and agency-fe. "
            "I want to tap on the workflow and enhance the workflow to perform the coding improvements "
            "based on the recommendations output from the agent."
        )

        with patch.object(WorkflowBuilderService, "_generate_structured", side_effect=RuntimeError("missing scope")):
            result = await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-plain-text-workflow-update-fallback",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": request_text,
                        "content": {"text": request_text},
                    },
                },
            )

        self.assertEqual(result["assistant_message"]["message_type"], "assistant_text")
        self.assertNotIn("approval_request", result)
        self.assertEqual(len(_FakeModelClient.seen_messages), 1)

    async def test_coder_todo_follow_up_goes_to_main_agent_llm(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-repo-improvements",
                name="Agency Repo Improvement Review",
                description="Looks for ideas and improvements in agency and agency-fe.",
                entrypoint="node-1",
                nodes=[
                    {
                        "id": "node-1",
                        "name": "Review recommendations",
                        "node_type": "task",
                        "task_id": "task-1",
                    }
                ],
                task_definitions=[
                    {
                        "id": "task-1",
                        "name": "Review recommendations",
                        "description": "Collect improvement ideas from the repo scan.",
                        "agent_id": "agent-1",
                    }
                ],
                agent_definitions=[
                    {
                        "id": "agent-1",
                        "name": "Recommendation Agent",
                        "instructions": "Review the repository and propose improvements.",
                        "model_profile_id": "profile-fake",
                    },
                    {
                        "id": "coder",
                        "name": "Coder Agent",
                        "instructions": "Implement repository TODOs.",
                        "model_profile_id": "profile-fake",
                    },
                ],
                metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
                versioning={
                    "version": "1.0.0",
                    "revision": 1,
                    "parent_version": None,
                    "is_published": True,
                    "labels": [],
                },
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Update workflows when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-coder-todo-follow-up",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        request_text = "i want to have the coder agent to work on the workflow to perform the todo"

        with patch.object(WorkflowBuilderService, "_generate_structured", side_effect=RuntimeError("missing scope")):
            result = await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-coder-todo-follow-up",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": request_text,
                        "content": {"text": request_text},
                    },
                },
            )

        self.assertEqual(result["assistant_message"]["message_type"], "assistant_text")
        self.assertTrue(result["assistant_message"]["plain_text"].startswith("Generated:"))
        self.assertNotIn("approval_request", result)
        self.assertEqual(len(_FakeModelClient.seen_messages), 1)
        self.assertEqual(_FakeModelClient.seen_messages[0][-1][1], request_text)

    async def test_direct_reply_surfaces_codex_reauth_without_model_call(self) -> None:
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(
                id="profile-codex",
                name="Codex",
                provider="openai-codex",
                model="gpt-5.3-codex",
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Reply to the user.",
                model_profile_id="profile-codex",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-codex-auth-preflight",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-codex-auth-preflight",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "hello",
                    "content": {"text": "hello"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["message_type"], "assistant_text")
        self.assertIn("requires authorization", result["assistant_message"]["plain_text"])
        model_auth = result["assistant_message"]["metadata"]["model_auth"]
        self.assertEqual(model_auth["auth_status"], "missing_scope")
        self.assertTrue(model_auth["reauthorization_required"])
        self.assertEqual(model_auth["auth_action"], "device_authorize")
        self.assertEqual(_CodexAuthRequiredModelClient.generate_calls, 0)

    async def test_main_agent_can_list_tools_through_assigned_tool(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition.model_validate(
                self._tool_payload(tool_id="tool-visible", name="visible_tool")
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Inspect tools when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[ModelToolCall(id="tool-call-list-tools", name="ListTools", arguments={})],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="I found the tools.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-list-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-list-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "List available tools",
                    "content": {"text": "List available tools"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["plain_text"], "I found the tools.")
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(messages["items"][1]["content"]["tool_name"], "list_tools")
        tool_ids = [item["id"] for item in messages["items"][2]["content"]["result"]["tools"]]
        self.assertIn("tool-visible", tool_ids)

    async def test_main_agent_can_propose_tool_create_from_tool_call(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Create tools when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-tool-create",
                        name="ProposeNewTool",
                        arguments={
                            "summary": "Create tool 'Created Tool'.",
                            "tool": self._tool_payload(tool_id="tool-created", name="created_tool"),
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-create-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-create-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create a tool",
                    "content": {"text": "Create a tool"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["message_type"], "approval_request")
        self.assertEqual(result["approval_request"]["approval_type"], "tool_create")
        self.assertIsNone(await self.context.tool_repo.get("tool-created"))
        approved = await self.service.approve_request(
            result["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Create it",
        )
        self.assertEqual(approved["tool"]["id"], "tool-created")
        persisted = await self.context.tool_repo.get("tool-created")
        assert persisted is not None
        self.assertEqual(
            persisted.framework_hints.metadata["provenance"]["approval_request_id"],
            result["approval_request"]["id"],
        )
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            ["user_text", "tool_call", "approval_request", "approval_result"],
        )

    async def test_rejected_tool_create_stays_in_approval_history_only(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Create tools when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-tool-create-reject",
                        name="ProposeNewTool",
                        arguments={
                            "summary": "Create tool 'Rejected Tool'.",
                            "tool": self._tool_payload(tool_id="tool-rejected", name="rejected_tool"),
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-create-reject-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-create-reject-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create a tool but reject it",
                    "content": {"text": "Create a tool but reject it"},
                },
            },
        )

        rejected = await self.service.reject_request(
            result["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Not needed",
        )

        self.assertEqual(rejected["approval_request"]["status"], "rejected")
        self.assertIsNone(await self.context.tool_repo.get("tool-rejected"))
        approvals = await self.service.list_approval_requests(conversation.id)
        self.assertEqual(approvals["items"][0]["approval_type"], "tool_create")

    async def test_chat_tool_create_rejects_invalid_allowlist_policy_before_approval(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Create tools when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        invalid_tool = self._tool_payload(tool_id="tool-http-invalid", name="http_invalid_tool")
        invalid_tool.update(
            {
                "tool_type": "http_request",
                "implementation": {
                    "implementation_type": "http_request",
                    "target": "https://example.com",
                    "callable_name": None,
                    "config": {},
                },
            }
        )
        invalid_tool["security"]["allow_network"] = True
        invalid_tool["security"]["module_allowlist"] = []
        invalid_tool["security"]["function_allowlist"] = []
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-tool-invalid",
                        name="ProposeNewTool",
                        arguments={"tool": invalid_tool},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="I could not create that tool.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-create-invalid-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-create-invalid-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create an invalid HTTP tool",
                    "content": {"text": "Create an invalid HTTP tool"},
                },
            },
        )

        self.assertIsNone(await self.context.tool_repo.get("tool-http-invalid"))
        approvals = await self.service.list_approval_requests(conversation.id)
        self.assertEqual(approvals["items"], [])
        messages = await self.service.list_messages(conversation.id)
        tool_result = next(item for item in messages["items"] if item["message_type"] == "tool_result")
        self.assertIn("HTTP tools require allowlisted domains", tool_result["content"]["result"]["error"])

    async def test_chat_tool_create_rejects_reserved_system_tools_before_approval(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Create tools when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        reserved_tool = self._tool_payload(tool_id="agency.tool.hack", name="reserved_tool")
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-tool-reserved",
                        name="ProposeNewTool",
                        arguments={"tool": reserved_tool},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="I could not create that tool.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-create-reserved-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-create-reserved-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create a reserved system tool",
                    "content": {"text": "Create a reserved system tool"},
                },
            },
        )

        self.assertIsNone(await self.context.tool_repo.get("agency.tool.hack"))
        approvals = await self.service.list_approval_requests(conversation.id)
        self.assertEqual(approvals["items"], [])
        messages = await self.service.list_messages(conversation.id)
        tool_result = next(item for item in messages["items"] if item["message_type"] == "tool_result")
        self.assertIn("reserved system tool ids", tool_result["content"]["result"]["error"])

    async def test_chat_tool_create_rejects_embedded_secret_config_before_approval(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Create tools when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        secret_tool = self._tool_payload(tool_id="tool-secret-config", name="secret_config_tool")
        secret_tool["implementation"]["config"] = {
            "headers": {
                "Authorization": "Bearer should-not-be-stored",
            }
        }
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-tool-secret-config",
                        name="ProposeNewTool",
                        arguments={"tool": secret_tool},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="I could not create that tool.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-create-secret-config-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-create-secret-config-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create a tool with an embedded secret",
                    "content": {"text": "Create a tool with an embedded secret"},
                },
            },
        )

        self.assertIsNone(await self.context.tool_repo.get("tool-secret-config"))
        approvals = await self.service.list_approval_requests(conversation.id)
        self.assertEqual(approvals["items"], [])
        messages = await self.service.list_messages(conversation.id)
        tool_call = next(item for item in messages["items"] if item["message_type"] == "tool_call")
        tool_result = next(item for item in messages["items"] if item["message_type"] == "tool_result")
        self.assertEqual(
            tool_call["content"]["arguments"]["tool"]["implementation"]["config"]["headers"]["Authorization"],
            "[REDACTED]",
        )
        self.assertIn("embedded secrets", tool_result["content"]["result"]["error"])

    async def test_tool_mutation_kill_switch_blocks_main_agent_proposals(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Create tools when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-tool-kill-switch",
                        name="ProposeNewTool",
                        arguments={"tool": self._tool_payload(tool_id="tool-kill-switch", name="kill_switch_tool")},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="Tool mutation is blocked.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-kill-switch-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        with patch.dict("os.environ", {"MAIN_AGENT_TOOL_MUTATION_ENABLED": "false"}):
            reset_settings_cache()
            await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-tool-kill-switch-1",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Create a tool",
                        "content": {"text": "Create a tool"},
                    },
                },
            )

        self.assertIsNone(await self.context.tool_repo.get("tool-kill-switch"))
        approvals = await self.service.list_approval_requests(conversation.id)
        self.assertEqual(approvals["items"], [])
        messages = await self.service.list_messages(conversation.id)
        tool_result = next(item for item in messages["items"] if item["message_type"] == "tool_result")
        self.assertEqual(tool_result["content"]["result"]["error"], "Main-agent tool mutation is disabled by policy.")

    async def test_main_agent_can_propose_tool_update_from_tool_call(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition.model_validate(
                self._tool_payload(
                    tool_id="tool-update",
                    name="update_tool",
                    description="Original tool description",
                )
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Update tools when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        updated_payload = self._tool_payload(
            tool_id="tool-update",
            name="update_tool",
            description="Updated tool description",
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-tool-update",
                        name="ProposeToolUpdate",
                        arguments={
                            "tool_id": "tool-update",
                            "summary": "Update tool 'update_tool'.",
                            "tool": updated_payload,
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-update-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-update-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update a tool",
                    "content": {"text": "Update a tool"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["message_type"], "approval_request")
        self.assertEqual(result["approval_request"]["approval_type"], "tool_update")
        self.assertIn("description changed", result["approval_request"]["diff_summary"])
        before = await self.context.tool_repo.get("tool-update")
        assert before is not None
        self.assertEqual(before.description, "Original tool description")
        approved = await self.service.approve_request(
            result["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Update it",
        )
        self.assertEqual(approved["tool"]["description"], "Updated tool description")
        after = await self.context.tool_repo.get("tool-update")
        assert after is not None
        self.assertEqual(after.framework_hints.metadata["provenance"]["action"], "tool_update")

    async def test_direct_tool_results_are_redacted_in_conversation_messages(self) -> None:
        token_tool_payload = self._tool_payload(tool_id="tool-token", name="token_tool")
        token_tool_payload["implementation"]["callable_name"] = "token_tool"
        token_tool_payload["security"]["function_allowlist"] = ["token_tool"]
        await self.context.tool_repo.create(ToolDefinition.model_validate(token_tool_payload))
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use tools when helpful.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        await self._assign_agent_tool_ids("tool-token")
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[ModelToolCall(id="tool-call-token", name="token_tool", arguments={"text": "secret-token"})],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="Token tool complete.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-token-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-token-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run token tool",
                    "content": {"text": "Run token tool"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["plain_text"], "Token tool complete.")
        messages = await self.service.list_messages(conversation.id)
        tool_result = next(item for item in messages["items"] if item["message_type"] == "tool_result")
        self.assertEqual(tool_result["content"]["result"]["token"], "[REDACTED]")
        self.assertEqual(tool_result["content"]["result"]["nested"]["api_key"], "[REDACTED]")

    async def test_tool_results_are_replayed_into_later_direct_reply_turns(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition(
                id="tool-echo",
                name="echo_tool",
                description="Echo text",
                tool_type=ToolType.PYTHON_FUNCTION,
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                output_schema={"type": "object"},
                implementation=ToolImplementationReference(
                    implementation_type="python_function",
                    target="tests.native_test_tools",
                    callable_name="echo_tool",
                ),
                security=SecuritySettings(requires_approval=False),
                mcp_exposure=MCPExposureSettings(),
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use tools when helpful.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        await self._assign_agent_tool_ids("tool-echo")
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[ModelToolCall(id="tool-call-2", name="echo_tool", arguments={"text": "persist me"})],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="First turn complete.", provider="fake", model="fake-model"),
            ModelResponse(content="Second turn complete.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-2",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-2a",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run the tool",
                    "content": {"text": "Run the tool"},
                },
            },
        )
        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-2b",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "What happened before?",
                    "content": {"text": "What happened before?"},
                },
            },
        )

        replayed_tool_messages = [item for item in _FakeModelClient.seen_messages[-1] if item[0] == "tool"]
        self.assertEqual(replayed_tool_messages, [("tool", "{\"echo\":\"persist me\"}", "echo_tool")])

    async def test_post_message_creates_approval_request_for_approval_gated_tool(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition(
                id="tool-click",
                name="click",
                description="Computer use click",
                tool_type=ToolType.PYTHON_FUNCTION,
                input_schema={
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "token": {"type": "string"},
                    },
                    "required": ["x", "y"],
                },
                output_schema={"type": "object"},
                implementation=ToolImplementationReference(
                    implementation_type="python_function",
                    target="tests.native_test_tools",
                    callable_name="echo_tool",
                    config={"tool_family": "computer_use", "canonical_tool_name": "click"},
                ),
                security=SecuritySettings(requires_approval=True),
                mcp_exposure=MCPExposureSettings(),
                tags=["computer_use"],
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use tools when helpful.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        await self._assign_agent_tool_ids("tool-click")
        _FakeModelClient.responses = [
            ModelResponse(
                content="I should click that.",
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-approve-1",
                        name="click",
                        arguments={"x": 12, "y": 34, "token": "secret-click-token"},
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-approval-tool-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-approval-tool-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Click there",
                    "content": {"text": "Click there"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["message_type"], "approval_request")
        self.assertEqual(result["approval_request"]["approval_type"], "tool_execute")
        self.assertEqual(result["approval_request"]["status"], "pending")
        self.assertEqual(result["assistant_message"]["content"]["target"]["name"], "click")
        self.assertEqual(
            result["assistant_message"]["content"]["target"]["arguments"],
            {"x": 12, "y": 34, "token": "[REDACTED]"},
        )
        self.assertEqual(
            result["approval_request"]["proposed_payload"]["arguments"],
            {"x": 12, "y": 34, "token": "[REDACTED]"},
        )

    async def test_approving_tool_request_executes_tool_and_generates_follow_up(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition(
                id="tool-click",
                name="click",
                description="Computer use click",
                tool_type=ToolType.PYTHON_FUNCTION,
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                    },
                    "required": ["text"],
                },
                output_schema={"type": "object"},
                implementation=ToolImplementationReference(
                    implementation_type="python_function",
                    target="tests.native_test_tools",
                    callable_name="echo_tool",
                    config={"tool_family": "computer_use", "canonical_tool_name": "click"},
                ),
                security=SecuritySettings(requires_approval=True),
                mcp_exposure=MCPExposureSettings(),
                tags=["computer_use"],
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use tools when helpful.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        await self._assign_agent_tool_ids("tool-click")
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[ModelToolCall(id="tool-call-approve-2", name="click", arguments={"text": "clicked"})],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="Click completed.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-approval-tool-2",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        requested = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-approval-tool-2",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Click there",
                    "content": {"text": "Click there"},
                },
            },
        )

        approved = await self.service.approve_request(
            requested["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Approved",
        )

        self.assertEqual(approved["approval_request"]["status"], "approved")
        self.assertEqual(approved["message"]["message_type"], "approval_result")
        self.assertEqual(approved["tool_result"], {"echo": "clicked"})
        self.assertEqual(approved["tool_result_message"]["message_type"], "tool_result")
        self.assertEqual(approved["assistant_message"]["plain_text"], "Click completed.")

        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            ["user_text", "approval_request", "approval_result", "tool_call", "tool_result", "assistant_text"],
        )
