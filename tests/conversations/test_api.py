from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.domain import ConversationMessage, ConversationMessageType, ConversationRole, MCPExposureSettings, \
    ModelProfileDefinition, SecuritySettings, ToolDefinition, ToolImplementationReference, ToolType
from app.llm.base import ModelResponse, ModelToolCall
from app.llm.registry import LLMEnvironmentConfig
from app.services.conversations.audit import ConversationAuditService
from app.services.main_agent_setup.service import MainAgentSetupConfig, MainAgentSetupService


class _FakeModelClient:
    provider_key = "fake"
    responses: list[ModelResponse] = []
    seen_profile_ids: list[str] = []
    seen_message_contents: list[list[str]] = []

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        _FakeModelClient.seen_profile_ids.append(self.profile.id)
        _FakeModelClient.seen_message_contents.append([str(item.content) for item in messages])
        if _FakeModelClient.responses:
            return _FakeModelClient.responses.pop(0)
        user_message = next((item.content for item in reversed(messages) if item.role == "user"), "hello")
        return ModelResponse(content=f"Assistant reply: {user_message}", provider="fake", model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content={"ok": True}, provider="fake", model=self.profile.model)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "unused"

    def count_tokens(self, messages, **kwargs):
        return 0

    def health_check(self):
        return {"ok": True}


class ConversationsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake", lambda profile, env: _FakeModelClient(profile, env))
        self._run(self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")))
        self._run(
            MainAgentSetupService(self.context).create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_description="Configured for tests.",
                    agent_instructions="Answer briefly.",
                    model_profile_id="profile-fake",
                    profile_id="main-agent-profile",
                )
            )
        )
        _FakeModelClient.responses = []
        _FakeModelClient.seen_profile_ids = []
        _FakeModelClient.seen_message_contents = []
        self.client = TestClient(create_app(context=self.context))

    def _run(self, awaitable):
        import asyncio

        return asyncio.run(awaitable)

    def test_create_get_list_patch_and_message_round_trip(self) -> None:
        profile_response = self.client.get("/conversations/main-agent-profile")
        self.assertEqual(profile_response.status_code, 200)
        self.assertEqual(profile_response.json()["id"], "main-agent-profile")

        create_response = self.client.post(
            "/conversations",
            json={
                "id": "conversation-1",
                "title": None,
                "created_by_user_id": "user-1",
                "channel_type": "api",
                "metadata": {"source": "test"},
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertIsNone(create_response.json()["title"])
        self.assertEqual(create_response.json()["main_agent_profile_id"], "main-agent-profile")

        append_response = self.client.post(
            "/conversations/conversation-1/messages",
            json={
                "id": "message-1",
                "role": "user",
                "message_type": "user_text",
                "plain_text": "Hello",
                "content": {"text": "Hello"},
            },
        )
        self.assertEqual(append_response.status_code, 200)
        self.assertEqual(append_response.json()["message"]["conversation_id"], "conversation-1")
        self.assertEqual(append_response.json()["assistant_message"]["role"], "assistant")
        self.assertEqual(append_response.json()["assistant_message"]["plain_text"], "Assistant reply: Hello")

        list_response = self.client.get("/conversations")
        self.assertEqual(list_response.status_code, 200)
        listed = list_response.json()["items"]
        self.assertIn("conversation-1", {item["id"] for item in listed})
        self.assertIn("main-agent-profile-monitor", {item["id"] for item in listed})

        get_response = self.client.get("/conversations/conversation-1")
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()["created_by_user_id"], "user-1")
        self.assertEqual(get_response.json()["title"], "Hello")

        patch_response = self.client.patch("/conversations/conversation-1", json={"title": "Renamed thread"})
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.json()["title"], "Renamed thread")

        messages_response = self.client.get("/conversations/conversation-1/messages")
        self.assertEqual(messages_response.status_code, 200)
        self.assertEqual(len(messages_response.json()["items"]), 2)
        self.assertEqual(messages_response.json()["items"][0]["plain_text"], "Hello")
        self.assertEqual(messages_response.json()["items"][1]["plain_text"], "Assistant reply: Hello")

    def test_active_main_agent_profile_can_switch_default_model(self) -> None:
        self._run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(id="profile-ollama", name="Ollama", provider="ollama", model="llama3:8b")
            )
        )

        response = self.client.patch(
            "/conversations/main-agent-profile",
            json={"default_model_profile_id": "profile-ollama"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["default_model_profile_id"], "profile-ollama")

        profile = self._run(self.context.main_agent_profile_repo.get("main-agent-profile"))
        assert profile is not None
        self.assertEqual(profile.default_model_profile_id, "profile-ollama")

        agent = self._run(self.context.agent_repo.get(profile.agent_id))
        assert agent is not None
        self.assertEqual(agent.model_profile_id, "profile-ollama")

        workflow = self._run(self.context.workflow_repo.get(profile.default_workflow_id))
        assert workflow is not None
        self.assertEqual(workflow.agent_definitions[0].model_profile_id, "profile-ollama")

    def test_context_usage_reports_resolved_model_window_and_alert_status(self) -> None:
        self._run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(
                    id="profile-small-context",
                    name="Small Context",
                    provider="fake",
                    model="small-context-model",
                    context_window=100,
                    max_tokens=20,
                )
            )
        )
        patch_response = self.client.patch(
            "/conversations/main-agent-profile",
            json={"default_model_profile_id": "profile-small-context"},
        )
        self.assertEqual(patch_response.status_code, 200)

        create_response = self.client.post(
            "/conversations",
            json={
                "id": "conversation-context-usage",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self._run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="context-usage-message-1",
                    conversation_id="conversation-context-usage",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Please retain this context. " * 20,
                    content={"text": "Please retain this context. " * 20},
                )
            )
        )

        usage_response = self.client.get("/conversations/conversation-context-usage/context-usage")
        self.assertEqual(usage_response.status_code, 200)
        payload = usage_response.json()
        self.assertEqual(payload["model_profile"]["id"], "profile-small-context")
        self.assertEqual(payload["context_window"], 100)
        self.assertGreater(payload["estimated_context_tokens"], 0)
        self.assertEqual(payload["status"], "overflow")
        self.assertTrue(payload["compact_recommended"])

    def test_direct_reply_records_governance_events_and_usage_snapshot(self) -> None:
        _FakeModelClient.responses = [
            ModelResponse(
                content="Governed reply",
                usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                provider="fake",
                model="fake-model",
                latency_ms=3,
            )
        ]
        create_response = self.client.post(
            "/conversations",
            json={
                "id": "conversation-governance",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            },
        )
        self.assertEqual(create_response.status_code, 200)

        append_response = self.client.post(
            "/conversations/conversation-governance/messages",
            json={
                "id": "message-governance",
                "role": "user",
                "message_type": "user_text",
                "plain_text": "Track governance",
                "content": {"text": "Track governance"},
            },
        )
        self.assertEqual(append_response.status_code, 200)
        self.assertEqual(append_response.json()["assistant_message"]["plain_text"], "Governed reply")

        audit_execution_id = ConversationAuditService(self.context).audit_execution_id("conversation-governance")
        events = self._run(self.context.execution_store.list_events(audit_execution_id))
        event_types = [event.event_type.value for event in events]

        self.assertIn("context.health.recorded", event_types)
        self.assertIn("llm.request.created", event_types)
        self.assertIn("llm.response.created", event_types)
        self.assertIn("token.usage.recorded", event_types)
        token_events = [event for event in events if event.event_type.value == "token.usage.recorded"]
        self.assertEqual(token_events[-1].metrics["total_tokens"], 18)
        self.assertEqual(token_events[-1].payload["call_kind"], "direct_reply")

        audit_execution = self._run(self.context.execution_store.get_execution(audit_execution_id))
        assert audit_execution is not None
        governance = audit_execution.metadata["runtime_governance"]
        self.assertEqual(governance["token_usage"]["total"]["total_tokens"], 18)
        self.assertGreater(governance["context_health"]["last"]["estimated_total_context_tokens"], 0)

    def test_main_agent_chat_uses_agent_equipped_model_profile(self) -> None:
        self._run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(id="profile-equipped", name="Equipped", provider="fake", model="equipped-model")
            )
        )
        profile = self._run(self.context.main_agent_profile_repo.get("main-agent-profile"))
        assert profile is not None
        agent = self._run(self.context.agent_repo.get(profile.agent_id))
        assert agent is not None
        self._run(self.context.agent_repo.update(agent.id, {"model_profile_id": "profile-equipped"}))

        create_response = self.client.post(
            "/conversations",
            json={
                "id": "conversation-equipped",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            },
        )
        self.assertEqual(create_response.status_code, 200)

        append_response = self.client.post(
            "/conversations/conversation-equipped/messages",
            json={
                "id": "message-equipped",
                "role": "user",
                "message_type": "user_text",
                "plain_text": "Hello from equipped model",
                "content": {"text": "Hello from equipped model"},
            },
        )
        self.assertEqual(append_response.status_code, 200)
        self.assertEqual(_FakeModelClient.seen_profile_ids[-1], "profile-equipped")

    def test_main_agent_chat_accepts_hyphenated_provider_alias(self) -> None:
        self.context.llm_provider_registry.register(
            "openai_codex",
            lambda profile, env: _FakeModelClient(profile, env),
        )
        self._run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(
                    id="profile-codex",
                    name="Codex",
                    provider="openai-codex",
                    model="gpt-5.3-codex",
                    base_url="https://codex-api.openai.com/v1",
                )
            )
        )
        profile = self._run(self.context.main_agent_profile_repo.get("main-agent-profile"))
        assert profile is not None
        agent = self._run(self.context.agent_repo.get(profile.agent_id))
        assert agent is not None
        self._run(self.context.agent_repo.update(agent.id, {"model_profile_id": "profile-codex"}))

        create_response = self.client.post(
            "/conversations",
            json={
                "id": "conversation-codex",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            },
        )
        self.assertEqual(create_response.status_code, 200)

        append_response = self.client.post(
            "/conversations/conversation-codex/messages",
            json={
                "id": "message-codex",
                "role": "user",
                "message_type": "user_text",
                "plain_text": "Hello from codex",
                "content": {"text": "Hello from codex"},
            },
        )
        self.assertEqual(append_response.status_code, 200)
        self.assertEqual(_FakeModelClient.seen_profile_ids[-1], "profile-codex")

    def test_main_agent_chat_filters_synthetic_failure_history(self) -> None:
        create_response = self.client.post(
            "/conversations",
            json={
                "id": "conversation-synthetic-history",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self._run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    conversation_id="conversation-synthetic-history",
                    role=ConversationRole.ASSISTANT,
                    message_type=ConversationMessageType.ASSISTANT_TEXT,
                    plain_text=(
                        "I could not reach the configured LLM for this main agent. "
                        "Model profile 'profile-fake' failed with: timed out"
                    ),
                    content={
                        "text": (
                            "I could not reach the configured LLM for this main agent. "
                            "Model profile 'profile-fake' failed with: timed out"
                        )
                    },
                )
            )
        )
        self._run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    conversation_id="conversation-synthetic-history",
                    role=ConversationRole.ASSISTANT,
                    message_type=ConversationMessageType.ASSISTANT_TEXT,
                    plain_text="I received your message: old fallback",
                    content={"text": "I received your message: old fallback"},
                )
            )
        )

        append_response = self.client.post(
            "/conversations/conversation-synthetic-history/messages",
            json={
                "id": "message-after-synthetic-history",
                "role": "user",
                "message_type": "user_text",
                "plain_text": "hi",
                "content": {"text": "hi"},
            },
        )

        self.assertEqual(append_response.status_code, 200)
        seen = _FakeModelClient.seen_message_contents[-1]
        self.assertIn("hi", seen)
        self.assertFalse(any("could not reach the configured LLM" in item for item in seen))
        self.assertFalse(any(item.startswith("I received your message:") for item in seen))

    def test_appending_message_requires_existing_conversation(self) -> None:
        response = self.client.post(
            "/conversations/missing/messages",
            json={
                "id": "message-1",
                "role": "user",
                "message_type": "user_text",
                "content": {"text": "Hello"},
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_conversation_api_supports_tool_execution_approval_flow(self) -> None:
        self._run(
            self.context.tool_repo.create(
                ToolDefinition(
                    id="tool-click",
                    name="click",
                    description="Computer use click",
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
                        config={"tool_family": "computer_use", "canonical_tool_name": "click"},
                    ),
                    security=SecuritySettings(requires_approval=True),
                    mcp_exposure=MCPExposureSettings(),
                    tags=["computer_use"],
                )
            )
        )
        profile = self._run(self.context.main_agent_profile_repo.get("main-agent-profile"))
        assert profile is not None
        agent = self._run(self.context.agent_repo.get(profile.agent_id))
        assert agent is not None
        self._run(self.context.agent_repo.update(agent.id, {"tool_ids": [*agent.tool_ids, "tool-click"]}))

        create_response = self.client.post(
            "/conversations",
            json={
                "id": "conversation-tool-approval-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            },
        )
        self.assertEqual(create_response.status_code, 200)

        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[ModelToolCall(id="api-tool-call-1", name="click", arguments={"text": "clicked"})],
                provider="fake",
                model="fake-model",
            ),
            ModelResponse(content="Click completed.", provider="fake", model="fake-model"),
        ]

        requested = self.client.post(
            "/conversations/conversation-tool-approval-1/messages",
            json={
                "message": {
                    "id": "message-tool-approval-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Click there",
                    "content": {"text": "Click there"},
                }
            },
        )
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(requested.json()["assistant_message"]["message_type"], "approval_request")
        self.assertEqual(requested.json()["approval_request"]["approval_type"], "tool_execute")

        approved = self.client.post(
            f"/conversations/approval-requests/{requested.json()['approval_request']['id']}/approve",
            json={"user_id": "user-1", "reason": "Approved"},
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["approval_request"]["status"], "approved")
        self.assertEqual(approved.json()["tool_result"], {"echo": "clicked"})
        self.assertEqual(approved.json()["assistant_message"]["plain_text"], "Click completed.")


if __name__ == "__main__":
    unittest.main()
