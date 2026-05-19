from __future__ import annotations

import asyncio
import json
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.routes.conversations.core import create_conversations_router
from app.domain import ConversationMessage, MCPExposureSettings, ModelProfileDefinition, SecuritySettings, \
    ToolDefinition, ToolImplementationReference, ToolType
from app.llm.base import ModelResponse, ModelToolCall
from app.llm.registry import LLMEnvironmentConfig
from app.services.conversations import ConversationService
from app.services.main_agent_setup import MainAgentSetupConfig, MainAgentSetupService


class _FakeModelClient:
    provider_key = "fake"
    responses: list[ModelResponse] = []

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        if _FakeModelClient.responses:
            return _FakeModelClient.responses.pop(0)
        return ModelResponse(content="stream reply", provider="fake", model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content={"ok": True}, provider="fake", model=self.profile.model)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "unused"

    def count_tokens(self, messages, **kwargs):
        return 0

    def health_check(self):
        return {"ok": True}


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


def _parse_sse_payload(chunk: str) -> tuple[str | None, dict]:
    event_name = None
    data_lines: list[str] = []
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event_name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))
    return event_name, json.loads("\n".join(data_lines))


class ConversationStreamingServiceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.context = create_test_api_context()
        cls.context.llm_provider_registry.register("fake", lambda profile, env: _FakeModelClient(profile, env))
        asyncio.run(
            cls.context.model_profile_repo.save(
                ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
            )
        )
        asyncio.run(
            MainAgentSetupService(cls.context).create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_description="Configured for tests.",
                    agent_instructions="Answer briefly.",
                    model_profile_id="profile-fake",
                    profile_id="main-agent-profile",
                )
            )
        )
        cls.service = ConversationService(cls.context)

    async def asyncSetUp(self) -> None:
        self.context = self.__class__.context
        self.service = self.__class__.service
        self.conversation_id = f"conversation-{self._testMethodName}"
        _FakeModelClient.responses = []
        await self.service.create_conversation(
            {
                "id": self.conversation_id,
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

    async def _assign_agent_tool(self, tool: ToolDefinition) -> None:
        await self.context.tool_repo.create(tool)
        profile = await self.context.main_agent_profile_repo.get("main-agent-profile")
        assert profile is not None
        agent = await self.context.agent_repo.get(profile.agent_id)
        assert agent is not None
        await self.context.agent_repo.update(agent.id, {"tool_ids": [*agent.tool_ids, tool.id]})

    async def test_service_stream_replays_history_and_respects_cursor(self) -> None:
        await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-1",
                conversation_id=self.conversation_id,
                role="user",
                message_type="user_text",
                plain_text="hello",
                content={"text": "hello"},
            )
        )
        await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-2",
                conversation_id=self.conversation_id,
                role="assistant",
                message_type="assistant_text",
                plain_text="hi",
                content={"text": "hi"},
            )
        )

        stream = self.service.stream_conversation_events(self.conversation_id, _ConnectedRequest(), after="message-1")
        first = await anext(stream)
        await stream.aclose()

        event_name, payload = _parse_sse_payload(first)
        self.assertEqual(event_name, "message.created")
        self.assertEqual(payload["message"]["id"], "message-2")

    async def test_service_stream_emits_live_message_event(self) -> None:
        stream = self.service.stream_conversation_events(self.conversation_id, _ConnectedRequest(),
                                                         idle_timeout_seconds=1.0)

        async def produce_message() -> None:
            await asyncio.sleep(0.05)
            await self.service.post_message(
                self.conversation_id,
                {
                    "id": "message-live-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "stream me",
                    "content": {"text": "stream me"},
                },
            )

        producer = asyncio.create_task(produce_message())
        first = await anext(stream)
        await producer
        await stream.aclose()

        event_name, payload = _parse_sse_payload(first)
        self.assertEqual(event_name, "message.created")
        self.assertEqual(payload["message"]["id"], "message-live-1")

    async def test_service_stream_emits_live_approval_event(self) -> None:
        stream = self.service.stream_conversation_events(self.conversation_id, _ConnectedRequest(),
                                                         idle_timeout_seconds=1.0)

        async def produce_approval() -> None:
            await asyncio.sleep(0.05)
            await self.service.post_message(
                self.conversation_id,
                {
                    "message": {
                        "id": "message-approval-1",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Request approval",
                        "content": {
                            "text": "Request approval",
                            "approval_request": {
                                "approval_type": "workflow_execution",
                                "target_type": "workflow",
                                "target_id": "workflow-1",
                                "summary": "Run workflow-1",
                            },
                        },
                    }
                },
            )

        producer = asyncio.create_task(produce_approval())
        seen_events: list[str] = []
        approval_payload = None
        for _ in range(4):
            chunk = await anext(stream)
            event_name, payload = _parse_sse_payload(chunk)
            seen_events.append(event_name or "")
            if event_name == "approval.requested":
                approval_payload = payload
                break
        await producer
        await stream.aclose()

        self.assertIn("approval.requested", seen_events)
        assert approval_payload is not None
        self.assertEqual(approval_payload["approval"]["status"], "pending")

    async def test_service_stream_emits_live_tool_approval_event(self) -> None:
        await self._assign_agent_tool(
            ToolDefinition(
                id="tool-click",
                name="click",
                description="Computer use click",
                tool_type=ToolType.PYTHON_FUNCTION,
                input_schema={
                    "type": "object",
                    "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
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
        _FakeModelClient.responses = [
            ModelResponse(
                content=None,
                tool_calls=[ModelToolCall(id="stream-tool-call-1", name="click", arguments={"x": 11, "y": 22})],
                provider="fake",
                model="fake-model",
            )
        ]
        stream = self.service.stream_conversation_events(self.conversation_id, _ConnectedRequest(),
                                                         idle_timeout_seconds=1.0)

        async def produce_message() -> None:
            await asyncio.sleep(0.05)
            await self.service.post_message(
                self.conversation_id,
                {
                    "message": {
                        "id": "message-tool-approval-1",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Click there",
                        "content": {"text": "Click there"},
                    }
                },
            )

        producer = asyncio.create_task(produce_message())
        approval_payload = None
        for _ in range(6):
            chunk = await anext(stream)
            event_name, payload = _parse_sse_payload(chunk)
            if event_name == "approval.requested":
                approval_payload = payload
                break
        await producer
        await stream.aclose()

        assert approval_payload is not None
        self.assertEqual(approval_payload["approval"]["approval_type"], "tool_execute")
        self.assertEqual(approval_payload["approval"]["target_type"], "tool")

    async def test_service_stream_emits_idle_event(self) -> None:
        stream = self.service.stream_conversation_events(self.conversation_id, _ConnectedRequest(),
                                                         idle_timeout_seconds=0.01)
        first = await anext(stream)
        await stream.aclose()

        event_name, payload = _parse_sse_payload(first)
        self.assertEqual(event_name, "conversation.idle")
        self.assertEqual(payload["conversation_id"], self.conversation_id)

    async def test_api_stream_returns_404_for_missing_conversation(self) -> None:
        app = FastAPI()
        app.include_router(create_conversations_router(self.context))
        with TestClient(app) as client:
            response = client.get("/conversations/missing/stream")

        self.assertEqual(response.status_code, 404)
