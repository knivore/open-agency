from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from app.core.config import reset_settings_cache
from app.graph.neo4j_read import GraphReadDocument, GraphReadEdge, GraphReadNode
from app.domain import (
    AgentDefinition,
    ChannelIdentityMapping,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    CredentialDefinition,
    Execution,
    ExecutionEvent,
    ExecutionEventType,
    MCPExposureSettings,
    ExecutionStatus,
    MemoryRecord,
    ModelProfileDefinition,
    PersonaStatus,
    SecuritySettings,
    ToolDefinition,
    ToolImplementationReference,
    ToolType,
    UserDefinition,
    WorkflowDefinition,
)
from app.llm.base import ModelResponse, ModelToolCall
from app.llm.registry import LLMEnvironmentConfig
from app.services.conversations.core import ConversationService
from app.services.agent_tools import (
    DEFAULT_MAIN_AGENT_SPEECH_TOOL_IDS,
    command_system_tool_ids,
    agent_management_system_tool_ids,
    connector_system_tool_ids,
    execution_system_tool_ids,
    goal_system_tool_ids,
    graph_system_tool_ids,
    memory_system_tool_ids,
    tool_management_system_tool_ids,
    workflow_system_tool_ids,
)
from app.services.main_agent_setup.service import (
    MainAgentSetupConfig,
    MainAgentSetupRequiredError,
    MainAgentSetupService,
)
from app.services.persona_factory import PersonaFactoryService
from app.services.workflow_builder import WorkflowBuilderService


class _FakeModelClient:
    provider_key = "fake"
    last_system_message: str | None = None
    responses: list[ModelResponse] = []
    seen_messages: list[list[tuple[str, object, str | None]]] = []
    seen_message_tool_calls: list[list[list[tuple[str | None, str]]]] = []
    seen_tools: list[list[dict] | None] = []

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        _FakeModelClient.seen_messages.append([(item.role, item.content, item.name) for item in messages])
        _FakeModelClient.seen_message_tool_calls.append(
            [[(tool_call.id, tool_call.name) for tool_call in item.tool_calls] for item in messages]
        )
        _FakeModelClient.seen_tools.append(kwargs.get("tools"))
        _FakeModelClient.last_system_message = next((item.content for item in messages if item.role == "system"), None)
        if _FakeModelClient.responses:
            return _FakeModelClient.responses.pop(0)
        latest_user = next((item.content for item in reversed(messages) if item.role == "user"), "hello")
        return ModelResponse(content=f"Generated: {latest_user}", provider="fake", model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        schema_name = kwargs.get("schema_name")
        if schema_name == "source_intelligence_classification":
            return ModelResponse(
                content={
                    "label": "decision",
                    "confidence": 0.9,
                    "signals": ["conversation_test_source"],
                    "document_kind": "workpaper",
                    "content_roles": ["decision", "domain_knowledge"],
                    "extraction_targets": ["decision_pattern", "domain_knowledge"],
                    "memory_layers": ["procedural", "semantic"],
                    "vector_tags": ["persona", "evidence"],
                    "graph_entities": [],
                    "graph_relationships": [],
                    "should_include": True,
                    "rationale": "The source describes a persona decision rule.",
                },
                provider="fake",
                model=self.profile.model,
            )
        if schema_name == "persona_llm_distillation_candidates":
            prompt = next((item.content for item in reversed(messages) if item.role == "user"), "{}")
            try:
                prompt_payload = json.loads(prompt) if isinstance(prompt, str) else {}
            except json.JSONDecodeError:
                prompt_payload = {}
            memory_payload = prompt_payload.get("memory") if isinstance(prompt_payload, dict) else {}
            source_title = (
                str(memory_payload.get("summary") or "Grade observations by evidence quality")
                if isinstance(memory_payload, dict)
                else "Grade observations by evidence quality"
            )
            source_content = (
                str(memory_payload.get("content") or "Grade observations by risk and evidence quality before escalating.")
                if isinstance(memory_payload, dict)
                else "Grade observations by risk and evidence quality before escalating."
            )
            return ModelResponse(
                content={
                    "candidates": [
                        {
                            "item_type": "decision_pattern",
                            "memory_layer": "procedural",
                            "title": source_title[:120],
                            "content": source_content[:1200],
                            "confidence": 0.86,
                            "source_evidence": "grades observations by risk and evidence quality",
                            "source_span": {"start": 0, "end": 52},
                            "review_reasons": ["source_backed"],
                            "structured_payload": {"rule": "Grade observations by risk and evidence quality."},
                            "inference_type": "extractive",
                        }
                    ]
                },
                provider="fake",
                model=self.profile.model,
            )
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


class _FakePersonaGraphReadService:
    def __init__(self):
        self.calls: list[dict] = []

    async def get_graph_preset(self, preset: str, **kwargs):
        self.calls.append({"preset": preset, **kwargs})
        return GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id=kwargs.get("persona_id") or "persona-1",
                    type="Persona",
                    labels=["Persona"],
                    properties={"name": "Audit Manager Persona"},
                ),
                GraphReadNode(
                    id="source-intelligence:workflow:audit-review",
                    type="Workflow",
                    labels=["Entity", "Workflow"],
                    properties={
                        "name": "Audit Review Workflow",
                        "evidence": "Reviewed graph hint from approved source intelligence.",
                    },
                ),
                GraphReadNode(
                    id="source-intelligence:artifact:mlp-observation",
                    type="Artifact",
                    labels=["Entity", "Artifact"],
                    properties={"name": "MLP Observation"},
                ),
            ],
            edges=[
                GraphReadEdge(
                    id="edge-persona-workflow",
                    source=kwargs.get("persona_id") or "persona-1",
                    target="source-intelligence:workflow:audit-review",
                    type="MENTIONS",
                ),
                GraphReadEdge(
                    id="edge-workflow-artifact",
                    source="source-intelligence:workflow:audit-review",
                    target="source-intelligence:artifact:mlp-observation",
                    type="PRODUCES",
                ),
            ],
            meta={"source": "fake-persona-graph"},
        )


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
        _FakeModelClient.seen_message_tool_calls = []
        _FakeModelClient.seen_tools = []
        _CodexAuthRequiredModelClient.generate_calls = 0

    async def asyncTearDown(self) -> None:
        reset_settings_cache()

    async def test_channel_context_prompt_and_approval_metadata_cover_chat_channels(self) -> None:
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
                "id": "conversation-channel-context",
                "created_by_user_id": "user-1",
                "channel_type": "discord",
                "channel_thread_id": "discord-thread-1",
                "channel_user_id": "discord-user-1",
            }
        )
        origin_message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-channel-context",
                conversation_id=conversation.id,
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text="What can you do here?",
                content={"text": "What can you do here?"},
                metadata={
                    "channel_context": {
                        "channel_type": "discord",
                        "thread_id": "discord-thread-1",
                        "user_id": "discord-user-1",
                        "display_name": "Discord User",
                        "guild_id": "guild-1",
                    }
                },
            )
        )

        prompt = self.service._channel_context_prompt(origin_message)
        self.assertIsNotNone(prompt)
        self.assertIn("Current Chat Channel Context:", prompt)
        self.assertIn('"channel_type":"discord"', prompt)
        self.assertIn('"thread_id":"discord-thread-1"', prompt)
        self.assertIn('"user_id":"discord-user-1"', prompt)
        self.assertIn("this workflow", prompt)
        self.assertIn("workflow_id", prompt)
        self.assertIn("ask for the missing identifier first", prompt)

        approval_metadata = await self.service._approval_origin_metadata(origin_message.id)
        self.assertEqual(approval_metadata["source"], "chat_channel")
        self.assertEqual(approval_metadata["source_channel_type"], "discord")
        self.assertEqual(approval_metadata["source_channel_context"]["thread_id"], "discord-thread-1")

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

    async def _publish_minimal_persona(
            self,
            *,
            user_id: str,
            persona_name: str,
            memory_id: str,
    ) -> dict:
        user = await self.context.user_repo.create(
            UserDefinition(id=user_id, email=f"{user_id}@example.com", display_name=user_id)
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id=memory_id,
                scope="user",
                created_by_user_id=user.id,
                content=f"{persona_name} grades observations by risk and evidence quality.",
                summary=f"{persona_name} grading rule",
                memory_type="decision",
                tags=["persona-source"],
            )
        )
        distill = await PersonaFactoryService(self.context).distill_from_memories(
            persona_id=None,
            name=persona_name,
            description="Reviews observations.",
            source_memory_ids=[memory_id],
            model_profile_id=None,
            current_user=user,
        )
        item_id = distill["items"][0]["id"]
        await PersonaFactoryService(self.context).approve_item(item_id)
        await PersonaFactoryService(self.context).synthesize_package_from_items(distill["run"]["id"])
        await PersonaFactoryService(self.context).approve_run(distill["run"]["id"], current_user=user)
        return await PersonaFactoryService(self.context).publish_run(distill["run"]["id"], current_user=user)

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

    async def test_run_page_self_heals_stale_execution_tool_access_and_reads_failure_evidence(self) -> None:
        profile = await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Diagnose runs from execution evidence.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        agent = await self.context.agent_repo.get(profile.agent_id)
        assert agent is not None
        await self.context.agent_repo.update(
            agent.id,
            {"tool_ids": [tool_id for tool_id in agent.tool_ids if not tool_id.startswith("agency.execution.")]},
        )

        execution = Execution(
            id="execution-run-diagnostic",
            workflow_id="workflow-news",
            runtime_adapter_id="native",
            status=ExecutionStatus.FAILED,
            input_payload={},
            error="host is not allowlisted: feeds.example.test",
            created_by="user-1",
        )
        await self.context.execution_store.save_execution(execution)
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                task_id="task-research",
                event_type=ExecutionEventType.TOOL_CALL_FAILED,
                sequence=1,
                status="failed",
                payload={
                    "tool_id": "agency.http.request",
                    "error": "host is not allowlisted: feeds.example.test",
                },
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-run-get",
                        name="get_execution",
                        arguments={"execution_id": execution.id},
                    ),
                    ModelToolCall(
                        id="tool-call-run-events",
                        name="list_execution_events",
                        arguments={"execution_id": execution.id},
                    ),
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(
                content="The first actionable error is that feeds.example.test is not allowlisted.",
                provider="fake",
                model="fake-model",
            ),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-run-diagnostic",
                "created_by_user_id": "user-1",
                "channel_type": "web",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-run-diagnostic",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Explain why this run failed using the first actionable error and evidence.",
                    "content": {"text": "Explain why this run failed using the first actionable error and evidence."},
                    "metadata": {
                        "page_context": {
                            "surface": "runs.detail",
                            "selection": {"runId": execution.id},
                            "entities": [{"type": "run", "id": execution.id, "label": "Selected run"}],
                        },
                        "assistant_providers": {
                            "version": "2026-05-27",
                            "providers": [
                                {
                                    "id": "execution.provider",
                                    "systemToolIds": [
                                        "agency.execution.get",
                                        "agency.execution.events",
                                    ],
                                }
                            ],
                        },
                    },
                },
            },
        )

        self.assertIn("feeds.example.test is not allowlisted", result["assistant_message"]["plain_text"])
        refreshed_agent = await self.context.agent_repo.get(profile.agent_id)
        assert refreshed_agent is not None
        self.assertIn("agency.execution.get", refreshed_agent.tool_ids)
        self.assertIn("agency.execution.events", refreshed_agent.tool_ids)
        messages = (await self.service.list_messages(conversation.id))["items"]
        tool_results = [item for item in messages if item["message_type"] == "tool_result"]
        self.assertEqual(len(tool_results), 2)
        self.assertEqual(tool_results[0]["content"]["result"]["execution"]["error"], execution.error)
        self.assertEqual(
            tool_results[1]["content"]["result"]["items"][0]["payload"]["error"],
            execution.error,
        )

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

    async def test_post_message_returns_exact_greetings_without_calling_the_model(self) -> None:
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
                "id": "conversation-instant-greeting",
                "created_by_user_id": "user-1",
                "channel_type": "web",
            }
        )

        with patch.dict(
            os.environ,
            {
                "MAIN_AGENT_ROUTER_ENABLED": "false",
                "MAIN_AGENT_ROUTER_FAST_PATH_ENABLED": "true",
                "MAIN_AGENT_ROUTER_FAST_PATH_RULES": "greeting",
            },
        ):
            reset_settings_cache()
            result = await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-instant-greeting",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "hi",
                        "content": {"text": "hi"},
                    },
                    "response_mode": "sync",
                },
            )

        self.assertEqual(result["assistant_message"]["plain_text"], "Hi — how can I help?")
        self.assertEqual(result["assistant_message"]["metadata"]["fast_path_rule"], "greeting")
        self.assertEqual(_FakeModelClient.seen_messages, [])
        refreshed = await self.service.get_conversation(conversation.id)
        assert refreshed is not None
        self.assertEqual(refreshed.title, "hi")

    async def test_post_message_invokes_published_persona_by_mention(self) -> None:
        user = await self.context.user_repo.create(
            UserDefinition(id="persona-user", email="persona@example.com", display_name="Persona User")
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
        await self.context.memory_repo.create(
            MemoryRecord(
                id="persona-source-memory",
                scope="user",
                created_by_user_id=user.id,
                content="Audit Manager Persona grades observations by risk, evidence quality, and management impact.",
                summary="Audit observation grading rule",
                memory_type="decision",
                tags=["persona-source"],
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="persona-rejected-memory",
                scope="user",
                created_by_user_id=user.id,
                content="Audit review workflow starts with planning, testing, and issue validation.",
                summary="Rejected audit workflow note",
                memory_type="fact",
                tags=["persona-source"],
            )
        )
        distill = await PersonaFactoryService(self.context).distill_from_memories(
            persona_id=None,
            name="Audit Manager Persona",
            description="Reviews audit observations.",
            source_memory_ids=["persona-source-memory", "persona-rejected-memory"],
            model_profile_id=None,
            current_user=user,
        )
        run_id = distill["run"]["id"]
        decision_item = next(item for item in distill["items"] if item["source_memory_id"] == "persona-source-memory")
        rejected_item = next(item for item in distill["items"] if item["source_memory_id"] == "persona-rejected-memory")
        await PersonaFactoryService(self.context).approve_item(decision_item["id"])
        await PersonaFactoryService(self.context).reject_item(rejected_item["id"], reason="Not enough source support.")
        await PersonaFactoryService(self.context).synthesize_package_from_items(run_id)
        await PersonaFactoryService(self.context).approve_run(run_id, current_user=user)
        await PersonaFactoryService(self.context).publish_run(run_id, current_user=user)

        conversation = await self.service.create_conversation(
            {
                "id": "conversation-persona",
                "created_by_user_id": user.id,
                "channel_type": "api",
            }
        )
        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-persona",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "@audit-manager-persona review this observation",
                    "content": {"text": "@audit-manager-persona review this observation"},
                },
                "response_mode": "sync",
            },
        )

        self.assertEqual(result["persona"]["slug"], "audit-manager-persona")
        self.assertEqual(result["assistant_message"]["metadata"]["delivery"], "persona")
        self.assertEqual(result["assistant_message"]["metadata"]["persona_slug"], "audit-manager-persona")
        provenance = result["assistant_message"]["metadata"]["persona_provenance"]
        self.assertEqual(provenance["package_strategy"], "item-synthesis-v1")
        self.assertIn("persona-source-memory", provenance["source_memory_ids"])
        self.assertTrue(provenance["distillation_item_ids"])
        self.assertTrue(provenance["source_refs"])
        runtime_context = provenance["runtime_context"]
        self.assertTrue(runtime_context["used_vector_memory"])
        self.assertFalse(runtime_context["used_graph_context"])
        self.assertEqual(runtime_context["vector_memory"]["source"], "approved_persona_memory")
        self.assertTrue(runtime_context["vector_memory"]["approved_persona_memory_used"])
        self.assertFalse(runtime_context["vector_memory"]["raw_source_fallback_used"])
        self.assertIn("@audit-manager-persona", _FakeModelClient.last_system_message or "")
        self.assertIn("Audit observation grading rule", _FakeModelClient.last_system_message or "")
        self.assertNotIn("Rejected audit workflow note", _FakeModelClient.last_system_message or "")
        projection_events = await self.context.graph_projection_event_repo.list_events(limit=200)
        self.assertTrue(
            any(
                event.event_type == "persona.runtime.invoked"
                and event.aggregate_type == "persona"
                and event.aggregate_id == result["persona"]["id"]
                for event in projection_events
            )
        )

    async def test_post_message_includes_persona_graph_context_when_enabled(self) -> None:
        with patch.dict(os.environ, {"GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED": "true"}, clear=False):
            reset_settings_cache()
            self.context.graph_read_service = _FakePersonaGraphReadService()
            await self.setup_service.create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_description="Configured for tests.",
                    agent_instructions="Answer briefly.",
                    model_profile_id="profile-fake",
                    profile_id="main-agent-profile",
                )
            )
            published = await self._publish_minimal_persona(
                user_id="persona-graph-user",
                persona_name="Graph Persona",
                memory_id="persona-graph-source-memory",
            )
            conversation = await self.service.create_conversation(
                {
                    "id": "conversation-persona-graph",
                    "created_by_user_id": "persona-graph-user",
                    "channel_type": "api",
                }
            )

            result = await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-persona-graph",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "@graph-persona use graph context",
                        "content": {"text": "@graph-persona use graph context"},
                    },
                    "response_mode": "sync",
                },
            )

        self.assertEqual(result["persona"]["id"], published["persona"]["id"])
        runtime_context = result["assistant_message"]["metadata"]["persona_provenance"]["runtime_context"]
        self.assertTrue(runtime_context["used_graph_context"])
        self.assertEqual(runtime_context["graph_context"]["status"], "used")
        self.assertEqual(runtime_context["graph_context"]["node_count"], 3)
        self.assertEqual(runtime_context["graph_context"]["edge_count"], 2)
        self.assertEqual(runtime_context["graph_context"]["policy"]["invocation_type"], "persona_runtime")
        self.assertEqual(runtime_context["graph_context"]["policy"]["preset"], "persona_lineage")
        self.assertEqual(runtime_context["graph_context"]["policy"]["fallback"], "skip_graph_context_without_failing_invocation")
        self.assertIn("# Persona Graph Context", _FakeModelClient.last_system_message or "")
        self.assertIn("Policy: preset=persona_lineage", _FakeModelClient.last_system_message or "")
        self.assertIn("Audit Review Workflow", _FakeModelClient.last_system_message or "")
        self.assertIn("PRODUCES", _FakeModelClient.last_system_message or "")
        self.assertEqual(self.context.graph_read_service.calls[0]["preset"], "persona_lineage")
        self.assertEqual(self.context.graph_read_service.calls[0]["persona_id"], published["persona"]["id"])

    async def test_post_message_invokes_explicit_published_persona_version(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        published_v1 = await self._publish_minimal_persona(
            user_id="persona-version-user",
            persona_name="Versioned Persona",
            memory_id="persona-version-source-memory",
        )
        user = await self.context.user_repo.get("persona-version-user")
        self.assertIsNotNone(user)
        factory = PersonaFactoryService(self.context)
        feedback = await factory.capture_feedback(
            persona_id=published_v1["persona"]["id"],
            title="Updated grading rule",
            content="Versioned Persona now also considers management response urgency.",
            item_type="decision_pattern",
            memory_layer="procedural",
            feedback_type="accepted_edit",
            confidence=0.8,
            source_memory_id=None,
            accepted_edit_of_item_id=None,
            source_conversation_id=None,
            source_message_id=None,
            source_run_id=None,
            metadata={},
            current_user=user,
        )
        await factory.approve_item(feedback["items"][0]["id"])
        await factory.synthesize_package_from_items(feedback["run"]["id"])
        approved_v2 = await factory.approve_run(feedback["run"]["id"], current_user=user)
        self.assertEqual(approved_v2["persona_version"]["version"], "1.0.1")
        await factory.publish_run(feedback["run"]["id"], current_user=user)

        conversation = await self.service.create_conversation(
            {
                "id": "conversation-persona-version",
                "created_by_user_id": user.id,
                "channel_type": "api",
            }
        )
        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-persona-version",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "@versioned-persona:1.0.0 review this observation",
                    "content": {"text": "@versioned-persona:1.0.0 review this observation"},
                },
                "response_mode": "sync",
            },
        )

        self.assertEqual(result["persona"]["slug"], "versioned-persona")
        self.assertEqual(result["persona_version"]["id"], published_v1["persona_version"]["id"])
        self.assertEqual(result["persona_version"]["version"], "1.0.0")
        self.assertEqual(result["assistant_message"]["metadata"]["persona_version_target"], "1.0.0")
        self.assertIn("Persona package version: 1.0.0", _FakeModelClient.last_system_message or "")

    async def test_post_message_excludes_sensitive_persona_memory(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        published = await self._publish_minimal_persona(
            user_id="persona-sensitive-user",
            persona_name="Sensitive Memory Persona",
            memory_id="sensitive-persona-source-memory",
        )
        persona = published["persona"]
        persona_version = published["persona_version"]
        await self.context.memory_repo.create(
            MemoryRecord(
                id="persona-sensitive-published-memory",
                scope="user",
                created_by_user_id="persona-sensitive-user",
                source="persona_factory",
                content="Sensitive private family detail that must not enter the persona prompt.",
                summary="Sensitive private family detail",
                memory_type="fact",
                tags=[f"persona:{persona['slug']}"],
                sensitive=True,
                metadata={
                    "persona_id": persona["id"],
                    "persona_version_id": persona_version["id"],
                    "distillation_item_id": "approved-sensitive-item",
                    "review_status": "approved",
                    "needs_review": False,
                    "memory_layer": "semantic",
                    "item_type": "domain_knowledge",
                },
            )
        )

        conversation = await self.service.create_conversation(
            {
                "id": "conversation-persona-sensitive",
                "created_by_user_id": "persona-sensitive-user",
                "channel_type": "api",
            }
        )
        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-persona-sensitive",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "@sensitive-memory-persona review this observation",
                    "content": {"text": "@sensitive-memory-persona review this observation"},
                },
                "response_mode": "sync",
            },
        )

        self.assertEqual(result["persona"]["slug"], "sensitive-memory-persona")
        self.assertIn("Sensitive Memory Persona grading rule", _FakeModelClient.last_system_message or "")
        self.assertNotIn("Sensitive private family detail", _FakeModelClient.last_system_message or "")

    async def test_post_message_renders_persona_document_memory_with_source_context(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        published = await self._publish_minimal_persona(
            user_id="persona-document-user",
            persona_name="Document Memory Persona",
            memory_id="document-persona-source-memory",
        )
        persona = published["persona"]
        persona_version = published["persona_version"]
        await self.context.memory_repo.create(
            MemoryRecord(
                id="persona-document-published-memory",
                scope="user",
                created_by_user_id="persona-document-user",
                source="persona_factory",
                content="Document chunk evidence " + ("detail " * 260),
                summary="Document evidence chunk summary",
                memory_type="fact",
                tags=[f"persona:{persona['slug']}"],
                metadata={
                    "persona_id": persona["id"],
                    "persona_version_id": persona_version["id"],
                    "distillation_item_id": "approved-document-item",
                    "review_status": "approved",
                    "needs_review": False,
                    "memory_layer": "semantic",
                    "item_type": "domain_knowledge",
                    "filename": "audit-evidence.md",
                    "chunk_index": 2,
                    "chunk_count": 5,
                },
            )
        )

        conversation = await self.service.create_conversation(
            {
                "id": "conversation-persona-document",
                "created_by_user_id": "persona-document-user",
                "channel_type": "api",
            }
        )
        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-persona-document",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "@document-memory-persona use the evidence",
                    "content": {"text": "@document-memory-persona use the evidence"},
                },
                "response_mode": "sync",
            },
        )

        system_message = _FakeModelClient.last_system_message or ""
        self.assertIn("audit-evidence.md", system_message)
        self.assertIn("chunk 3/5", system_message)
        self.assertIn("Document chunk evidence", system_message)
        self.assertIn("[truncated]", system_message)

    async def test_post_message_applies_persona_memory_layer_filter(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        published = await self._publish_minimal_persona(
            user_id="persona-layer-user",
            persona_name="Layer Filter Persona",
            memory_id="layer-persona-source-memory",
        )
        persona = published["persona"]
        persona_version = published["persona_version"]
        version = await self.context.persona_version_repo.get(persona_version["id"])
        assert version is not None
        package = dict(version.package)
        package.setdefault("runtime", {})["memory_layer_filter"] = ["tool"]
        await self.context.persona_version_repo.save(version.model_copy(update={"package": package}))
        for memory_id, layer, content in (
            ("persona-layer-semantic-memory", "semantic", "Semantic-only persona memory should be filtered out."),
            ("persona-layer-tool-memory", "tool", "Tool-layer persona memory should be injected."),
        ):
            await self.context.memory_repo.create(
                MemoryRecord(
                    id=memory_id,
                    scope="user",
                    created_by_user_id="persona-layer-user",
                    source="persona_factory",
                    content=content,
                    summary=content,
                    memory_type="fact",
                    tags=[f"persona:{persona['slug']}"],
                    metadata={
                        "persona_id": persona["id"],
                        "persona_version_id": persona_version["id"],
                        "distillation_item_id": memory_id,
                        "review_status": "approved",
                        "needs_review": False,
                        "memory_layer": layer,
                        "item_type": "domain_knowledge",
                    },
                )
            )

        conversation = await self.service.create_conversation(
            {
                "id": "conversation-persona-layer-filter",
                "created_by_user_id": "persona-layer-user",
                "channel_type": "api",
            }
        )
        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-persona-layer-filter",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "@layer-filter-persona use tool memory",
                    "content": {"text": "@layer-filter-persona use tool memory"},
                },
                "response_mode": "sync",
            },
        )

        system_message = _FakeModelClient.last_system_message or ""
        self.assertIn("Tool-layer persona memory should be injected.", system_message)
        self.assertNotIn("Semantic-only persona memory should be filtered out.", system_message)

    async def test_post_message_uses_raw_source_memory_as_fallback_evidence(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        published = await self._publish_minimal_persona(
            user_id="persona-fallback-user",
            persona_name="Fallback Evidence Persona",
            memory_id="fallback-persona-source-memory",
        )
        for memory_id in published["memory_ids"]:
            self.context.memory_repo._items.pop(memory_id, None)

        conversation = await self.service.create_conversation(
            {
                "id": "conversation-persona-fallback",
                "created_by_user_id": "persona-fallback-user",
                "channel_type": "api",
            }
        )
        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-persona-fallback",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "@fallback-evidence-persona use fallback evidence",
                    "content": {"text": "@fallback-evidence-persona use fallback evidence"},
                },
                "response_mode": "sync",
            },
        )

        system_message = _FakeModelClient.last_system_message or ""
        self.assertIn("Fallback Evidence Persona grading rule", system_message)
        self.assertIn("Fallback Evidence Persona grades observations by risk", system_message)
        runtime_context = result["assistant_message"]["metadata"]["persona_provenance"]["runtime_context"]
        self.assertTrue(runtime_context["used_vector_memory"])
        self.assertEqual(runtime_context["vector_memory"]["source"], "raw_source_memory_fallback")
        self.assertFalse(runtime_context["vector_memory"]["approved_persona_memory_used"])
        self.assertTrue(runtime_context["vector_memory"]["raw_source_fallback_used"])
        self.assertIn("fallback-persona-source-memory", runtime_context["vector_memory"]["memory_ids"])

    async def test_post_message_reports_unknown_persona_mention(self) -> None:
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
                "id": "conversation-unknown-persona",
                "created_by_user_id": "persona-user",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-unknown-persona",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "@missing-persona help",
                    "content": {"text": "@missing-persona help"},
                },
                "response_mode": "sync",
            },
        )

        self.assertEqual(result["assistant_message"]["metadata"]["delivery"], "persona")
        self.assertEqual(result["assistant_message"]["metadata"]["persona_error"], "not_found")
        self.assertIn("could not find", result["assistant_message"]["plain_text"])
        self.assertIsNone(_FakeModelClient.last_system_message)

    async def test_post_message_reports_unpublished_persona_mention(self) -> None:
        user = await self.context.user_repo.create(
            UserDefinition(id="draft-persona-user", email="draft@example.com", display_name="Draft User")
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
        await self.context.memory_repo.create(
            MemoryRecord(
                id="draft-persona-source-memory",
                scope="user",
                created_by_user_id=user.id,
                content="Draft persona uses concise responses.",
                summary="Draft persona response style",
                memory_type="preference",
                tags=["persona-source"],
            )
        )
        await PersonaFactoryService(self.context).distill_from_memories(
            persona_id=None,
            name="Draft Persona",
            description="Not published yet.",
            source_memory_ids=["draft-persona-source-memory"],
            model_profile_id=None,
            current_user=user,
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-draft-persona",
                "created_by_user_id": user.id,
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-draft-persona",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "@draft-persona help",
                    "content": {"text": "@draft-persona help"},
                },
                "response_mode": "sync",
            },
        )

        self.assertEqual(result["assistant_message"]["metadata"]["delivery"], "persona")
        self.assertEqual(result["assistant_message"]["metadata"]["persona_error"], "not_published")
        self.assertIn("not published", result["assistant_message"]["plain_text"])
        self.assertIsNone(_FakeModelClient.last_system_message)

    async def test_post_message_reports_archived_persona_mention(self) -> None:
        user = await self.context.user_repo.create(
            UserDefinition(id="archived-persona-user", email="archived@example.com", display_name="Archived User")
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
        await self.context.memory_repo.create(
            MemoryRecord(
                id="archived-persona-source-memory",
                scope="user",
                created_by_user_id=user.id,
                content="Archived persona uses concise responses.",
                summary="Archived persona response style",
                memory_type="preference",
                tags=["persona-source"],
            )
        )
        distill = await PersonaFactoryService(self.context).distill_from_memories(
            persona_id=None,
            name="Archived Persona",
            description="Archived before publishing.",
            source_memory_ids=["archived-persona-source-memory"],
            model_profile_id=None,
            current_user=user,
        )
        await self.context.persona_repo.update(distill["persona"]["id"], {"status": PersonaStatus.ARCHIVED.value})
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-archived-persona",
                "created_by_user_id": user.id,
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-archived-persona",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "@archived-persona help",
                    "content": {"text": "@archived-persona help"},
                },
                "response_mode": "sync",
            },
        )

        self.assertEqual(result["assistant_message"]["metadata"]["delivery"], "persona")
        self.assertEqual(result["assistant_message"]["metadata"]["persona_error"], "not_published")
        self.assertIn("not published", result["assistant_message"]["plain_text"])
        self.assertIsNone(_FakeModelClient.last_system_message)

    async def test_post_message_async_persona_invocation_returns_stream_and_completes_later(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        published = await self._publish_minimal_persona(
            user_id="async-persona-user",
            persona_name="Async Persona",
            memory_id="async-persona-source-memory",
        )
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-async-persona",
                "created_by_user_id": "async-persona-user",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-async-persona",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "@async-persona review this",
                    "content": {"text": "@async-persona review this"},
                },
                "response_mode": "async",
            },
        )

        self.assertEqual(result["message"]["id"], "message-async-persona")
        self.assertNotIn("assistant_message", result)
        self.assertEqual(
            result["stream_url"],
            "/conversations/conversation-async-persona/stream?after=message-async-persona",
        )

        async def _assistant_messages():
            messages = await self.service.list_messages(conversation.id)
            return [item for item in messages["items"] if item["role"] == "assistant"]

        assistant_messages = []
        for _ in range(20):
            assistant_messages = await _assistant_messages()
            if assistant_messages:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(assistant_messages)
        self.assertEqual(assistant_messages[-1]["metadata"]["delivery"], "persona")
        self.assertEqual(assistant_messages[-1]["metadata"]["persona_id"], published["persona"]["id"])
        self.assertEqual(
            assistant_messages[-1]["metadata"]["persona_provenance"]["package_strategy"],
            "item-synthesis-v1",
        )

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
        self.assertIn("create the UI approval request", _FakeModelClient.last_system_message)
        self.assertIn("backend apply/persist step", _FakeModelClient.last_system_message)
        self.assertIn("backend/UI mismatch", _FakeModelClient.last_system_message)
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
                memory_type="decision",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-commitment-1",
                scope="conversation",
                conversation_id="conversation-memory-v2-1",
                content="Finish the implementation spec before changing runtime behavior.",
                memory_type="task_commitment",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-preference-1",
                scope="user",
                created_by_user_id="user-1",
                content="The user's timezone preference is Asia/Singapore.",
                summary="Timezone preference is Asia/Singapore.",
                memory_type="preference",
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
                memory_type="daily_summary",
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
                memory_type="fact",
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

    async def test_context_pack_prompt_injection_is_disabled_by_default(self) -> None:
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
                "id": "conversation-context-pack-disabled",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="context-pack-disabled",
                scope="conversation",
                conversation_id=conversation.id,
                source_conversation_id=conversation.id,
                source="compact_tool",
                content="Compact context that should stay out of prompts by default.",
                summary="Disabled context pack.",
                memory_type="context_pack",
                metadata={"mode": "handoff"},
                tags=["context_pack", "conversation", "handoff"],
            )
        )

        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-context-pack-disabled",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "What context do you have?",
                    "content": {"text": "What context do you have?"},
                },
            },
        )

        assert _FakeModelClient.last_system_message is not None
        self.assertNotIn("Relevant compact conversation context", _FakeModelClient.last_system_message)
        self.assertNotIn("Disabled context pack", _FakeModelClient.last_system_message)
        self.assertNotIn("Compact context that should stay out", _FakeModelClient.last_system_message)

    async def test_context_pack_prompt_injection_can_be_enabled(self) -> None:
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
                "id": "conversation-context-pack-enabled",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="context-pack-enabled",
                scope="conversation",
                conversation_id=conversation.id,
                source_conversation_id=conversation.id,
                source="compact_tool",
                content="The compact pack says the user wants context packs used for long conversations.",
                summary="Enabled context pack.",
                memory_type="context_pack",
                metadata={"mode": "handoff"},
                tags=["context_pack", "conversation", "handoff"],
            )
        )

        with patch.dict(os.environ, {"MEMORY_CONTEXT_PACK_PROMPT_INJECTION_ENABLED": "true"}, clear=False):
            reset_settings_cache()
            await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-context-pack-enabled",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "What context do you have?",
                        "content": {"text": "What context do you have?"},
                    },
                },
            )
            reset_settings_cache()

        assert _FakeModelClient.last_system_message is not None
        self.assertIn("Relevant compact conversation context", _FakeModelClient.last_system_message)
        self.assertIn("Enabled context pack", _FakeModelClient.last_system_message)
        self.assertIn("The compact pack says", _FakeModelClient.last_system_message)

    async def test_context_pack_prompt_injection_prefers_higher_importance_pack(self) -> None:
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
                "id": "conversation-context-pack-importance",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="context-pack-low-importance",
                scope="conversation",
                conversation_id=conversation.id,
                source_conversation_id=conversation.id,
                source="compact_tool",
                content="Lower importance context pack.",
                summary="Low importance pack.",
                memory_type="context_pack",
                importance=20,
                metadata={"mode": "handoff"},
                tags=["context_pack", "conversation", "handoff"],
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="context-pack-high-importance",
                scope="conversation",
                conversation_id=conversation.id,
                source_conversation_id=conversation.id,
                source="compact_tool",
                content="Higher importance context pack.",
                summary="High importance pack.",
                memory_type="context_pack",
                importance=95,
                metadata={"mode": "handoff"},
                tags=["context_pack", "conversation", "handoff"],
            )
        )

        with patch.dict(
                os.environ,
                {
                    "MEMORY_CONTEXT_PACK_PROMPT_INJECTION_ENABLED": "true",
                    "MEMORY_CONTEXT_PACK_PROMPT_LIMIT": "1",
                },
                clear=False,
        ):
            reset_settings_cache()
            await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-context-pack-importance",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Which compact context do you use?",
                        "content": {"text": "Which compact context do you use?"},
                    },
                },
            )
            reset_settings_cache()

        assert _FakeModelClient.last_system_message is not None
        self.assertIn("High importance pack.", _FakeModelClient.last_system_message)
        self.assertIn("Higher importance context pack.", _FakeModelClient.last_system_message)
        self.assertNotIn("Low importance pack.", _FakeModelClient.last_system_message)

    async def test_context_pack_history_compaction_keeps_recent_raw_messages(self) -> None:
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
                "id": "conversation-context-pack-history-compaction",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="context-pack-history-compaction",
                scope="conversation",
                conversation_id=conversation.id,
                source_conversation_id=conversation.id,
                source="compact_tool",
                content="Older discussion has already been compacted into this handoff pack.",
                summary="History compaction pack.",
                memory_type="context_pack",
                metadata={"mode": "handoff"},
                tags=["context_pack", "conversation", "handoff"],
            )
        )
        for index in range(12):
            await self.context.conversation_message_repo.create(
                ConversationMessage(
                    id=f"message-history-compaction-{index:02d}",
                    conversation_id=conversation.id,
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text=f"Old raw message {index}",
                    content={"text": f"Old raw message {index}"},
                )
            )

        with patch.dict(
                os.environ,
                {
                    "MEMORY_CONTEXT_PACK_PROMPT_INJECTION_ENABLED": "true",
                    "MEMORY_CONTEXT_PACK_HISTORY_COMPACTION_ENABLED": "true",
                    "MEMORY_CONTEXT_PACK_HISTORY_MIN_MESSAGES": "10",
                    "MEMORY_CONTEXT_PACK_HISTORY_RECENT_MESSAGES": "3",
                },
                clear=False,
        ):
            reset_settings_cache()
            await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-history-compaction-latest",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Latest raw message must stay visible.",
                        "content": {"text": "Latest raw message must stay visible."},
                    },
                },
            )
            reset_settings_cache()

        model_messages = _FakeModelClient.seen_messages[-1]
        flattened = "\n".join(str(content) for _, content, _ in model_messages)
        self.assertIn("Relevant compact conversation context", flattened)
        self.assertIn("History compaction pack", flattened)
        self.assertIn("Latest raw message must stay visible.", flattened)
        self.assertIn("Old raw message 11", flattened)
        self.assertNotIn("Old raw message 0", flattened)
        self.assertLessEqual(len([role for role, _, _ in model_messages if role == "user"]), 3)

    async def test_context_pack_history_compaction_can_use_estimated_token_threshold(self) -> None:
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
                "id": "conversation-context-pack-token-compaction",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="context-pack-token-compaction",
                scope="conversation",
                conversation_id=conversation.id,
                source_conversation_id=conversation.id,
                source="compact_tool",
                content="Token-heavy older discussion is already compacted.",
                summary="Token threshold pack.",
                memory_type="context_pack",
                metadata={"mode": "handoff"},
                tags=["context_pack", "conversation", "handoff"],
            )
        )
        long_text = "Token-heavy old raw message. " * 20
        for index in range(4):
            await self.context.conversation_message_repo.create(
                ConversationMessage(
                    id=f"message-token-compaction-{index:02d}",
                    conversation_id=conversation.id,
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text=f"{long_text}{index}",
                    content={"text": f"{long_text}{index}"},
                )
            )

        with patch.dict(
                os.environ,
                {
                    "MEMORY_CONTEXT_PACK_PROMPT_INJECTION_ENABLED": "true",
                    "MEMORY_CONTEXT_PACK_HISTORY_COMPACTION_ENABLED": "true",
                    "MEMORY_CONTEXT_PACK_HISTORY_MIN_MESSAGES": "100",
                    "MEMORY_CONTEXT_PACK_HISTORY_MAX_RAW_TOKENS": "20",
                    "MEMORY_CONTEXT_PACK_HISTORY_RECENT_MESSAGES": "2",
                },
                clear=False,
        ):
            reset_settings_cache()
            await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-token-compaction-latest",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Latest token-threshold message must stay visible.",
                        "content": {"text": "Latest token-threshold message must stay visible."},
                    },
                },
            )
            reset_settings_cache()

        model_messages = _FakeModelClient.seen_messages[-1]
        flattened = "\n".join(str(content) for _, content, _ in model_messages)
        self.assertIn("Token threshold pack.", flattened)
        self.assertIn("Latest token-threshold message must stay visible.", flattened)
        self.assertNotIn("message. 0", flattened)
        self.assertLessEqual(len([role for role, _, _ in model_messages if role == "user"]), 2)

    async def test_context_pack_auto_create_enables_history_compaction(self) -> None:
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
                "id": "conversation-context-pack-auto-create",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )
        for index in range(12):
            await self.context.conversation_message_repo.create(
                ConversationMessage(
                    id=f"message-auto-context-pack-{index:02d}",
                    conversation_id=conversation.id,
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text=f"Auto compact old raw message {index}",
                    content={"text": f"Auto compact old raw message {index}"},
                )
            )

        with patch.dict(
                os.environ,
                {
                    "MEMORY_CONTEXT_PACK_AUTO_CREATE_ENABLED": "true",
                    "MEMORY_CONTEXT_PACK_PROMPT_INJECTION_ENABLED": "true",
                    "MEMORY_CONTEXT_PACK_HISTORY_COMPACTION_ENABLED": "true",
                    "MEMORY_CONTEXT_PACK_HISTORY_MIN_MESSAGES": "10",
                    "MEMORY_CONTEXT_PACK_HISTORY_RECENT_MESSAGES": "3",
                },
                clear=False,
        ):
            reset_settings_cache()
            await self.service.post_message(
                conversation.id,
                {
                    "message": {
                        "id": "message-auto-context-pack-latest",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Latest message after auto compact.",
                        "content": {"text": "Latest message after auto compact."},
                    },
                },
            )
            reset_settings_cache()

        packs = await self.context.memory_repo.query(
            conversation_id=conversation.id,
            memory_types=["context_pack"],
            tags=["handoff"],
            limit=10,
        )
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0].metadata["source_range"], "older_than_recent")
        self.assertTrue(str(packs[0].metadata["idempotency_key"]).startswith("auto-handoff:"))

        model_messages = _FakeModelClient.seen_messages[-1]
        user_messages = [str(content) for role, content, _ in model_messages if role == "user"]
        self.assertLessEqual(len(user_messages), 3)
        self.assertIn("Latest message after auto compact.", "\n".join(user_messages))
        assert _FakeModelClient.last_system_message is not None
        self.assertIn("Relevant compact conversation context", _FakeModelClient.last_system_message)

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
        expected_system_tool_ids = [
            *workflow_system_tool_ids(),
            *tool_management_system_tool_ids(),
            *agent_management_system_tool_ids(),
            *connector_system_tool_ids(),
            *memory_system_tool_ids(),
            *execution_system_tool_ids(),
            *command_system_tool_ids(),
            *graph_system_tool_ids(),
            *goal_system_tool_ids(),
        ]
        self.assertEqual(
            sorted(agent.tool_ids),
            sorted(
                [
                    *expected_system_tool_ids,
                    *DEFAULT_MAIN_AGENT_SPEECH_TOOL_IDS,
                    "mcp:computer-use-macos:press_key",
                    "mcp:computer-use-macos:snapshot",
                ]
            ),
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
                security=SecuritySettings(
                    requires_approval=False,
                    module_allowlist=["tests.native_test_tools"],
                    function_allowlist=["echo_tool"],
                ),
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

    async def test_main_agent_routes_extended_workflow_tools_through_contract_runtime(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-improvement-review",
                name="Workflow Improvement Review",
                description="Workflow with an empty improvement proposal history.",
                entrypoint="manual",
                metadata={"visible_to_main_agent": True},
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use workflow review tools when helpful.",
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
                        id="tool-call-list-improvements",
                        name="list_workflow_improvement_proposals",
                        arguments={"workflow_id": "workflow-improvement-review"},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="There are no proposals yet.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-workflow-improvement-review",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-improvement-review",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "List workflow improvement proposals",
                    "content": {"text": "List workflow improvement proposals"},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["plain_text"], "There are no proposals yet.")
        messages = await self.service.list_messages(conversation.id)
        tool_result = next(item for item in messages["items"] if item["message_type"] == "tool_result")
        self.assertEqual(tool_result["content"]["result"]["status"], "ok")
        self.assertEqual(tool_result["content"]["result"]["count"], 0)
        self.assertNotIn("error", tool_result["content"]["result"])

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
        with patch.dict(os.environ, {"MEMORY_CONTEXT_PACK_AUTO_CREATE_ENABLED": "true"}, clear=False):
            reset_settings_cache()
            approved = await self.service.approve_request(
                result["approval_request"]["id"],
                actor_user_id="user-1",
                reason="Create it",
            )
            reset_settings_cache()
        self.assertEqual(approved["workflow"]["id"], "workflow-tool-create")
        persisted = await self.context.workflow_repo.get("workflow-tool-create")
        assert persisted is not None
        self.assertEqual(persisted.metadata["created_by"], "user-1")
        self.assertEqual(persisted.metadata["owner_ids"], ["user-1"])
        self.assertEqual(persisted.metadata["provenance"]["approval_request_id"], result["approval_request"]["id"])
        packs = await self.context.memory_repo.query(
            workflow_id="workflow-tool-create",
            source="compact_tool",
            memory_types=["context_pack"],
            tags=["handoff"],
            limit=10,
        )
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0].scope.value, "workflow")
        self.assertEqual(packs[0].metadata["target_scope"], "workflow")
        self.assertTrue(str(packs[0].metadata["idempotency_key"]).startswith("workflow-mutation-handoff:"))
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            ["user_text", "tool_call", "workflow_proposal", "tool_result", "approval_result"],
        )
        self.assertEqual(messages["items"][3]["content"]["result"]["status"], "approval_requested")
        self.assertEqual(
            messages["items"][3]["content"]["result"]["approval_request_id"],
            result["approval_request"]["id"],
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

    async def test_main_agent_prefers_reusing_matching_global_agents_when_building_workflows(self) -> None:
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
        await self.context.agent_repo.create(
            AgentDefinition(
                id="agent-launch-strategist",
                name="Launch Strategist",
                role="Plans the workflow approach",
                description="Reusable strategist for launch planning workflows.",
                instructions="Use the existing catalog strategist playbook.",
                backstory="Catalog agent",
                model_profile_id="profile-fake",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-create-goal-reuse",
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
                "id": "conversation-workflow-create-goal-tool-reuse-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-workflow-create-goal-tool-reuse-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create a launch planning workflow",
                    "content": {"text": "Create a launch planning workflow"},
                },
            },
        )

        proposed = result["approval_request"]["proposed_payload"]["workflow"]
        self.assertEqual(proposed["agent_definitions"][0]["id"], "agent-launch-strategist")
        self.assertEqual(
            proposed["agent_definitions"][0]["metadata"]["workflow_builder_reused_global_agent_id"],
            "agent-launch-strategist",
        )
        self.assertEqual(proposed["task_definitions"][0]["agent_id"], "agent-launch-strategist")

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
        with patch.dict(os.environ, {"MEMORY_CONTEXT_PACK_AUTO_CREATE_ENABLED": "true"}, clear=False):
            reset_settings_cache()
            approved = await self.service.approve_request(
                result["approval_request"]["id"],
                actor_user_id="user-1",
                reason="Update it",
            )
            reset_settings_cache()
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
        packs = await self.context.memory_repo.query(
            workflow_id="workflow-tool-update",
            source="compact_tool",
            memory_types=["context_pack"],
            tags=["handoff"],
            limit=10,
        )
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0].scope.value, "workflow")
        self.assertEqual(packs[0].metadata["target_scope"], "workflow")
        self.assertTrue(str(packs[0].metadata["idempotency_key"]).startswith("workflow-mutation-handoff:"))
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            ["user_text", "tool_call", "workflow_update_proposal", "tool_result", "approval_result"],
        )
        self.assertEqual(messages["items"][3]["content"]["result"]["status"], "approval_requested")
        self.assertEqual(
            messages["items"][3]["content"]["result"]["approval_request_id"],
            result["approval_request"]["id"],
        )

    async def test_popup_provider_metadata_drives_workflow_update_tool_and_approval_diff(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-popup-provider-update",
                name="Popup Provider Workflow",
                description="Original popup description",
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
                agent_instructions="Use page providers when the popup supplies them.",
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
                        id="tool-call-popup-provider-update",
                        name="ProposeWorkflowUpdate",
                        arguments={
                            "workflow_id": "workflow-popup-provider-update",
                            "summary": "Append popup provider smoke text.",
                            "workflow": self._workflow_payload(
                                workflow_id="workflow-popup-provider-update",
                                name="Popup Provider Workflow",
                                description="Original popup description. Verified by popup provider.",
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
                "id": "conversation-popup-provider-update",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-popup-provider-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "On this workflow page, propose appending the provider smoke text.",
                    "content": {"text": "On this workflow page, propose appending the provider smoke text."},
                    "metadata": {
                        "page_context": {
                            "surface": "workflow.detail",
                            "route": "/workflows/workflow-popup-provider-update",
                            "selection": {"workflowId": "workflow-popup-provider-update"},
                            "entities": [
                                {
                                    "type": "workflow",
                                    "id": "workflow-popup-provider-update",
                                    "label": "Popup Provider Workflow",
                                }
                            ],
                            "allowedActions": ["workflow.inspect", "workflow.propose_update"],
                        },
                        "assistant_providers": {
                            "version": "2026-05-27",
                            "providers": [
                                {
                                    "id": "workflow.provider",
                                    "label": "Workflow provider",
                                    "systemToolIds": [
                                        "agency.workflow.get",
                                        "agency.workflow.propose-update",
                                    ],
                                    "selection": {"workflowId": "workflow-popup-provider-update"},
                                }
                            ],
                        },
                    },
                },
            },
        )

        assert _FakeModelClient.last_system_message is not None
        self.assertIn("assistant_providers", _FakeModelClient.last_system_message)
        self.assertIn("workflow.provider", _FakeModelClient.last_system_message)
        self.assertIn("agency.workflow.propose-update", _FakeModelClient.last_system_message)
        self.assertIn("Do not treat arbitrary hyphenated marker text", _FakeModelClient.last_system_message)
        self.assertEqual(result["assistant_message"]["message_type"], "workflow_update_proposal")
        approval = result["approval_request"]
        self.assertEqual(approval["approval_type"], "workflow_update")
        self.assertEqual(approval["conversation_id"], conversation.id)
        self.assertEqual(approval["origin_message_id"], "message-popup-provider-update")
        self.assertEqual(approval["metadata"]["source"], "popup_assistant")
        self.assertEqual(approval["metadata"]["source_surface"], "workflow.detail")
        self.assertEqual(approval["metadata"]["source_route"], "/workflows/workflow-popup-provider-update")
        self.assertEqual(approval["metadata"]["source_page_context"]["selection"]["workflowId"],
                         "workflow-popup-provider-update")
        self.assertEqual(approval["metadata"]["source_provider_ids"], ["workflow.provider"])
        diff_rows = approval["proposed_payload"]["diff"]
        self.assertEqual(diff_rows[0]["path"], "description")
        self.assertEqual(diff_rows[0]["current"], "Original popup description")
        self.assertEqual(
            diff_rows[0]["proposed"],
            "Original popup description. Verified by popup provider.",
        )
        approvals = await self.service.list_approval_requests(conversation.id)
        self.assertEqual([item["id"] for item in approvals["items"]], [approval["id"]])

        approved = await self.service.approve_request(
            approval["id"],
            actor_user_id="user-1",
            reason="Approve popup provider smoke update.",
        )

        self.assertEqual(approved["approval_request"]["status"], "approved")
        workflow = await self.context.workflow_repo.get("workflow-popup-provider-update")
        assert workflow is not None
        self.assertEqual(workflow.description, "Original popup description. Verified by popup provider.")
        self.assertEqual(workflow.metadata["provenance"]["approval_request_id"], approval["id"])
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            ["user_text", "tool_call", "workflow_update_proposal", "tool_result", "approval_result"],
        )
        self.assertEqual(messages["items"][3]["content"]["result"]["status"], "approval_requested")
        self.assertEqual(messages["items"][3]["content"]["result"]["approval_request_id"], approval["id"])

    async def test_popup_agent_provider_drives_agent_update_tool_and_approval_diff(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use agent page providers when supplied.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        await self.context.agent_repo.save(
            AgentDefinition(
                id="agent-popup-provider",
                name="Popup Agent",
                description="Original agent description",
                instructions="Original instructions",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-popup-agent-update",
                        name="ProposeAgentUpdate",
                        arguments={
                            "agent_id": "agent-popup-provider",
                            "summary": "Update popup agent description.",
                            "patch": {"description": "Updated by popup agent provider"},
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-popup-agent-provider",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-popup-agent-provider",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "On this agent page, propose updating the selected agent description.",
                    "content": {"text": "On this agent page, propose updating the selected agent description."},
                    "metadata": {
                        "page_context": {
                            "surface": "agent.list",
                            "selection": {"agentId": "agent-popup-provider"},
                            "entities": [{"type": "agent", "id": "agent-popup-provider", "label": "Popup Agent"}],
                        },
                        "assistant_providers": {
                            "version": "2026-05-27",
                            "providers": [
                                {
                                    "id": "agent.provider",
                                    "label": "Agent provider",
                                    "systemToolIds": ["agency.agent.get", "agency.agent.propose-update"],
                                    "selection": {"agentId": "agent-popup-provider"},
                                }
                            ],
                        },
                    },
                },
            },
        )

        assert _FakeModelClient.last_system_message is not None
        self.assertIn("agent.provider", _FakeModelClient.last_system_message)
        self.assertIn("agency.agent.propose-update", _FakeModelClient.last_system_message)
        approval = result["approval_request"]
        self.assertEqual(result["assistant_message"]["message_type"], "approval_request")
        self.assertEqual(approval["conversation_id"], conversation.id)
        self.assertEqual(approval["origin_message_id"], "message-popup-agent-provider")
        self.assertEqual(approval["target_type"], "agent")
        self.assertEqual(approval["target_id"], "agent-popup-provider")
        self.assertEqual(approval["metadata"]["source"], "popup_assistant")
        self.assertEqual(approval["metadata"]["source_surface"], "agent.list")
        self.assertEqual(approval["metadata"]["source_provider_ids"], ["agent.provider"])
        diff_rows = approval["proposed_payload"]["diff"]
        self.assertEqual(diff_rows[0]["path"], "description")
        self.assertEqual(diff_rows[0]["current"], "Original agent description")
        self.assertEqual(diff_rows[0]["proposed"], "Updated by popup agent provider")

        approved = await self.service.approve_request(
            approval["id"],
            actor_user_id="user-1",
            reason="Approve agent provider update.",
        )

        self.assertEqual(approved["approval_request"]["status"], "approved")
        agent = await self.context.agent_repo.get("agent-popup-provider")
        assert agent is not None
        self.assertEqual(agent.description, "Updated by popup agent provider")
        self.assertEqual(agent.metadata["provenance"]["approval_request_id"], approval["id"])

    async def test_popup_tool_provider_drives_tool_update_tool_and_approval_diff(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition.model_validate(
                self._tool_payload(
                    tool_id="tool-popup-provider",
                    name="popup_provider_tool",
                    description="Original tool provider description",
                )
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use tool page providers when supplied.",
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
                        id="tool-call-popup-tool-update",
                        name="ProposeToolUpdate",
                        arguments={
                            "tool_id": "tool-popup-provider",
                            "summary": "Update popup provider tool.",
                            "tool": self._tool_payload(
                                tool_id="tool-popup-provider",
                                name="popup_provider_tool",
                                description="Updated by popup tool provider",
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
                "id": "conversation-popup-tool-provider",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-popup-tool-provider",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "On this tool page, propose updating the selected tool description.",
                    "content": {"text": "On this tool page, propose updating the selected tool description."},
                    "metadata": {
                        "page_context": {
                            "surface": "tools.contracts",
                            "selection": {"toolId": "tool-popup-provider"},
                            "entities": [{"type": "tool", "id": "tool-popup-provider", "label": "popup_provider_tool"}],
                        },
                        "assistant_providers": {
                            "version": "2026-05-27",
                            "providers": [
                                {
                                    "id": "tool.provider",
                                    "label": "Tool provider",
                                    "systemToolIds": ["agency.tool.get", "agency.tool.propose-update"],
                                    "selection": {"toolId": "tool-popup-provider"},
                                }
                            ],
                        },
                    },
                },
            },
        )

        assert _FakeModelClient.last_system_message is not None
        self.assertIn("tool.provider", _FakeModelClient.last_system_message)
        self.assertIn("agency.tool.propose-update", _FakeModelClient.last_system_message)
        approval = result["approval_request"]
        self.assertEqual(result["assistant_message"]["message_type"], "approval_request")
        self.assertEqual(approval["approval_type"], "tool_update")
        self.assertEqual(approval["conversation_id"], conversation.id)
        self.assertEqual(approval["origin_message_id"], "message-popup-tool-provider")
        self.assertEqual(approval["metadata"]["source"], "popup_assistant")
        self.assertEqual(approval["metadata"]["source_surface"], "tools.contracts")
        self.assertEqual(approval["metadata"]["source_provider_ids"], ["tool.provider"])
        diff_rows = approval["proposed_payload"]["diff"]
        self.assertEqual(diff_rows[0]["path"], "description")
        self.assertEqual(diff_rows[0]["current"], "Original tool provider description")
        self.assertEqual(diff_rows[0]["proposed"], "Updated by popup tool provider")

        approved = await self.service.approve_request(
            approval["id"],
            actor_user_id="user-1",
            reason="Approve tool provider update.",
        )

        self.assertEqual(approved["approval_request"]["status"], "approved")
        tool = await self.context.tool_repo.get("tool-popup-provider")
        assert tool is not None
        self.assertEqual(tool.description, "Updated by popup tool provider")
        self.assertEqual(tool.framework_hints.metadata["provenance"]["approval_request_id"], approval["id"])

    async def test_popup_execution_provider_drives_execution_control_tool(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use execution page providers when supplied.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        paused_execution = Execution(
            id="execution-popup-provider",
            workflow_id="workflow-popup-provider-update",
            runtime_adapter_id="native",
            status=ExecutionStatus.PAUSED,
            input_payload={},
            created_by="user-1",
        )
        await self.context.execution_store.save_execution(paused_execution)
        self.context.control_plane.pause = AsyncMock(return_value=paused_execution)
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-popup-execution-pause",
                        name="pause_execution",
                        arguments={"execution_id": "execution-popup-provider"},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="Paused the selected run.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-popup-execution-provider",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-popup-execution-provider",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "On this run page, pause the selected execution.",
                    "content": {"text": "On this run page, pause the selected execution."},
                    "metadata": {
                        "page_context": {
                            "surface": "runs.detail",
                            "selection": {"runId": "execution-popup-provider"},
                            "entities": [{"type": "run", "id": "execution-popup-provider", "label": "Selected run"}],
                        },
                        "assistant_providers": {
                            "version": "2026-05-27",
                            "providers": [
                                {
                                    "id": "execution.provider",
                                    "label": "Execution provider",
                                    "systemToolIds": ["agency.execution.get", "agency.execution.pause"],
                                    "selection": {"runId": "execution-popup-provider"},
                                }
                            ],
                        },
                    },
                },
            },
        )

        assert _FakeModelClient.last_system_message is not None
        self.assertIn("execution.provider", _FakeModelClient.last_system_message)
        self.assertIn("agency.execution.pause", _FakeModelClient.last_system_message)
        self.assertEqual(result["assistant_message"]["plain_text"], "Paused the selected run.")
        self.context.control_plane.pause.assert_awaited_once_with("execution-popup-provider")
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            ["user_text", "tool_call", "tool_result", "assistant_text"],
        )
        tool_result = messages["items"][2]["content"]["result"]
        self.assertEqual(tool_result["status"], "ok")
        self.assertEqual(tool_result["execution"]["status"], "paused")

    async def test_popup_connector_provider_drives_connector_credentials_tool_with_redaction(self) -> None:
        await self.context.credential_repo.create(
            CredentialDefinition(
                id="credential-popup-provider",
                owner_user_id="user-1",
                name="Popup Telegram",
                provider="telegram-bot",
                secret_ref="env://TELEGRAM_BOT_TOKEN",
                metadata={"bot_token": "should-redact", "chat_id": "12345"},
            )
        )
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Use connector page providers when supplied.",
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
                        id="tool-call-popup-connector-credentials",
                        name="list_connector_credentials",
                        arguments={"provider": "telegram"},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="Connector credentials are ready.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-popup-connector-provider",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-popup-connector-provider",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "On this integrations page, inspect my Telegram connector credentials.",
                    "content": {"text": "On this integrations page, inspect my Telegram connector credentials."},
                    "metadata": {
                        "page_context": {
                            "surface": "integrations",
                            "selection": {"provider": "telegram-bot"},
                            "entities": [{"type": "connector", "id": "telegram-bot", "label": "Telegram"}],
                        },
                        "assistant_providers": {
                            "version": "2026-05-27",
                            "providers": [
                                {
                                    "id": "connector.provider",
                                    "label": "Connector provider",
                                    "systemToolIds": [
                                        "agency.connector.capabilities",
                                        "agency.connector.credentials",
                                    ],
                                    "selection": {"provider": "telegram-bot"},
                                }
                            ],
                        },
                    },
                },
            },
        )

        assert _FakeModelClient.last_system_message is not None
        self.assertIn("connector.provider", _FakeModelClient.last_system_message)
        self.assertIn("agency.connector.credentials", _FakeModelClient.last_system_message)
        self.assertEqual(result["assistant_message"]["plain_text"], "Connector credentials are ready.")
        messages = await self.service.list_messages(conversation.id)
        self.assertEqual(
            [item["message_type"] for item in messages["items"]],
            ["user_text", "tool_call", "tool_result", "assistant_text"],
        )
        tool_result = messages["items"][2]["content"]["result"]
        self.assertEqual(tool_result["status"], "ok")
        self.assertEqual(tool_result["items"][0]["id"], "credential-popup-provider")
        self.assertEqual(tool_result["items"][0]["metadata"]["bot_token"], "[REDACTED]")
        self.assertEqual(tool_result["items"][0]["metadata"]["chat_id"], "12345")

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
        self.assertEqual(proposed["metadata"]["repo_write_permission"]["status"], "pending_human_approval")
        self.assertEqual(
            result["assistant_message"]["content"]["repo_write_permission"]["permission_type"],
            "repo_write",
        )
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
        self.assertEqual(_FakeModelClient.seen_messages[1][-2][0], "assistant")
        self.assertEqual(
            _FakeModelClient.seen_message_tool_calls[1][-2],
            [("tool-call-list-tools", "ListTools")],
        )
        self.assertEqual(_FakeModelClient.seen_messages[1][-1][0], "tool")
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
            ["user_text", "tool_call", "approval_request", "tool_result", "approval_result"],
        )
        self.assertEqual(messages["items"][3]["content"]["result"]["status"], "approval_requested")
        self.assertEqual(
            messages["items"][3]["content"]["result"]["approval_request_id"],
            result["approval_request"]["id"],
        )

    async def test_main_agent_can_propose_agent_update_from_tool_call(self) -> None:
        await self.setup_service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Update agents when asked.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )
        await self.context.agent_repo.save(
            AgentDefinition(
                id="agent-target",
                name="Target Agent",
                description="Original description",
                instructions="Original instructions",
            )
        )
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-agent-update",
                        name="ProposeAgentUpdate",
                        arguments={
                            "agent_id": "agent-target",
                            "summary": "Update agent 'Target Agent'.",
                            "patch": {"description": "Updated by agent management tool"},
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            )
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-agent-update-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-agent-update-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update the target agent.",
                    "content": {"text": "Update the target agent."},
                },
            },
        )

        self.assertEqual(result["assistant_message"]["message_type"], "approval_request")
        self.assertEqual(result["approval_request"]["approval_type"], "other")
        self.assertEqual(result["approval_request"]["target_type"], "agent")
        self.assertEqual(result["approval_request"]["target_id"], "agent-target")
        self.assertEqual(
            result["approval_request"]["proposed_payload"]["agent"]["description"],
            "Updated by agent management tool",
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

    async def test_tool_create_rejects_flattened_security_and_missing_implementation(self) -> None:
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
        invalid_tool = self._tool_payload(tool_id="tool-invalid-create", name="invalid_create_tool")
        invalid_tool.pop("implementation")
        invalid_tool["read_only"] = True
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-tool-invalid-create",
                        name="ProposeNewTool",
                        arguments={
                            "summary": "Create tool 'Invalid Create Tool'.",
                            "tool": invalid_tool,
                        },
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(
                content="I could not create that tool because the payload is invalid: missing required field(s): implementation.",
                provider="fake",
                model="fake-model",
            ),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-create-invalid-shape-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        result = await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-create-invalid-shape-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create an invalid tool payload",
                    "content": {"text": "Create an invalid tool payload"},
                },
            },
        )

        self.assertIn("missing required field(s): implementation", result["assistant_message"]["plain_text"])
        self.assertNotIn("read_only", result["assistant_message"]["plain_text"])
        self.assertIsNone(await self.context.tool_repo.get("tool-invalid-create"))
        approvals = await self.service.list_approval_requests(conversation.id)
        self.assertEqual(approvals["items"], [])

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

    async def test_chat_tool_create_rejects_reserved_webhook_ids(self) -> None:
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
        reserved_webhook_tool = self._tool_payload(tool_id="agency.webhook.send", name="send_webhook")
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="tool-call-propose-tool-webhook-reserved",
                        name="ProposeNewTool",
                        arguments={"tool": reserved_webhook_tool},
                    )
                ],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="I’ll use a non-reserved webhook id instead.", provider="fake", model="fake-model"),
        ]
        conversation = await self.service.create_conversation(
            {
                "id": "conversation-tool-create-webhook-reserved-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        await self.service.post_message(
            conversation.id,
            {
                "message": {
                    "id": "message-tool-create-webhook-reserved-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create a webhook sender tool",
                    "content": {"text": "Create a webhook sender tool"},
                },
            },
        )

        self.assertIsNone(await self.context.tool_repo.get("agency.webhook.send"))
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
                security=SecuritySettings(
                    requires_approval=False,
                    module_allowlist=["tests.native_test_tools"],
                    function_allowlist=["echo_tool"],
                ),
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

        replayed_assistant_tool_calls = [
            tool_calls
            for message, tool_calls in zip(_FakeModelClient.seen_messages[-1], _FakeModelClient.seen_message_tool_calls[-1])
            if message[0] == "assistant" and tool_calls
        ]
        self.assertEqual(replayed_assistant_tool_calls, [[("tool-call-2", "echo_tool")]])
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
                security=SecuritySettings(
                    requires_approval=True,
                    module_allowlist=["tests.native_test_tools"],
                    function_allowlist=["echo_tool"],
                ),
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
                security=SecuritySettings(
                    requires_approval=True,
                    module_allowlist=["tests.native_test_tools"],
                    function_allowlist=["echo_tool"],
                ),
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
