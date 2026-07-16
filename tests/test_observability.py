from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.api.routes.connectors import create_connectors_router
from app.api.routes.observability import GRAPH_UNAVAILABLE_REASON
from app.api.routes.observability import create_observability_router
from app.api.routes.users import create_users_router
from app.core.config import reset_settings_cache
from app.core.time import utc_now
from app.domain import CredentialDefinition, Execution, ExecutionEvent, ExecutionEventType, \
    ExecutionStatus, ModelProfileDefinition
from app.graph.neo4j_read import GraphReadDocument, GraphReadEdge, GraphReadNode
from app.graph.service import GRAPH_NEIGHBORHOOD_PRESETS, GraphReadUnavailableError
from app.llm.base import ModelResponse
from app.llm.registry import LLMEnvironmentConfig
from app.observability.event_bus import EventBus, set_default_event_bus
from app.observability.exporters.jsonl import JSONLExporter
from app.observability.exporters.langfuse import LangfuseExporter
from app.observability.redaction import Redactor
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState
from app.services.connector_retention import ConnectorRetentionService
from app.services.conversations.core import ConversationService
from app.services.conversations.audit import ConversationAuditService
from app.services.main_agent_setup.service import MainAgentSetupConfig, MainAgentSetupService


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


class _FakeLangfuseObservation:
    def __init__(self):
        self.ended = False

    def end(self):
        self.ended = True


class _FakeLangfuseClient:
    def __init__(self):
        self.observations = []

    def start_observation(self, **kwargs):
        observation = _FakeLangfuseObservation()
        self.observations.append((kwargs, observation))
        return observation


class _FakeObservabilityGraphReadService:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def get_neighborhood(
            self,
            node_id: str,
            *,
            labels=None,
            relationship_types=None,
            depth=1,
            limit=200,
            include_deleted=False,
    ):
        self.calls.append(
            (
                "get_neighborhood",
                {
                    "node_id": node_id,
                    "labels": labels,
                    "relationship_types": relationship_types,
                    "depth": depth,
                    "limit": limit,
                    "include_deleted": include_deleted,
                },
            )
        )
        return GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id=node_id,
                    type="WorkflowRun",
                    labels=["WorkflowRun"],
                    properties={"status": "completed"},
                ),
                GraphReadNode(
                    id="step-run-1",
                    type="StepRun",
                    labels=["StepRun"],
                    properties={"task_id": "task-obs"},
                ),
            ],
            edges=[
                GraphReadEdge(
                    id="edge-observability-1",
                    source=node_id,
                    target="step-run-1",
                    type="HAS_STEP_RUN",
                )
            ],
            meta={"source": "fake"},
        )


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
            metrics={"latency_ms": 5, "credential_token": "Bearer metric-secret-token"},
        )

        stored = (await self.context.execution_store.list_events("exec-1"))[0]
        self.assertEqual(stored.payload["authorization"], "[REDACTED]")
        self.assertEqual(stored.payload["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(stored.metrics["credential_token"], "[REDACTED]")
        self.assertEqual(stored.metrics["latency_ms"], 5)
        self.assertTrue(stored.redacted_fields)
        self.assertEqual(event.trace_id, state.trace_id)
        self.assertEqual(event.agent_id, "agent-1")
        self.assertEqual(event.task_id, "task-1")

        with open(self.jsonl_path, "r", encoding="utf-8") as handle:
            line = json.loads(handle.readline())
        self.assertEqual(line["payload"]["authorization"], "[REDACTED]")
        self.assertEqual(line["metrics"]["credential_token"], "[REDACTED]")
        self.assertEqual(line["metrics"]["latency_ms"], 5)

    async def test_free_text_redaction_removes_adjacent_secret_values(self):
        redactor = Redactor()

        for value in (
            "password=hunter2",
            "token: abc123",
            "secret value xyz",
            "authorization is BasicCredential",
        ):
            with self.subTest(value=value):
                redacted, fields = redactor.redact_text(value)
                self.assertEqual(redacted, "[REDACTED]")
                self.assertTrue(fields)

        # Ordinary operational prose must not be treated as a credential pair.
        self.assertEqual(redactor.redact_text("token budget remains")[0], "token budget remains")

    async def test_langfuse_exporter_maps_llm_and_tool_events_to_observations(self):
        client = _FakeLangfuseClient()
        exporter = LangfuseExporter(client=client)
        bus = EventBus(exporters=[exporter], redact_secrets=True)
        state = NativeExecutionState(execution_id="exec-langfuse", workflow_id="workflow-langfuse")
        state.current_agent_id = "agent-langfuse"
        state.current_task_id = "task-langfuse"

        request = ExecutionEvent(
            execution_id=state.execution_id,
            workflow_id=state.workflow_id,
            agent_id=state.current_agent_id,
            task_id=state.current_task_id,
            trace_id=state.trace_id,
            event_type=ExecutionEventType.LLM_REQUEST_CREATED,
            model_request_id="model-request-1",
            payload={
                "messages": [{"role": "user", "content": "Use token sk-secret-1234567890"}],
                "model_profile_id": "profile-1",
            },
            metrics={"model_provider": "fake", "model_name": "fake-model", "input_tokens": 3},
        )
        response = ExecutionEvent(
            execution_id=state.execution_id,
            workflow_id=state.workflow_id,
            agent_id=state.current_agent_id,
            task_id=state.current_task_id,
            trace_id=state.trace_id,
            event_type=ExecutionEventType.LLM_RESPONSE_CREATED,
            model_request_id="model-request-1",
            payload={"content": "done", "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}},
            metrics={"model_provider": "fake", "model_name": "fake-model", "total_tokens": 5},
        )
        tool_started = ExecutionEvent(
            execution_id=state.execution_id,
            workflow_id=state.workflow_id,
            agent_id=state.current_agent_id,
            task_id=state.current_task_id,
            trace_id=state.trace_id,
            event_type=ExecutionEventType.TOOL_CALL_STARTED,
            tool_call_id="tool-call-1",
            payload={"tool_name": "send_email", "arguments": {"to": "ops@example.com"}},
        )
        tool_completed = ExecutionEvent(
            execution_id=state.execution_id,
            workflow_id=state.workflow_id,
            agent_id=state.current_agent_id,
            task_id=state.current_task_id,
            trace_id=state.trace_id,
            event_type=ExecutionEventType.TOOL_CALL_COMPLETED,
            tool_call_id="tool-call-1",
            payload={"tool_name": "send_email", "output": {"sent": True}},
        )

        for event in (request, response, tool_started, tool_completed):
            bus.publish(event)

        observations = [item[0] for item in client.observations]
        self.assertEqual([item["as_type"] for item in observations], ["span", "generation", "tool", "tool"])
        generation = observations[1]
        self.assertEqual(generation["model"], "fake-model")
        self.assertEqual(generation["usage_details"], {"input": 3, "output": 2, "total": 5})
        self.assertEqual(generation["input"][0]["content"], "Use token [REDACTED]")
        self.assertEqual(generation["metadata"]["agent_id"], "agent-langfuse")
        self.assertEqual(generation["metadata"]["execution_id"], "exec-langfuse")
        self.assertEqual(generation["trace_context"]["trace_id"], state.trace_id.replace("-", ""))
        self.assertTrue(all(observation.ended for _, observation in client.observations))

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
        self.assertEqual(direct_reply.payload["response_kind"], "direct_reply_model_call")
        self.assertEqual(direct_reply.payload["call_kind"], "direct_reply")
        self.assertEqual(direct_reply.payload["model_profile_id"], "profile-fake")

        with open(self.jsonl_path, "r", encoding="utf-8") as handle:
            exported = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(any(item["event_type"] == "llm.response.created" for item in exported))

    async def test_conversation_approval_and_workflow_mutation_emit_audit_events(self):
        service = await self._create_main_agent_conversation_service()
        conversation = await service.create_conversation(
            {
                "id": "conversation-observability-mutation",
                "created_by_user_id": "user-observability",
                "channel_type": "api",
            }
        )

        result = await service.post_message(
            conversation.id,
            {
                "message": {
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
        reset_settings_cache()
        self.context = create_test_api_context()
        app = FastAPI()
        app.include_router(create_users_router(self.context))
        app.include_router(create_connectors_router(self.context))
        app.include_router(create_observability_router(self.context))
        self.client = TestClient(app)
        self.owner_headers = {
            "x-agency-user-id": "user-1",
            "x-agency-user-email": "owner@example.com",
        }
        self.client.post(
            "/users/sync",
            json={
                "id": "user-1",
                "email": "owner@example.com",
                "display_name": "Owner One",
            },
        )

        execution = Execution(
            id="exec-metrics",
            workflow_id="workflow-obs",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
            metadata={
                "agent_ids": ["agent-obs"],
                "runtime_governance": {
                    "token_usage": {
                        "total": {
                            "total_tokens": 15,
                            "estimated_cost": 0.01,
                        }
                    }
                },
            },
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
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                        "provider_usage": {
                            "model_fallback": {
                                "used": True,
                                "primary_provider": "fake",
                                "primary_model": "primary-model",
                                "fallback_provider": "fake",
                                "fallback_model": "fake-model",
                                "fallback_index": 1,
                            }
                        },
                    },
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
            ExecutionEvent(
                execution_id=execution.id,
                workflow_id="workflow-obs",
                agent_id="agent-obs",
                task_id="task-obs",
                event_type=ExecutionEventType.CONTEXT_HEALTH_RECORDED,
                sequence=3,
                payload={
                    "status": "warning",
                    "estimated_prompt_tokens": 8000,
                    "context_window": 10000,
                    "usage_ratio": 0.8,
                },
            ),
            ExecutionEvent(
                execution_id=execution.id,
                workflow_id="workflow-obs",
                agent_id="agent-obs",
                task_id="task-obs",
                event_type=ExecutionEventType.TOKEN_BUDGET_WARNING,
                sequence=4,
                payload={
                    "scope": "run",
                    "limit": 20,
                    "used_tokens": 15,
                    "usage_ratio": 0.75,
                },
            ),
            ExecutionEvent(
                execution_id=execution.id,
                workflow_id="workflow-obs",
                agent_id="agent-obs",
                task_id="task-obs",
                event_type=ExecutionEventType.CONTEXT_COMPACTION_COMPLETED,
                sequence=5,
                payload={
                    "record": {
                        "compacted": True,
                        "reason": "context_health_threshold",
                        "estimated_tokens_saved": 500,
                    },
                    "compacted": True,
                },
                metrics={"estimated_tokens_saved": 500},
            ),
            ExecutionEvent(
                execution_id=execution.id,
                workflow_id="workflow-obs",
                agent_id="agent-obs",
                task_id="task-obs",
                event_type=ExecutionEventType.MODEL_FALLBACK_FAILED,
                sequence=6,
                payload={
                    "primary_provider": "fake",
                    "primary_model": "primary-model",
                    "attempts": [{"model": "primary-model"}, {"model": "backup-model"}],
                    "error": "backup-model timed out",
                },
            ),
        ]
        other_execution = Execution(
            id="exec-other-metrics",
            workflow_id="workflow-other",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
            metadata={"agent_ids": ["agent-other"]},
        )
        self.context.execution_store._executions[other_execution.id] = other_execution  # noqa: SLF001
        self.context.execution_store._events[other_execution.id] = [  # noqa: SLF001
            ExecutionEvent(
                execution_id=other_execution.id,
                workflow_id="workflow-other",
                agent_id="agent-other",
                task_id="task-other",
                event_type=ExecutionEventType.LLM_RESPONSE_CREATED,
                sequence=1,
                payload={
                    "usage": {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40},
                    "model_provider": "other",
                    "model_name": "other-model",
                },
                metrics={
                    "model_provider": "other",
                    "model_name": "other-model",
                    "input_tokens": 30,
                    "output_tokens": 10,
                    "total_tokens": 40,
                    "estimated_cost": 0.02,
                },
            )
        ]

    def tearDown(self):
        reset_settings_cache()

    def _run(self, awaitable):
        import asyncio

        return asyncio.run(awaitable)

    def test_timeline_and_metrics_endpoints(self):
        timeline = self.client.get("/observability/executions/exec-metrics/timeline")
        self.assertEqual(timeline.status_code, 200)
        self.assertEqual(len(timeline.json()["events"]), 6)

        agent_metrics = self.client.get("/observability/agents/agent-obs/metrics")
        self.assertEqual(agent_metrics.status_code, 200)
        agent_payload = agent_metrics.json()
        self.assertEqual(agent_payload["total_tokens"], 15)
        self.assertEqual(agent_payload["context_health"]["latest"]["status"], "warning")
        self.assertEqual(agent_payload["budget"]["warning_count"], 1)
        self.assertEqual(agent_payload["compaction"]["status_counts"]["completed"], 1)

        workflow_metrics = self.client.get("/observability/workflows/workflow-obs/metrics")
        self.assertEqual(workflow_metrics.status_code, 200)
        workflow_payload = workflow_metrics.json()
        self.assertEqual(workflow_payload["workflow_id"], "workflow-obs")
        self.assertEqual(workflow_payload["total_tokens"], 15)
        self.assertEqual(workflow_payload["context_health"]["status_counts"]["warning"], 1)

        model_usage = self.client.get("/observability/models/usage")
        self.assertEqual(model_usage.status_code, 200)
        usage_by_model = {item["model"]: item for item in model_usage.json()["items"]}
        self.assertEqual(usage_by_model["fake-model"]["total_tokens"], 15)
        self.assertEqual(usage_by_model["fake-model"]["fallback_count"], 1)
        self.assertEqual(usage_by_model["fake-model"]["fallback_rate"], 1.0)
        self.assertEqual(usage_by_model["other-model"]["total_tokens"], 40)
        fallback_summary = model_usage.json()["fallback_summary"]
        self.assertEqual(fallback_summary["fallback_count"], 1)
        self.assertEqual(fallback_summary["fallback_failure_count"], 1)
        self.assertEqual(fallback_summary["fallback_primary_models"], {"fake:primary-model": 1})
        self.assertEqual(fallback_summary["recent_failures"][0]["error"], "backup-model timed out")

    def test_model_usage_endpoint_filters_usage(self):
        filters = {
            "workflow_id": "workflow-obs",
            "agent_id": "agent-obs",
            "execution_id": "exec-metrics",
            "provider": "fake",
            "model": "fake-model",
        }
        response = self.client.get("/observability/models/usage", params=filters)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["filters"], filters)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["provider"], "fake")
        self.assertEqual(body["items"][0]["model"], "fake-model")
        self.assertEqual(body["items"][0]["total_tokens"], 15)

    def test_model_usage_endpoint_filters_by_execution_workflow_fallback(self):
        event = self.context.execution_store._events["exec-metrics"][0]  # noqa: SLF001
        event.workflow_id = None

        response = self.client.get("/observability/models/usage", params={"workflow_id": "workflow-obs"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([item["model"] for item in body["items"]], ["fake-model"])

    def test_connector_history_endpoint_returns_owner_scoped_audit_runs(self):
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-obs-telegram",
                    owner_user_id="user-1",
                    name="Obs Telegram",
                    provider="telegram",
                    secret_ref="env://TELEGRAM_BOT_TOKEN",
                    metadata={},
                )
            )
        )

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "telegram-token"}, clear=False), patch(
                "app.services.connectors.httpx.request",
                return_value=type("Resp", (),
                                  {"status_code": 200, "json": lambda self: {"ok": True, "result": {"id": 123}}})(),
        ):
            response = self.client.get("/integrations/connectors/credential-obs-telegram/health",
                                       headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)

        after = (utc_now() - timedelta(minutes=5)).isoformat()
        history = self.client.get(
            "/observability/connectors/history",
            headers=self.owner_headers,
            params={"provider": "telegram", "started_after": after},
        )
        self.assertEqual(history.status_code, 200)
        body = history.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["credentialId"], "credential-obs-telegram")

    def test_connector_retention_status_endpoint_returns_last_run_telemetry(self):
        with patch.dict(
                "os.environ",
                {
                    "CONNECTOR_HEALTH_HISTORY_RETENTION_ENABLED": "true",
                    "CONNECTOR_HEALTH_HISTORY_RETENTION_INTERVAL_SECONDS": "1800",
                    "CONNECTOR_HEALTH_HISTORY_RETENTION_DAYS": "14",
                    "CONNECTOR_HEALTH_HISTORY_RETENTION_MAX_PER_CREDENTIAL": "5",
                },
                clear=False,
        ):
            reset_settings_cache()
            self._run(
                ConnectorRetentionService(self.context).run_policy(
                    started_before=utc_now() - timedelta(days=14),
                    keep_latest_per_credential=5,
                )
            )
            response = self.client.get("/observability/connectors/retention", headers=self.owner_headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["intervalSeconds"], 1800)
        self.assertEqual(payload["retentionDays"], 14)
        self.assertEqual(payload["maxPerCredential"], 5)
        self.assertEqual(payload["counters"]["connector_retention.runs"], 1)
        self.assertIsNotNone(payload["lastRun"])
        self.assertEqual(payload["lastRun"]["action"], "connector_retention_run")

    def test_execution_projection_graph_endpoint_returns_observability_graph(self):
        fake_graph = _FakeObservabilityGraphReadService()
        self.context.graph_read_service = fake_graph

        response = self.client.get(
            "/observability/executions/exec-metrics/graph",
            headers=self.owner_headers,
            params={"depth": 2, "limit": 25},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["available"])
        self.assertIsNone(body["reason"])
        self.assertEqual(body["graph"]["meta"]["query"], "observability_execution_graph")
        self.assertEqual(body["graph"]["meta"]["projection_available"], True)
        self.assertEqual(body["graph"]["meta"]["node_count"], 2)
        self.assertEqual(
            fake_graph.calls[-1],
            (
                "get_neighborhood",
                {
                    "node_id": "exec-metrics",
                    "labels": GRAPH_NEIGHBORHOOD_PRESETS["workflow_run"]["labels"],
                    "relationship_types": GRAPH_NEIGHBORHOOD_PRESETS["workflow_run"]["relationship_types"],
                    "depth": 2,
                    "limit": 25,
                    "include_deleted": False,
                },
            ),
        )

    def test_projection_graph_endpoint_requires_execution_read_scope_before_traversal(self):
        fake_graph = _FakeObservabilityGraphReadService()
        self.context.graph_read_service = fake_graph

        response = self.client.get("/observability/executions/exec-metrics/graph")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(fake_graph.calls, [])

    def test_workflow_projection_graph_endpoint_reports_projection_unavailable(self):
        with patch(
            "app.api.routes.observability.resolve_graph_reader",
            side_effect=GraphReadUnavailableError(GRAPH_UNAVAILABLE_REASON),
        ):
            response = self.client.get(
                "/observability/workflows/workflow-obs/graph",
                headers=self.owner_headers,
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["reason"], "Neo4j graph read API is disabled or unavailable")
        self.assertEqual(body["graph"]["nodes"], [])
        self.assertEqual(body["graph"]["edges"], [])
        self.assertEqual(body["graph"]["meta"]["query"], "observability_workflow_graph")
        self.assertEqual(body["graph"]["meta"]["projection_available"], False)


if __name__ == "__main__":
    unittest.main()
