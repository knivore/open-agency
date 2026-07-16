from __future__ import annotations

import asyncio
import json
import os
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from pydantic import ValidationError

from app.api.context import create_test_api_context
from app.core.config import Settings, reset_settings_cache
from app.db.repositories.domain_sql import SQLToolRepository
from app.domain import (
    AgentDefinition,
    ContextScope,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    ExecutionMode,
    FrameworkHints,
    RequestComplexity,
    RoutingDecision,
    SecuritySettings,
    ToolDefinition,
    ToolImplementationReference,
    ToolRoutingMetadata,
    ModelProfileDefinition,
)
from app.llm.base import ModelResponse, ModelToolCall
from app.services.conversations.intent_routing import (
    DeterministicFastPathClassifier,
    LightweightIntentRouter,
    ROUTING_PATTERN_CACHE,
    RoutingPatternCache,
    RoutingPolicy,
    redact_routing_text,
    routing_cache_key,
)
from app.services.conversations.core import ConversationService
from app.services.conversations.policy import MainAgentPolicyService
from app.services.agent_tools import AgentToolResolver
from app.services.agent_tools import (
    memory_system_tool_definitions,
    tool_management_system_tool_definitions,
    workflow_system_tool_definitions,
)
from app.services.main_agent_setup.service import MainAgentSetupConfig, MainAgentSetupService
from app.tools.names import tool_call_name
from app.tools.routing import compact_tool_groups, resolve_tool_groups, tool_catalogue_version


def _tool(tool_id: str, *, read_only: bool) -> ToolDefinition:
    return ToolDefinition(
        id=tool_id,
        name=tool_id.replace(".", "_"),
        description=f"Test tool {tool_id}",
        input_schema={"type": "object", "properties": {}},
        implementation=ToolImplementationReference(target="tests.native_test_tools"),
        security=SecuritySettings(read_only=read_only),
    )


class RoutingDecisionTests(unittest.TestCase):
    def test_direct_response_rejects_tools(self) -> None:
        with self.assertRaises(ValidationError):
            RoutingDecision(
                intent="hello",
                complexity=RequestComplexity.TRIVIAL,
                execution_mode=ExecutionMode.DIRECT_RESPONSE,
                tool_groups=["memory.read"],
                confidence=1.0,
                reason_code="test",
            )

    def test_selected_tools_requires_a_group(self) -> None:
        with self.assertRaises(ValidationError):
            RoutingDecision(
                intent="memory_lookup",
                complexity=RequestComplexity.SIMPLE,
                execution_mode=ExecutionMode.SELECTED_TOOLS,
                confidence=0.9,
                reason_code="test",
            )


class FastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = DeterministicFastPathClassifier()

    def test_greeting_is_a_no_tool_direct_response(self) -> None:
        result = self.classifier.evaluate("Hello!")
        self.assertTrue(result.matched)
        self.assertEqual(result.rule_code, "greeting")
        self.assertEqual(result.decision.execution_mode, ExecutionMode.DIRECT_RESPONSE)
        self.assertEqual(result.decision.tool_groups, [])

    def test_previous_answer_edit_uses_recent_context(self) -> None:
        result = self.classifier.evaluate("Make your previous answer shorter")
        self.assertTrue(result.matched)
        self.assertEqual(result.decision.context_scope, ContextScope.RECENT_TURNS)

    def test_explicit_answer_continuation_uses_recent_context(self) -> None:
        result = self.classifier.evaluate("Continue your previous explanation")
        self.assertTrue(result.matched)
        self.assertEqual(result.rule_code, "continuation")
        self.assertEqual(result.decision.context_scope, ContextScope.RECENT_TURNS)

    def test_short_tool_request_falls_through(self) -> None:
        self.assertFalse(self.classifier.evaluate("Run ls").matched)

    def test_rules_can_be_disabled(self) -> None:
        self.assertFalse(DeterministicFastPathClassifier(set()).evaluate("Hello").matched)

    def test_router_text_redacts_common_inline_credentials(self) -> None:
        redacted = redact_routing_text("Use api_key=sk-example and Authorization: Bearer abc.def")
        self.assertNotIn("sk-example", redacted)
        self.assertNotIn("abc.def", redacted)
        self.assertIn("[REDACTED]", redacted)


class ToolCatalogueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = [
            _tool("agency.memory.list", read_only=True),
            _tool("agency.memory.update", read_only=False),
            _tool("agency.workflow.list", read_only=True),
        ]

    def test_compact_groups_do_not_include_input_schemas(self) -> None:
        groups = compact_tool_groups(self.tools)
        self.assertEqual([group.id for group in groups], ["memory.read", "memory.write", "workflow.read"])
        self.assertNotIn("properties", groups[0].model_dump())

    def test_group_resolution_is_stable_and_deduplicated(self) -> None:
        resolved = resolve_tool_groups(self.tools, ["memory.read", "memory.read"])
        self.assertEqual([tool.id for tool in resolved], ["agency.memory.list"])

    def test_tool_in_multiple_groups_is_emitted_once(self) -> None:
        multi_group = self.tools[0].model_copy(
            update={
                "routing": ToolRoutingMetadata(
                    group="memory.read",
                    additional_groups=["knowledge.read"],
                    short_description="Read memory knowledge",
                    read_only=True,
                    risk_level="read",
                )
            }
        )
        groups = compact_tool_groups([multi_group])
        self.assertEqual([group.id for group in groups], ["knowledge.read", "memory.read"])
        resolved = resolve_tool_groups([multi_group], ["memory.read", "knowledge.read"])
        self.assertEqual([tool.id for tool in resolved], [multi_group.id])

    def test_catalogue_version_changes_with_routing_exposure(self) -> None:
        original = tool_catalogue_version(self.tools)
        changed = tool_catalogue_version([self.tools[0], self.tools[2]])
        self.assertNotEqual(original, changed)

    def test_disabled_routing_metadata_is_not_exposed(self) -> None:
        disabled = self.tools[0].model_copy(
            update={
                "routing": ToolRoutingMetadata(
                    group="memory.read",
                    short_description="Read memory",
                    read_only=True,
                    risk_level="read",
                    enabled=False,
                )
            }
        )
        self.assertEqual(compact_tool_groups([disabled]), [])

    def test_explicit_routing_metadata_survives_sql_mapping(self) -> None:
        routed = self.tools[0].model_copy(
            update={
                "routing": ToolRoutingMetadata(
                    group="memory.read",
                    short_description="Read memory",
                    read_only=True,
                    risk_level="read",
                )
            }
        )
        repository = SQLToolRepository(session_factory=None)
        round_trip = repository._to_domain(repository._to_orm(routed))
        self.assertEqual(round_trip.routing, routed.routing)

    def test_explicit_metadata_cannot_downgrade_tool_security(self) -> None:
        write_tool = self.tools[1].model_copy(
            update={
                "routing": ToolRoutingMetadata(
                    group="memory.read",
                    short_description="Misclassified write",
                    read_only=True,
                    risk_level="read",
                )
            }
        )
        descriptor = compact_tool_groups([write_tool])[0]
        self.assertEqual(descriptor.risk, "write")


class ContextScopeTests(unittest.TestCase):
    def test_recent_history_is_capped_by_estimated_tokens(self) -> None:
        history = [
            ConversationMessage(
                id=f"message-{index}",
                conversation_id="conversation-1",
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text=str(index) + ("x" * 400),
            )
            for index in range(3)
        ]
        decision = RoutingDecision(
            intent="follow_up",
            complexity=RequestComplexity.SIMPLE,
            execution_mode=ExecutionMode.DIRECT_RESPONSE,
            context_scope=ContextScope.RECENT_TURNS,
            confidence=0.9,
            reason_code="follow_up",
        )
        selected = ConversationService._history_for_routing_scope(history, decision, token_budget=120)
        self.assertEqual([message.id for message in selected], ["message-2"])


class UserToolVisibilityTests(unittest.TestCase):
    def test_optional_user_allowlist_is_enforced_after_agent_allowlist(self) -> None:
        context = create_test_api_context()
        tool = _tool("agency.memory.list", read_only=True).model_copy(
            update={"framework_hints": FrameworkHints(metadata={"allowed_user_ids": ["user-a"]})}
        )
        policy = MainAgentPolicyService(context)
        self.assertTrue(policy.tool_is_visible_to_user(tool, "user-a"))
        self.assertFalse(policy.tool_is_visible_to_user(tool, "user-b"))
        self.assertFalse(policy.tool_is_visible_to_user(tool, None))


class RoutingPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.groups = compact_tool_groups([
            _tool("agency.memory.list", read_only=True),
            _tool("agency.memory.update", read_only=False),
        ])
        self.settings = Settings(
            MAIN_AGENT_ROUTER_DIRECT_RESPONSE_ENABLED=True,
            MAIN_AGENT_ROUTER_MIN_CONFIDENCE=0.7,
            MAIN_AGENT_ROUTER_MAX_TOOL_GROUPS=2,
        )
        self.policy = RoutingPolicy(self.settings)

    def test_low_confidence_uses_full_agent_fallback(self) -> None:
        result = self.policy.apply(
            RoutingDecision(
                intent="memory_lookup",
                complexity=RequestComplexity.SIMPLE,
                execution_mode=ExecutionMode.SELECTED_TOOLS,
                tool_groups=["memory.read"],
                confidence=0.5,
                reason_code="test",
            ),
            self.groups,
        )
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.decision.execution_mode, ExecutionMode.FULL_AGENT)

    def test_write_groups_are_not_selectively_enabled_by_default(self) -> None:
        result = self.policy.apply(
            RoutingDecision(
                intent="memory_update",
                complexity=RequestComplexity.SIMPLE,
                execution_mode=ExecutionMode.SELECTED_TOOLS,
                tool_groups=["memory.write"],
                confidence=0.9,
                reason_code="test",
            ),
            self.groups,
        )
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.reason_code, "selective_write_disabled")

    def test_unknown_group_is_rejected(self) -> None:
        result = self.policy.apply(
            RoutingDecision(
                intent="unknown",
                complexity=RequestComplexity.SIMPLE,
                execution_mode=ExecutionMode.SELECTED_TOOLS,
                tool_groups=["not.a.group"],
                confidence=0.9,
                reason_code="test",
            ),
            self.groups,
        )
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.reason_code, "unknown_tool_group")

    def test_group_limit_is_enforced(self) -> None:
        policy = RoutingPolicy(
            Settings(MAIN_AGENT_ROUTER_MAX_TOOL_GROUPS=1, MAIN_AGENT_ROUTER_MIN_CONFIDENCE=0.7)
        )
        result = policy.apply(
            RoutingDecision(
                intent="multi_read",
                complexity=RequestComplexity.COMPLEX,
                execution_mode=ExecutionMode.SELECTED_TOOLS,
                tool_groups=["memory.read", "memory.write"],
                confidence=0.9,
                reason_code="test",
            ),
            self.groups,
        )
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.reason_code, "tool_group_limit")

    def test_write_group_requires_explicit_rollout_flag(self) -> None:
        policy = RoutingPolicy(
            Settings(
                MAIN_AGENT_ROUTER_SELECTIVE_WRITE_TOOLS_ENABLED=True,
                MAIN_AGENT_ROUTER_MAX_TOOL_ITERATIONS=2,
                MAIN_AGENT_ROUTER_MAX_TOKEN_BUDGET=1000,
            )
        )
        result = policy.apply(
            RoutingDecision(
                intent="memory_update",
                complexity=RequestComplexity.SIMPLE,
                execution_mode=ExecutionMode.SELECTED_TOOLS,
                tool_groups=["memory.write"],
                confidence=0.9,
                reason_code="memory_update",
                max_tool_iterations=10,
                token_budget=10000,
            ),
            self.groups,
        )
        self.assertFalse(result.fallback_used)
        self.assertEqual(result.decision.max_tool_iterations, 2)
        self.assertEqual(result.decision.token_budget, 1000)

    def test_configured_read_only_safe_fallback_is_used(self) -> None:
        policy = RoutingPolicy(Settings(MAIN_AGENT_ROUTER_SAFE_FALLBACK_GROUPS="memory.read"))
        result = policy.safe_fallback("router_timeout", self.groups)
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.decision.execution_mode, ExecutionMode.SELECTED_TOOLS)
        self.assertEqual(result.decision.tool_groups, ["memory.read"])


@dataclass
class _StructuredResponse:
    content: object


class _FakeStructuredClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    async def agenerate_structured(self, messages, **kwargs):
        self.requests.append({"messages": messages, "kwargs": kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _StructuredResponse(response)


class _SlowStructuredClient:
    async def agenerate_structured(self, _messages, **_kwargs):
        await asyncio.sleep(0.2)
        return _StructuredResponse({})


class LightweightIntentRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(MAIN_AGENT_ROUTER_TIMEOUT_MS=1000)
        self.groups = compact_tool_groups([_tool("agency.memory.list", read_only=True)])

    def test_router_uses_compact_groups_and_validates_response(self) -> None:
        client = _FakeStructuredClient([
            {
                "intent": "memory_lookup",
                "complexity": "simple",
                "execution_mode": "selected_tools",
                "tool_groups": ["memory.read"],
                "confidence": 0.9,
                "reason_code": "memory_lookup",
            }
        ])
        result = asyncio.run(
            LightweightIntentRouter(self.settings).route(
                client=client,
                message="What did I decide?",
                available_groups=self.groups,
            )
        )
        self.assertEqual(result.decision.tool_groups, ["memory.read"])
        prompt = client.requests[0]["messages"][1].content
        self.assertIn('"tool_groups"', prompt)
        self.assertNotIn('"properties"', prompt)

    def test_router_repairs_one_malformed_response_then_falls_back(self) -> None:
        client = _FakeStructuredClient([{"not": "a decision"}, {"still": "invalid"}])
        result = asyncio.run(
            LightweightIntentRouter(self.settings).route(
                client=client,
                message="Anything",
                available_groups=self.groups,
            )
        )
        self.assertIsNone(result.decision)
        self.assertEqual(result.failure_code, "router_invalid_output")
        self.assertTrue(result.repaired)

    def test_router_timeout_repairs_once_then_returns_safe_failure(self) -> None:
        settings = Settings(MAIN_AGENT_ROUTER_TIMEOUT_MS=100)
        result = asyncio.run(
            LightweightIntentRouter(settings).route(
                client=_SlowStructuredClient(),
                message="Inspect the project",
                available_groups=self.groups,
            )
        )
        self.assertIsNone(result.decision)
        self.assertEqual(result.failure_code, "router_timeout")
        self.assertTrue(result.repaired)


class RoutingPatternCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = RoutingPatternCache()
        self.groups = compact_tool_groups([
            _tool("agency.memory.list", read_only=True),
            _tool("agency.memory.update", read_only=False),
        ])

    def test_key_changes_with_catalogue_and_permissions(self) -> None:
        base = dict(
            message="Find my decision",
            router_prompt_version="v1",
            router_provider="fake",
            router_model="small",
            catalogue_version="catalogue-a",
            specialist_version="specialists-a",
            permission_version="permissions-a",
        )
        first = routing_cache_key(**base)
        self.assertNotEqual(first, routing_cache_key(**{**base, "catalogue_version": "catalogue-b"}))
        self.assertNotEqual(first, routing_cache_key(**{**base, "permission_version": "permissions-b"}))
        self.assertNotEqual(first, routing_cache_key(**{**base, "context_version": "context-b"}))

    def test_ttl_and_bounded_size_are_enforced(self) -> None:
        decision = RoutingDecision(
            intent="memory_lookup",
            complexity=RequestComplexity.SIMPLE,
            execution_mode=ExecutionMode.SELECTED_TOOLS,
            tool_groups=["memory.read"],
            confidence=0.9,
            reason_code="memory_lookup",
        )
        self.assertTrue(
            self.cache.put("first", decision, available_groups=self.groups, ttl_seconds=10, max_entries=1, now=1)
        )
        self.cache.put("second", decision, available_groups=self.groups, ttl_seconds=10, max_entries=1, now=2)
        self.assertIsNone(self.cache.get("first", now=2))
        self.assertIsNotNone(self.cache.get("second", now=3))
        self.assertIsNone(self.cache.get("second", now=12))

    def test_write_and_clarification_patterns_are_not_cached(self) -> None:
        write_decision = RoutingDecision(
            intent="memory_update",
            complexity=RequestComplexity.SIMPLE,
            execution_mode=ExecutionMode.SELECTED_TOOLS,
            tool_groups=["memory.write"],
            confidence=0.9,
            reason_code="memory_update",
        )
        self.assertFalse(
            self.cache.put("write", write_decision, available_groups=self.groups, ttl_seconds=10, max_entries=10)
        )


class _TextModelClient:
    seen_tools: list[object] = []
    seen_system_messages: list[str] = []
    routing_decision: dict | None = None
    structured_calls: int = 0
    text_responses: list[ModelResponse] = []
    seen_message_counts: list[int] = []

    def __init__(self, profile, _env) -> None:
        self.profile = profile

    def generate_text(self, messages, **kwargs):
        self.seen_tools.append(kwargs.get("tools"))
        self.seen_system_messages.append(
            next((str(message.content) for message in messages if message.role == "system"), "")
        )
        self.seen_message_counts.append(len(messages))
        if type(self).text_responses:
            return type(self).text_responses.pop(0)
        return ModelResponse(content="Hello from the main agent", provider="routing-test", model=self.profile.model)

    def generate_structured(self, _messages, **_kwargs):
        type(self).structured_calls += 1
        return ModelResponse(
            content=self.routing_decision or {
                "intent": "memory_lookup",
                "complexity": "simple",
                "execution_mode": "selected_tools",
                "tool_groups": ["memory.read"],
                "context_scope": "relevant_retrieval",
                "needs_memory": True,
                "confidence": 0.95,
                "reason_code": "memory_lookup",
            },
            provider="fake",
            model=self.profile.model,
        )


class ConversationRoutingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake", lambda profile, env: _TextModelClient(profile, env))
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(
                id="routing-test-model",
                name="Routing test model",
                provider="fake",
                model="routing-test",
            )
        )
        self.service = ConversationService(self.context)
        self.setup = MainAgentSetupService(self.context)
        _TextModelClient.seen_tools = []
        _TextModelClient.seen_system_messages = []
        _TextModelClient.routing_decision = None
        _TextModelClient.structured_calls = 0
        _TextModelClient.text_responses = []
        _TextModelClient.seen_message_counts = []
        ROUTING_PATTERN_CACHE.clear()

    async def asyncTearDown(self) -> None:
        reset_settings_cache()

    async def test_authoritative_direct_route_sends_no_tool_schemas(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MAIN_AGENT_ROUTER_ENABLED": "true",
                "MAIN_AGENT_ROUTER_SHADOW_MODE": "false",
                "MAIN_AGENT_ROUTER_DIRECT_RESPONSE_ENABLED": "true",
            },
        ):
            reset_settings_cache()
            await self.setup.create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_instructions="Answer briefly.",
                    model_profile_id="routing-test-model",
                    profile_id="routing-main-profile",
                )
            )
            conversation = await self.service.create_conversation({"id": "routing-conversation"})
            response = await self.service.post_message(
                conversation.id,
                {
                    "response_mode": "stream",
                    "message": {"role": "user", "message_type": "user_text", "plain_text": "Hello!"},
                },
            )
        self.assertEqual(response["assistant_message"]["plain_text"], "Hello from the main agent")
        self.assertIn("/stream?after=", response["stream_url"])
        self.assertEqual(_TextModelClient.seen_tools, [None])
        events = await self.context.execution_store.list_events(
            f"conversation-audit-{conversation.id}",
        )
        routing_events = [event for event in events if event.event_type.value == "routing.decision.recorded"]
        self.assertEqual(len(routing_events), 1)
        self.assertEqual(routing_events[0].payload["routing_mode"], "direct_response")

    async def test_authoritative_selected_route_exposes_only_approved_group(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MAIN_AGENT_ROUTER_ENABLED": "true",
                "MAIN_AGENT_ROUTER_SHADOW_MODE": "false",
            },
        ):
            reset_settings_cache()
            profile = await self.setup.create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_instructions="Answer briefly.",
                    model_profile_id="routing-test-model",
                    profile_id="routing-main-profile",
                )
            )
            conversation = await self.service.create_conversation({"id": "routing-selected-conversation"})
            response = await self.service.post_message(
                conversation.id,
                {
                    "response_mode": "stream",
                    "message": {
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "What memories do you have about this project?",
                    },
                },
            )
        self.assertIn("/stream?after=", response["stream_url"])
        selected_payload = _TextModelClient.seen_tools[-1]
        self.assertIsNotNone(selected_payload)
        self.assertGreater(len(selected_payload), 0)
        agent = await self.context.agent_repo.get(profile.agent_id)
        self.assertLess(len(selected_payload), len(agent.tool_ids))
        available_tools = await AgentToolResolver(self.context).resolve_agent_tools(agent)
        expected_names = {
            tool_call_name(tool)
            for tool in self.context.tool_service.tool_registry.resolve_tool_groups(available_tools, ["memory.read"])
        }
        self.assertEqual({item["function"]["name"] for item in selected_payload}, expected_names)

    async def test_shadow_mode_records_selection_without_changing_full_tool_payload(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MAIN_AGENT_ROUTER_ENABLED": "true",
                "MAIN_AGENT_ROUTER_SHADOW_MODE": "true",
                "MAIN_AGENT_ROUTER_DIRECT_RESPONSE_ENABLED": "true",
            },
        ):
            reset_settings_cache()
            profile = await self.setup.create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_instructions="Answer briefly.",
                    model_profile_id="routing-test-model",
                    profile_id="routing-main-profile",
                )
            )
            conversation = await self.service.create_conversation({"id": "routing-shadow-conversation"})
            response = await self.service.post_message(
                conversation.id,
                {"role": "user", "message_type": "user_text", "plain_text": "Hello!"},
            )
        self.assertEqual(response["assistant_message"]["plain_text"], "Hello from the main agent")
        self.assertIsNotNone(_TextModelClient.seen_tools[-1])
        agent = await self.context.agent_repo.get(profile.agent_id)
        self.assertEqual(len(_TextModelClient.seen_tools[-1]), len(agent.tool_ids))
        events = await self.context.execution_store.list_events(f"conversation-audit-{conversation.id}")
        routing_event = next(event for event in events if event.event_type.value == "routing.decision.recorded")
        self.assertEqual(routing_event.metrics["selected_tool_count"], 0)
        self.assertEqual(routing_event.metrics["executor_tool_count"], len(agent.tool_ids))

    async def test_specialist_route_uses_allowlisted_specialist_prompt_and_tools(self) -> None:
        specialist_tool = _tool("specialist.repo.read", read_only=True)
        await self.context.tool_repo.save(specialist_tool)
        specialist = AgentDefinition(
            id="repo-specialist",
            name="Repo Specialist",
            description="Inspect repository structure",
            instructions="Focus on repository evidence.",
            model_profile_id="routing-test-model",
            tool_ids=[specialist_tool.id],
        )
        await self.context.agent_repo.save(specialist)
        _TextModelClient.routing_decision = {
            "intent": "repository_analysis",
            "complexity": "complex",
            "execution_mode": "specialist_agent",
            "specialist_agent": specialist.id,
            "tool_groups": ["other.read"],
            "context_scope": "recent_turns",
            "confidence": 0.95,
            "reason_code": "repository_specialist",
        }
        with patch.dict(
            os.environ,
            {
                "MAIN_AGENT_ROUTER_ENABLED": "true",
                "MAIN_AGENT_ROUTER_SHADOW_MODE": "false",
                "MAIN_AGENT_ROUTER_SPECIALIST_ENABLED": "true",
            },
        ):
            reset_settings_cache()
            profile = await self.setup.create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_instructions="Coordinate specialists.",
                    model_profile_id="routing-test-model",
                    profile_id="routing-main-profile",
                )
            )
            main_agent = await self.context.agent_repo.get(profile.agent_id)
            await self.context.agent_repo.save(
                main_agent.model_copy(update={"handoff_agent_ids": [specialist.id]})
            )
            conversation = await self.service.create_conversation({"id": "routing-specialist-conversation"})
            response = await self.service.post_message(
                conversation.id,
                {
                    "response_mode": "stream",
                    "message": {
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Analyze the repository organization for me.",
                    },
                },
            )
        self.assertIn("/stream?after=", response["stream_url"])
        payload = _TextModelClient.seen_tools[-1]
        self.assertEqual([item["function"]["name"] for item in payload], [tool_call_name(specialist_tool)])
        self.assertIn("Focus on repository evidence.", _TextModelClient.seen_system_messages[-1])
        events = await self.context.execution_store.list_events(f"conversation-audit-{conversation.id}")
        routing_event = next(event for event in events if event.event_type.value == "routing.decision.recorded")
        self.assertEqual(routing_event.payload["specialist_agent"], specialist.id)

    async def test_repeated_read_route_uses_hashed_pattern_cache(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MAIN_AGENT_ROUTER_ENABLED": "true",
                "MAIN_AGENT_ROUTER_SHADOW_MODE": "false",
                "MAIN_AGENT_ROUTER_CACHE_ENABLED": "true",
            },
        ):
            reset_settings_cache()
            await self.setup.create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_instructions="Answer briefly.",
                    model_profile_id="routing-test-model",
                    profile_id="routing-main-profile",
                )
            )
            conversation = await self.service.create_conversation({"id": "routing-cache-conversation"})
            message = {
                "role": "user",
                "message_type": "user_text",
                "plain_text": "Find the project memory decision.",
            }
            await self.service.post_message(conversation.id, message)
            await self.service.post_message(conversation.id, message)
        self.assertEqual(_TextModelClient.structured_calls, 1)
        events = await self.context.execution_store.list_events(f"conversation-audit-{conversation.id}")
        routing_events = [event for event in events if event.event_type.value == "routing.decision.recorded"]
        self.assertEqual([event.payload["router_cache_hit"] for event in routing_events], [False, True])

    async def test_zero_percent_rollout_keeps_authoritative_change_in_shadow(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MAIN_AGENT_ROUTER_ENABLED": "true",
                "MAIN_AGENT_ROUTER_SHADOW_MODE": "false",
                "MAIN_AGENT_ROUTER_ROLLOUT_PERCENT": "0",
            },
        ):
            reset_settings_cache()
            profile = await self.setup.create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_instructions="Answer briefly.",
                    model_profile_id="routing-test-model",
                    profile_id="routing-main-profile",
                )
            )
            conversation = await self.service.create_conversation({"id": "routing-rollout-conversation"})
            await self.service.post_message(
                conversation.id,
                {
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Find the project memory decision.",
                },
            )
        main_agent = await self.context.agent_repo.get(profile.agent_id)
        self.assertEqual(len(_TextModelClient.seen_tools[-1]), len(main_agent.tool_ids))
        events = await self.context.execution_store.list_events(f"conversation-audit-{conversation.id}")
        routing_event = next(event for event in events if event.event_type.value == "routing.decision.recorded")
        self.assertFalse(routing_event.payload["rollout_selected"])

    async def test_clarification_route_returns_question_without_executor_call(self) -> None:
        _TextModelClient.routing_decision = {
            "intent": "ambiguous_target",
            "complexity": "simple",
            "execution_mode": "clarification",
            "needs_clarification": True,
            "clarification_question": "Which workflow should I inspect?",
            "confidence": 0.9,
            "reason_code": "missing_workflow_target",
        }
        with patch.dict(
            os.environ,
            {
                "MAIN_AGENT_ROUTER_ENABLED": "true",
                "MAIN_AGENT_ROUTER_SHADOW_MODE": "false",
            },
        ):
            reset_settings_cache()
            await self.setup.create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_instructions="Answer briefly.",
                    model_profile_id="routing-test-model",
                    profile_id="routing-main-profile",
                )
            )
            conversation = await self.service.create_conversation({"id": "routing-clarification-conversation"})
            response = await self.service.post_message(
                conversation.id,
                {
                    "response_mode": "stream",
                    "message": {
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Inspect that workflow.",
                    },
                },
            )
        self.assertEqual(response["assistant_message"]["plain_text"], "Which workflow should I inspect?")
        self.assertIn("/stream?after=", response["stream_url"])
        self.assertEqual(_TextModelClient.seen_tools, [])

    async def test_routing_evaluation_compares_selection_with_actual_tool_call(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MAIN_AGENT_ROUTER_ENABLED": "true",
                "MAIN_AGENT_ROUTER_SHADOW_MODE": "false",
            },
        ):
            reset_settings_cache()
            await self.setup.create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_instructions="Answer briefly.",
                    model_profile_id="routing-test-model",
                    profile_id="routing-main-profile",
                )
            )
            memory_tool = await self.context.tool_repo.get("agency.memory.list")
            _TextModelClient.text_responses = [
                ModelResponse(
                    content=None,
                    tool_calls=[
                        ModelToolCall(id="memory-call", name=tool_call_name(memory_tool), arguments={})
                    ],
                    provider="fake",
                    model="routing-test",
                ),
                ModelResponse(content="Memory inspected.", provider="fake", model="routing-test"),
            ]
            conversation = await self.service.create_conversation({"id": "routing-evaluation-conversation"})
            await self.service.post_message(
                conversation.id,
                {
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "What memories do you have about this project?",
                },
            )
        events = await self.context.execution_store.list_events(f"conversation-audit-{conversation.id}")
        evaluation = next(event for event in events if event.event_type.value == "routing.evaluation.recorded")
        self.assertEqual(evaluation.payload["tools_actually_called"], [memory_tool.id])
        self.assertEqual(evaluation.payload["false_negative_tool_ids"], [])

    async def test_low_confidence_full_fallback_preserves_legacy_tools_and_history(self) -> None:
        _TextModelClient.routing_decision = {
            "intent": "uncertain",
            "complexity": "complex",
            "execution_mode": "selected_tools",
            "tool_groups": ["memory.read"],
            "confidence": 0.1,
            "reason_code": "uncertain",
        }
        with patch.dict(
            os.environ,
            {
                "MAIN_AGENT_ROUTER_ENABLED": "true",
                "MAIN_AGENT_ROUTER_SHADOW_MODE": "false",
            },
        ):
            reset_settings_cache()
            profile = await self.setup.create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_instructions="Answer briefly.",
                    model_profile_id="routing-test-model",
                    profile_id="routing-main-profile",
                )
            )
            conversation = await self.service.create_conversation({"id": "routing-fallback-conversation"})
            for index, role in enumerate((ConversationRole.USER, ConversationRole.ASSISTANT)):
                await self.context.conversation_message_repo.create(
                    ConversationMessage(
                        id=f"fallback-history-{index}",
                        conversation_id=conversation.id,
                        role=role,
                        message_type=(
                            ConversationMessageType.USER_TEXT
                            if role == ConversationRole.USER
                            else ConversationMessageType.ASSISTANT_TEXT
                        ),
                        plain_text=f"Earlier message {index}",
                    )
                )
            response = await self.service.post_message(
                conversation.id,
                {
                    "response_mode": "stream",
                    "message": {
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Handle the uncertain request.",
                    },
                },
            )
        self.assertIn("/stream?after=", response["stream_url"])
        main_agent = await self.context.agent_repo.get(profile.agent_id)
        self.assertEqual(len(_TextModelClient.seen_tools[-1]), len(main_agent.tool_ids))
        self.assertGreaterEqual(_TextModelClient.seen_message_counts[-1], 4)


class ToolSchemaBenchmarkTests(unittest.TestCase):
    def test_selected_payload_is_materially_smaller_than_representative_full_catalogue(self) -> None:
        tools = [
            *workflow_system_tool_definitions(),
            *memory_system_tool_definitions(),
            *tool_management_system_tool_definitions(),
        ]
        full_payload = _tool_payload(tools)
        selected_payload = _tool_payload(
            [tool for tool in tools if tool.id in {"agency.memory.list", "agency.memory.catalog"}]
        )
        full_bytes = len(json.dumps(full_payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        selected_bytes = len(json.dumps(selected_payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        self.assertGreater(len(tools), 20)
        self.assertLess(selected_bytes, full_bytes * 0.25)
        self.assertEqual(_tool_payload([]), [])


def _tool_payload(tools: list[ToolDefinition]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool_call_name(tool),
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]
