from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.domain import (
    Conversation,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    Execution,
    ExecutionEventType,
    ModelProfileDefinition,
    WorkflowDefinition,
)
from app.llm.base import ModelResponse
from app.llm.registry import LLMEnvironmentConfig
from app.services.conversation_compact import ConversationCompactService, MODE_PROFILE_REGISTRY, SUPPORTED_COMPACT_MODES
from app.services.conversations.audit import ConversationAuditService


class _FakeCompactModelClient:
    provider_key = "compact_fake"
    responses: list[ModelResponse | Exception] = []
    seen_schema_names: list[str | None] = []
    seen_system_messages: list[str] = []
    seen_user_messages: list[str] = []

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content="unused", provider="compact_fake", model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        _FakeCompactModelClient.seen_schema_names.append(kwargs.get("schema_name"))
        _FakeCompactModelClient.seen_system_messages.append(
            next((item.content for item in messages if item.role == "system"), "")
        )
        _FakeCompactModelClient.seen_user_messages.append(
            next((item.content for item in reversed(messages) if item.role == "user"), "")
        )
        if _FakeCompactModelClient.responses:
            response = _FakeCompactModelClient.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return ModelResponse(
            content={
                "summary": "LLM compact summary.",
                "content": "LLM compact content.",
                "structured": {
                    "goals": ["Build context packs."],
                    "facts": [],
                    "preferences": [],
                    "decisions": ["Use memory_records."],
                    "constraints": ["Keep raw messages."],
                    "commitments": [],
                    "open_questions": [],
                    "next_actions": ["Add prompt usage."],
                    "artifacts": ["docs/compact-tool.md"],
                    "risks": [],
                    "owners": [],
                    "expected_outputs": [],
                    "discarded_approaches": [],
                    "verification_needed": [],
                },
            },
            provider="compact_fake",
            model=self.profile.model,
        )

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "unused"

    def count_tokens(self, messages, **kwargs):
        return 0

    def health_check(self):
        return {"ok": True}


class ConversationCompactApiTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("MEMORY_CONTEXT_PACK_ENABLED", None)
        reset_settings_cache()
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register(
            "compact_fake",
            lambda profile, env: _FakeCompactModelClient(profile, env),
        )
        self._run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(
                    id="profile-compact",
                    name="Compact Fake",
                    provider="compact_fake",
                    model="compact-fake-model",
                    supports_structured_output=True,
                )
            )
        )
        _FakeCompactModelClient.responses = []
        _FakeCompactModelClient.seen_schema_names = []
        _FakeCompactModelClient.seen_system_messages = []
        _FakeCompactModelClient.seen_user_messages = []
        self.client = TestClient(create_app(context=self.context))
        self._run(
            self.context.conversation_repo.create(
                Conversation(
                    id="conversation-compact",
                    created_by_user_id="user-1",
                    workspace_id="workspace-1",
                    channel_type="api",
                )
            )
        )
        self._run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-1",
                    conversation_id="conversation-compact",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="We need a compact tool for Agency conversations.",
                    content={"text": "We need a compact tool for Agency conversations."},
                )
            )
        )
        self._run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-2",
                    conversation_id="conversation-compact",
                    role=ConversationRole.ASSISTANT,
                    message_type=ConversationMessageType.ASSISTANT_TEXT,
                    plain_text=(
                        "Decision: keep full conversation history and store compact context packs "
                        "in memory_records."
                    ),
                    content={
                        "text": (
                            "Decision: keep full conversation history and store compact context packs "
                            "in memory_records."
                        )
                    },
                )
            )
        )
        self._run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-3",
                    conversation_id="conversation-compact",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Next, expose create and list endpoints. What risks remain?",
                    content={"text": "Next, expose create and list endpoints. What risks remain?"},
                )
            )
        )

    def tearDown(self) -> None:
        os.environ.pop("MEMORY_CONTEXT_PACK_ENABLED", None)
        reset_settings_cache()

    @staticmethod
    def _run(awaitable):
        return asyncio.run(awaitable)

    def test_compact_conversation_persists_context_pack(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "handoff", "token_budget": 1200},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["mode"], "handoff")
        self.assertIsNotNone(payload["memory_id"])
        self.assertIn("Current state", payload["content"])
        self.assertEqual(payload["structured"]["message_counts"]["total"], 3)
        self.assertGreaterEqual(payload["progress"]["completed_steps"], 5)
        self.assertEqual(payload["progress"]["failed_steps"], 0)
        self.assertIn("persist", [event["step"] for event in payload["progress"]["events"]])

        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertEqual(memory.memory_type.value, "context_pack")
        self.assertEqual(memory.scope.value, "conversation")
        self.assertEqual(memory.conversation_id, "conversation-compact")
        self.assertEqual(memory.source_conversation_id, "conversation-compact")
        self.assertEqual(memory.metadata["mode"], "handoff")
        self.assertEqual(memory.metadata["source_message_start_id"], "message-1")
        self.assertEqual(memory.metadata["source_message_end_id"], "message-3")

    def test_compact_conversation_can_persist_workspace_scoped_context_pack(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "handoff", "scope": "workspace"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scope"], "workspace")

        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertEqual(memory.scope.value, "workspace")
        self.assertIsNone(memory.conversation_id)
        self.assertEqual(memory.workspace_id, "workspace-1")
        self.assertEqual(memory.source_conversation_id, "conversation-compact")
        self.assertEqual(memory.metadata["target_scope"], "workspace")
        self.assertEqual(memory.metadata["owner_ids"], ["user-1"])

    def test_compact_conversation_can_persist_user_scoped_context_pack(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "memory", "scope": "user"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scope"], "user")

        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertEqual(memory.scope.value, "user")
        self.assertEqual(memory.created_by_user_id, "user-1")
        self.assertEqual(memory.source_conversation_id, "conversation-compact")
        self.assertEqual(memory.metadata["target_scope"], "user")

    def test_compact_conversation_can_persist_workflow_scoped_context_pack(self) -> None:
        self._run(
            self.context.workflow_repo.create(
                WorkflowDefinition(
                    id="workflow-compact-target",
                    name="Compact Target",
                    description="Receives compact context.",
                    entrypoint="manual",
                    metadata={"created_by": "user-1", "owner_ids": ["user-1"]},
                )
            )
        )

        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "workflow", "scope": "workflow", "workflow_id": "workflow-compact-target"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scope"], "workflow")

        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertEqual(memory.scope.value, "workflow")
        self.assertEqual(memory.workflow_id, "workflow-compact-target")
        self.assertEqual(memory.source_conversation_id, "conversation-compact")
        self.assertEqual(memory.metadata["target_scope"], "workflow")

    def test_compact_conversation_persists_source_execution_provenance(self) -> None:
        self._run(
            self.context.execution_store.save_execution(
                Execution(
                    id="execution-compact-source",
                    workflow_id="workflow-source",
                    runtime_adapter="native",
                    status="completed",
                )
            )
        )

        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "handoff", "source_execution_id": "execution-compact-source"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source_execution_id"], "execution-compact-source")
        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertEqual(memory.source_execution_id, "execution-compact-source")
        self.assertEqual(memory.metadata["source_execution_id"], "execution-compact-source")

    def test_compact_conversation_rejects_unknown_source_execution_id(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "handoff", "source_execution_id": "missing-execution"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("Execution 'missing-execution' was not found", response.json()["detail"])

    def test_workflow_scoped_compact_requires_workflow_id(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "workflow", "scope": "workflow"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("workflow_id", response.json()["detail"])

    def test_mode_profile_registry_is_valid(self) -> None:
        self.assertEqual(set(MODE_PROFILE_REGISTRY), SUPPORTED_COMPACT_MODES)
        self.assertEqual(ConversationCompactService.validate_mode_profiles(), [])
        self.assertEqual(MODE_PROFILE_REGISTRY["handoff"].importance, 70)
        self.assertEqual(MODE_PROFILE_REGISTRY["custom"].default_sections[0], "goals")

    def test_sensitive_source_requires_confirmation_to_persist_context_pack(self) -> None:
        self._run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-sensitive",
                    conversation_id="conversation-compact",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="The API key is sk-test-sensitive; preserve only if confirmed.",
                    content={"text": "The API key is sk-test-sensitive; preserve only if confirmed."},
                )
            )
        )

        preview = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "brief", "persist": False},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.json()["sensitive"])

        blocked = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "brief"},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("Sensitive memory writes require explicit user confirmation", blocked.json()["detail"])

        confirmed = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "brief", "confirmed": True},
        )
        self.assertEqual(confirmed.status_code, 200)
        payload = confirmed.json()
        self.assertTrue(payload["sensitive"])

        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertTrue(memory.sensitive)
        self.assertTrue(memory.metadata["sensitive_source_detected"])

    def test_list_compact_packs_returns_active_packs(self) -> None:
        first = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "brief", "token_budget": 500},
        )
        self.assertEqual(first.status_code, 200)

        list_response = self.client.get("/conversations/conversation-compact/compact-packs?mode=brief")

        self.assertEqual(list_response.status_code, 200)
        items = list_response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["memory_type"], "context_pack")
        self.assertEqual(items[0]["metadata"]["mode"], "brief")

    def test_memory_mode_renders_memory_focused_sections(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "memory", "token_budget": 700},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "memory")
        self.assertIn("Stable facts", payload["content"])
        self.assertIn("Preferences", payload["content"])
        self.assertIn("Decisions", payload["content"])

    def test_workflow_mode_renders_workflow_focused_sections(self) -> None:
        self._run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-workflow-owner",
                    conversation_id="conversation-compact",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text=(
                        "Owner: research-agent is responsible for drafting. "
                        "Expected output: a rollout checklist."
                    ),
                    content={
                        "text": (
                            "Owner: research-agent is responsible for drafting. "
                            "Expected output: a rollout checklist."
                        )
                    },
                )
            )
        )

        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "workflow", "token_budget": 700},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "workflow")
        self.assertIn("Workflow goal", payload["content"])
        self.assertIn("Tasks and commitments", payload["content"])
        self.assertIn("Blockers and risks", payload["content"])
        self.assertIn("Owners and target agents", payload["content"])
        self.assertIn("Expected outputs", payload["content"])

    def test_custom_mode_respects_keep_and_drop_fields(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={
                "mode": "custom",
                "custom_keep": ["goals", "decisions", "next_actions", "artifacts"],
                "custom_drop": ["artifacts"],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "custom")
        self.assertIn("Goals", payload["content"])
        self.assertIn("Decisions", payload["content"])
        self.assertIn("Next Actions", payload["content"])
        self.assertNotIn("Artifacts", payload["content"])

        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertEqual(memory.metadata["custom_keep"], ["goals", "decisions", "next_actions", "artifacts"])
        self.assertEqual(memory.metadata["custom_drop"], ["artifacts"])

    def test_tool_result_inclusion_is_mode_specific(self) -> None:
        self._run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-tool-result",
                    conversation_id="conversation-compact",
                    role=ConversationRole.TOOL,
                    message_type=ConversationMessageType.TOOL_RESULT,
                    plain_text="Tool result: wrote docs/tool-output.md for implementation notes.",
                    content={"text": "Tool result: wrote docs/tool-output.md for implementation notes."},
                )
            )
        )

        memory_response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "memory", "persist": False},
        )
        technical_response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "technical", "persist": False},
        )

        self.assertEqual(memory_response.status_code, 200)
        self.assertEqual(technical_response.status_code, 200)
        self.assertNotIn("docs/tool-output.md", memory_response.json()["structured"]["artifacts"])
        self.assertIn("docs/tool-output.md", technical_response.json()["content"])
        self.assertTrue(
            any("skipped" in warning for warning in memory_response.json()["warnings"])
        )
        self.assertFalse(
            any("skipped" in warning for warning in technical_response.json()["warnings"])
        )

    def test_compact_conversation_supports_json_output_format(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "handoff", "format": "json", "persist": False},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["format"], "json")
        content = json.loads(payload["content"])
        self.assertEqual(content["summary"], payload["summary"])
        self.assertEqual(content["structured"], payload["structured"])

    def test_compact_conversation_rejects_unknown_output_format(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "handoff", "format": "pdf"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("Unsupported compact format", response.json()["detail"])

    def test_supersede_previous_replaces_active_pack_for_mode(self) -> None:
        first = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "technical"},
        )
        second = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "technical"},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        active_response = self.client.get("/conversations/conversation-compact/compact-packs?mode=technical")
        active_items = active_response.json()["items"]
        self.assertEqual(len(active_items), 1)
        self.assertEqual(active_items[0]["id"], second.json()["memory_id"])

        all_response = self.client.get(
            "/conversations/conversation-compact/compact-packs?mode=technical&include_superseded=true"
        )
        statuses = {item["id"]: item["status"] for item in all_response.json()["items"]}
        self.assertEqual(statuses[first.json()["memory_id"]], "superseded")
        self.assertEqual(statuses[second.json()["memory_id"]], "active")

    def test_preview_does_not_persist_memory(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "archive", "persist": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "preview")
        self.assertIsNone(response.json()["memory_id"])
        memories = self._run(self.context.memory_repo.list())
        self.assertEqual(memories, [])

    def test_compact_does_not_mutate_raw_messages(self) -> None:
        before = self._run(self.context.conversation_message_repo.list_by_conversation("conversation-compact"))

        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "handoff"},
        )

        self.assertEqual(response.status_code, 200)
        after = self._run(self.context.conversation_message_repo.list_by_conversation("conversation-compact"))
        self.assertEqual([item.id for item in after], [item.id for item in before])
        self.assertEqual([item.plain_text for item in after], [item.plain_text for item in before])

    def test_llm_strategy_persists_model_generated_context_pack(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={
                "mode": "handoff",
                "strategy": "llm",
                "model_profile_id": "profile-compact",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["summary"], "LLM compact summary.")
        self.assertEqual(payload["content"], "LLM compact content.")
        self.assertEqual(payload["structured"]["decisions"], ["Use memory_records."])
        self.assertEqual(_FakeCompactModelClient.seen_schema_names, ["conversation_context_pack"])
        self.assertIn("Render a handoff pack", _FakeCompactModelClient.seen_system_messages[0])
        self.assertIn("Mode render instructions: Render a handoff pack", _FakeCompactModelClient.seen_user_messages[0])

        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertEqual(memory.metadata["requested_strategy"], "llm")
        self.assertEqual(memory.metadata["generation_strategy"], "llm")

    def test_llm_strategy_records_governance_events_and_usage_snapshot(self) -> None:
        _FakeCompactModelClient.responses = [
            ModelResponse(
                content={
                    "summary": "LLM compact summary.",
                    "content": "LLM compact content.",
                    "structured": {
                        "goals": ["Build context packs."],
                        "facts": [],
                        "preferences": [],
                        "decisions": ["Use memory_records."],
                        "constraints": ["Keep raw messages."],
                        "commitments": [],
                        "open_questions": [],
                        "next_actions": ["Add prompt usage."],
                        "artifacts": ["docs/compact-tool.md"],
                        "risks": [],
                        "owners": [],
                        "expected_outputs": [],
                        "discarded_approaches": [],
                        "verification_needed": [],
                    },
                },
                provider="compact_fake",
                model="compact-fake-model",
                usage={"prompt_tokens": 31, "completion_tokens": 17, "total_tokens": 48},
            )
        ]

        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={
                "mode": "handoff",
                "strategy": "llm",
                "model_profile_id": "profile-compact",
            },
        )

        self.assertEqual(response.status_code, 200)
        audit_execution_id = ConversationAuditService(self.context).audit_execution_id("conversation-compact")
        events = self._run(self.context.execution_store.list_events(audit_execution_id))
        event_types = [event.event_type for event in events]
        self.assertIn(ExecutionEventType.CONTEXT_HEALTH_RECORDED, event_types)
        self.assertIn(ExecutionEventType.LLM_REQUEST_CREATED, event_types)
        self.assertIn(ExecutionEventType.LLM_RESPONSE_CREATED, event_types)
        self.assertIn(ExecutionEventType.TOKEN_USAGE_RECORDED, event_types)

        token_event = next(event for event in events if event.event_type == ExecutionEventType.TOKEN_USAGE_RECORDED)
        self.assertEqual(token_event.payload["call_kind"], "conversation_compaction")
        self.assertEqual(token_event.payload["compaction_mode"], "handoff")
        self.assertEqual(token_event.payload["usage"]["total_tokens"], 48)

        audit_execution = self._run(self.context.execution_store.get_execution(audit_execution_id))
        assert audit_execution is not None
        governance = audit_execution.metadata["runtime_governance"]
        self.assertEqual(governance["token_usage"]["total"]["total_tokens"], 48)
        self.assertEqual(
            governance["token_usage"]["by_model"]["compact_fake:compact-fake-model"]["completion_tokens"],
            17,
        )
        self.assertEqual(governance["context_health"]["last"]["status"], "unknown")

    def test_llm_strategy_uses_mode_specific_render_prompt(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={
                "mode": "workflow",
                "strategy": "llm",
                "model_profile_id": "profile-compact",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("owners or target agents", _FakeCompactModelClient.seen_system_messages[0])
        self.assertIn("expected outputs", _FakeCompactModelClient.seen_user_messages[0])

    def test_llm_strategy_regenerates_when_structured_response_is_invalid(self) -> None:
        _FakeCompactModelClient.responses = [
            ModelResponse(content="not structured json", provider="compact_fake", model="compact-fake-model"),
            ModelResponse(
                content={
                    "summary": "Repaired summary.",
                    "content": "Repaired content.",
                    "structured": {
                        "goals": ["Repair compact output."],
                        "facts": [],
                        "preferences": [],
                        "decisions": [],
                        "constraints": [],
                        "commitments": [],
                        "open_questions": [],
                        "next_actions": [],
                        "artifacts": [],
                        "risks": [],
                        "owners": [],
                        "expected_outputs": [],
                        "discarded_approaches": [],
                        "verification_needed": [],
                    },
                },
                provider="compact_fake",
                model="compact-fake-model",
            ),
        ]

        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={
                "mode": "handoff",
                "strategy": "llm",
                "model_profile_id": "profile-compact",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary"], "Repaired summary.")
        self.assertEqual(payload["content"], "Repaired content.")
        self.assertEqual(_FakeCompactModelClient.seen_schema_names, [
            "conversation_context_pack",
            "conversation_context_pack",
        ])
        self.assertIn("Regenerate only valid structured JSON", _FakeCompactModelClient.seen_user_messages[-1])
        self.assertEqual(payload["warnings"], [])

    def test_llm_strategy_falls_back_to_deterministic_when_model_fails(self) -> None:
        _FakeCompactModelClient.responses = [RuntimeError("model unavailable")]

        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={
                "mode": "handoff",
                "strategy": "llm",
                "model_profile_id": "profile-compact",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "created")
        self.assertIn("Current state", payload["content"])
        self.assertTrue(any("LLM compaction fallback used" in item for item in payload["warnings"]))

        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertEqual(memory.metadata["requested_strategy"], "llm")
        self.assertEqual(memory.metadata["generation_strategy"], "deterministic")

    def test_compact_api_can_be_disabled_by_feature_flag(self) -> None:
        with patch.dict(os.environ, {"MEMORY_CONTEXT_PACK_ENABLED": "false"}, clear=False):
            reset_settings_cache()
            create_response = self.client.post(
                "/conversations/conversation-compact/compact",
                json={"mode": "handoff"},
            )
            list_response = self.client.get("/conversations/conversation-compact/compact-packs")
            reset_settings_cache()

        self.assertEqual(create_response.status_code, 503)
        self.assertEqual(list_response.status_code, 503)
        self.assertIn("disabled", create_response.json()["detail"])

    def test_selected_source_range_compacts_explicit_message_window(self) -> None:
        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={
                "mode": "archive",
                "source_message_start_id": "message-2",
                "source_message_end_id": "message-3",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source_range"], "selected")
        self.assertEqual(payload["source_message_count"], 2)

        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertEqual(memory.metadata["source_range"], "selected")
        self.assertEqual(memory.metadata["source_message_start_id"], "message-2")
        self.assertEqual(memory.metadata["source_message_end_id"], "message-3")

    def test_idempotency_key_returns_existing_context_pack(self) -> None:
        first = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "handoff", "idempotency_key": "compact-request-1"},
        )
        second = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "handoff", "idempotency_key": "compact-request-1"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["status"], "created")
        self.assertEqual(second.json()["status"], "existing")
        self.assertEqual(second.json()["memory_id"], first.json()["memory_id"])

        memories = self._run(self.context.memory_repo.list())
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].metadata["idempotency_key"], "compact-request-1")

    def test_since_last_compact_source_range_uses_messages_after_previous_pack(self) -> None:
        first = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "handoff"},
        )
        self.assertEqual(first.status_code, 200)
        self._run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-4",
                    conversation_id="conversation-compact",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Incremental follow-up: add source range selection.",
                    content={"text": "Incremental follow-up: add source range selection."},
                )
            )
        )

        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "handoff", "source_range": "since_last_compact"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source_range"], "since_last_compact")
        self.assertEqual(payload["source_message_count"], 1)

        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertEqual(memory.metadata["source_range"], "since_last_compact")
        self.assertEqual(memory.metadata["source_message_start_id"], "message-4")
        self.assertEqual(memory.metadata["source_message_end_id"], "message-4")

    def test_older_than_recent_source_range_leaves_recent_messages_raw(self) -> None:
        for index in (4, 5):
            self._run(
                self.context.conversation_message_repo.create(
                    ConversationMessage(
                        id=f"message-{index}",
                        conversation_id="conversation-compact",
                        role=ConversationRole.USER,
                        message_type=ConversationMessageType.USER_TEXT,
                        plain_text=f"Recent message {index}",
                        content={"text": f"Recent message {index}"},
                    )
                )
            )

        response = self.client.post(
            "/conversations/conversation-compact/compact",
            json={"mode": "brief", "source_range": "older_than_recent", "recent_message_limit": 2},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source_range"], "older_than_recent")
        self.assertEqual(payload["source_message_count"], 3)

        memory = self._run(self.context.memory_repo.get(payload["memory_id"]))
        assert memory is not None
        self.assertEqual(memory.metadata["source_range"], "older_than_recent")
        self.assertEqual(memory.metadata["recent_message_limit"], 2)
        self.assertEqual(memory.metadata["source_message_end_id"], "message-3")


if __name__ == "__main__":
    unittest.main()
