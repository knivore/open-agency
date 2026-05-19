from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.routes.observability import create_observability_router
from app.core.config import reset_settings_cache
from app.domain import Execution, ExecutionEvent, ExecutionEventType, ExecutionStatus, ModelProfileDefinition
from app.domain.models import MCPExposureSettings, SecuritySettings, ToolImplementationReference
from app.llm.base import ModelResponse
from app.llm.registry import LLMEnvironmentConfig
from app.observability import EventBus, set_default_event_bus
from app.observability.exporters.jsonl import JSONLExporter
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState
from app.services.conversations import ConversationService
from app.services.conversations.audit import ConversationAuditService
from app.services.main_agent_setup import MainAgentSetupConfig, MainAgentSetupService


class _FakeConversationModelClient:
    provider_key = "fake"

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content="direct reply", provider="fake", model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content={"ok": True}, provider="fake", model=self.profile.model)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "unused"

    def count_tokens(self, messages, **kwargs):
        return 0

    def health_check(self):
        return {"ok": True}


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = create_test_api_context()
        self.tempdir = tempfile.TemporaryDirectory()
        self.jsonl_path = os.path.join(self.tempdir.name, "observability.jsonl")
        set_default_event_bus(EventBus(exporters=[JSONLExporter(self.jsonl_path)], redact_secrets=True))

    async def asyncTearDown(self):
        set_default_event_bus(None)
        reset_settings_cache()
        self.tempdir.cleanup()

    async def _create_main_agent_conversation_service(self) -> ConversationService:
        self.context.llm_provider_registry.register(
            "fake",
            lambda profile, env: _FakeConversationModelClient(profile, env),
        )
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
        )
        await MainAgentSetupService(self.context).create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for observability tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        return ConversationService(self.context)

    async def test_event_emission_redacts_before_storage_and_export(self):
        emitter = ExecutionEventEmitter(self.context.execution_store)
        state = NativeExecutionState(execution_id="exec-1", workflow_id="workflow-1")
        state.current_agent_id = "agent-1"
        state.current_task_id = "task-1"
        await self.context.execution_store.save_execution(
            Execution(
                id="exec-1",
                workflow_id="workflow-1",
                runtime_adapter_id="native",
                status=ExecutionStatus.RUNNING,
                input_payload={},
            )
        )

        event = await emitter.emit(
            state,
            ExecutionEventType.TOOL_CALL_STARTED,
            payload={"authorization": "Bearer super-secret-token", "nested": {"api_key": "sk-secret-1234567890"}},
            metrics={"latency_ms": 5},
        )

        stored = (await self.context.execution_store.list_events("exec-1"))[0]
        self.assertEqual(stored.payload["authorization"], "[REDACTED]")
        self.assertEqual(stored.payload["nested"]["api_key"], "[REDACTED]")
        self.assertTrue(stored.redacted_fields)
        self.assertEqual(event.trace_id, state.trace_id)
        self.assertEqual(event.agent_id, "agent-1")
        self.assertEqual(event.task_id, "task-1")

        with open(self.jsonl_path, "r", encoding="utf-8") as handle:
            line = json.loads(handle.readline())
        self.assertEqual(line["payload"]["authorization"], "[REDACTED]")

    async def test_conversation_direct_reply_emits_audit_event_to_store_and_exporter(self):
        service = await self._create_main_agent_conversation_service()
        conversation = await service.create_conversation({"id": "conversation-observability-direct"})

        await service.post_message(
            conversation.id,
            {
                "id": "message-observability-direct",
                "role": "user",
                "message_type": "user_text",
                "plain_text": "Hello",
                "content": {"text": "Hello"},
            },
        )

        audit_execution_id = ConversationAuditService(self.context).audit_execution_id(conversation.id)
        events = await self.context.execution_store.list_events(audit_execution_id)
        self.assertTrue(any(item.event_type == ExecutionEventType.LLM_RESPONSE_CREATED for item in events))
        direct_reply = next(item for item in events if item.event_type == ExecutionEventType.LLM_RESPONSE_CREATED)
        self.assertEqual(direct_reply.payload["response_kind"], "direct_reply")
        self.assertEqual(direct_reply.payload["model_profile_id"], "profile-fake")

        with open(self.jsonl_path, "r", encoding="utf-8") as handle:
            exported = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(any(item["event_type"] == "llm.response.created" for item in exported))

    async def test_conversation_approval_and_workflow_mutation_emit_audit_events(self):
        service = await self._create_main_agent_conversation_service()
        conversation = await service.create_conversation({"id": "conversation-observability-mutation"})

        result = await service.post_message(
            conversation.id,
            {
                "id": "message-observability-mutation",
                "role": "user",
                "message_type": "user_text",
                "plain_text": "Create workflow",
                "content": {
                    "text": "Create workflow",
                    "workflow_proposal": {
                        "workflow": {
                            "id": "workflow-observability-created",
                            "name": "Observed Workflow",
                            "entrypoint": "node-1",
                            "nodes": [
                                {"id": "node-1", "name": "Entry", "node_type": "task", "task_id": "task-1"}
                            ],
                            "task_definitions": [
                                {"id": "task-1", "name": "Task One", "description": "Do observable work"}
                            ],
                            "metadata": {"visible_to_main_agent": True, "mutable_by_main_agent": True},
                        }
                    },
                },
            },
        )
        approval_id = result["approval_request"]["id"]
        await service.approve_request(approval_id, actor_user_id="user-observability", reason="approved")

        audit_execution_id = ConversationAuditService(self.context).audit_execution_id(conversation.id)
        events = await self.context.execution_store.list_events(audit_execution_id)
        event_types = [item.event_type for item in events]
        self.assertIn(ExecutionEventType.APPROVAL_REQUESTED, event_types)
        self.assertIn(ExecutionEventType.APPROVAL_GRANTED, event_types)
        self.assertTrue(
            any(
                item.payload.get("mutation_type") == "workflow_create"
                and item.payload.get("decision") == "approved"
                for item in events
            )
        )


class ObservabilityApiTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        app = FastAPI()
        app.include_router(create_observability_router(self.context))
        self.client = TestClient(app)

        execution = Execution(
            id="exec-metrics",
            workflow_id="workflow-obs",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
            metadata={"agent_ids": ["agent-obs"]},
        )
        self.context.execution_store._executions[execution.id] = execution  # noqa: SLF001
        self.context.execution_store._events[execution.id] = [  # noqa: SLF001
            ExecutionEvent(
                execution_id=execution.id,
                workflow_id="workflow-obs",
                agent_id="agent-obs",
                task_id="task-obs",
                event_type=ExecutionEventType.LLM_RESPONSE_CREATED,
                sequence=1,
                payload={
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    "model_provider": "fake",
                    "model_name": "fake-model",
                },
                metrics={
                    "model_provider": "fake",
                    "model_name": "fake-model",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "estimated_cost": 0.01,
                },
            ),
            ExecutionEvent(
                execution_id=execution.id,
                workflow_id="workflow-obs",
                agent_id="agent-obs",
                task_id="task-obs",
                event_type=ExecutionEventType.TOOL_CALL_COMPLETED,
                sequence=2,
                metrics={"latency_ms": 4, "tool_success": True},
            ),
        ]

    def _run(self, awaitable):
        import asyncio

        return asyncio.run(awaitable)

    def test_timeline_and_metrics_endpoints(self):
        timeline = self.client.get("/observability/executions/exec-metrics/timeline")
        self.assertEqual(timeline.status_code, 200)
        self.assertEqual(len(timeline.json()["events"]), 2)

        agent_metrics = self.client.get("/observability/agents/agent-obs/metrics")
        self.assertEqual(agent_metrics.status_code, 200)
        self.assertEqual(agent_metrics.json()["total_tokens"], 15)

        workflow_metrics = self.client.get("/observability/workflows/workflow-obs/metrics")
        self.assertEqual(workflow_metrics.status_code, 200)
        self.assertEqual(workflow_metrics.json()["workflow_id"], "workflow-obs")

        model_usage = self.client.get("/observability/models/usage")
        self.assertEqual(model_usage.status_code, 200)
        self.assertEqual(model_usage.json()["items"][0]["total_tokens"], 15)


if __name__ == "__main__":
    unittest.main()
