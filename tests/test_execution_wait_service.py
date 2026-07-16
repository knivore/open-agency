from __future__ import annotations

import asyncio
import os
import time
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.api.routes.executions import create_executions_router
from app.core.config import reset_settings_cache
from app.core.time import utc_now
from app.domain import Execution, ExecutionEventType, ExecutionStatus, ExecutionWaitKind, UserDefinition
from app.domain import (
    AgentDefinition,
    ModelProfileDefinition,
    TaskDefinition,
    WorkflowDefinition,
    WorkflowEdgeDefinition,
    WorkflowNodeDefinition,
)
from app.llm.base import ModelResponse
from app.services.execution_waits import ExecutionWaitConflictError, ExecutionWaitService


class ExecutionWaitServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.execution = Execution(
            id="execution-wait-service",
            workflow_id="workflow-wait-service",
            runtime_adapter="native",
            status=ExecutionStatus.PAUSED,
            output_json={
                "node_outputs": {"node-1": {"result": "checkpoint"}},
                "checkpoint": {"completed_node_ids": ["node-1"]},
            },
        )
        await self.context.execution_store.save_execution(self.execution)
        self.service = ExecutionWaitService(self.context)

    async def test_create_wait_is_idempotent_and_records_audit_event(self) -> None:
        wait, created = await self.service.create_wait(
            execution_id=self.execution.id,
            kind=ExecutionWaitKind.INPUT,
            idempotency_key="input:region",
            request_payload={"question": "Which region?"},
            policy={"on_timeout": "pause"},
        )
        duplicate, duplicate_created = await self.service.create_wait(
            execution_id=self.execution.id,
            kind=ExecutionWaitKind.INPUT,
            idempotency_key="input:region",
            request_payload={"question": "Which region?"},
        )

        current = await self.context.execution_store.get_execution(self.execution.id)
        events = await self.context.execution_store.list_events(self.execution.id)
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.id, wait.id)
        self.assertEqual(current.status, ExecutionStatus.WAITING_FOR_INPUT)
        self.assertEqual(current.metadata["active_wait"]["wait_id"], wait.id)
        self.assertEqual(wait.checkpoint["node_outputs"]["node-1"], {"result": "checkpoint"})
        self.assertEqual(wait.policy, {"on_timeout": "pause"})
        self.assertEqual([event.event_type for event in events], [ExecutionEventType.EXECUTION_WAITING])

    async def test_second_pending_wait_is_rejected(self) -> None:
        await self.service.create_wait(
            execution_id=self.execution.id,
            kind=ExecutionWaitKind.EVENT,
            idempotency_key="event:first",
            correlation_key="deploy:123",
        )

        with self.assertRaisesRegex(ExecutionWaitConflictError, "already has pending wait"):
            await self.service.create_wait(
                execution_id=self.execution.id,
                kind=ExecutionWaitKind.INPUT,
                idempotency_key="input:second",
            )

    async def test_resolve_wait_is_single_claim_and_preserves_resolution_for_resume(self) -> None:
        wait, _ = await self.service.create_wait(
            execution_id=self.execution.id,
            kind=ExecutionWaitKind.INPUT,
            idempotency_key="input:region",
        )

        first = await self.service.resolve_wait(
            execution_id=self.execution.id,
            wait_id=wait.id,
            resolution_key="message:42",
            resolution_payload={"region": "ap-southeast-1"},
            resolved_by="operator-1",
            resume=False,
        )
        duplicate = await self.service.resolve_wait(
            execution_id=self.execution.id,
            wait_id=wait.id,
            resolution_key="message:42",
            resolution_payload={"region": "ap-southeast-1"},
            resolved_by="operator-1",
            resume=False,
        )

        current = await self.context.execution_store.get_execution(self.execution.id)
        events = await self.context.execution_store.list_events(self.execution.id)
        self.assertTrue(first["claimed"])
        self.assertFalse(duplicate["claimed"])
        self.assertEqual(current.status, ExecutionStatus.PAUSED)
        self.assertEqual(
            current.input_payload["wait_resolutions"][wait.id]["payload"],
            {"region": "ap-southeast-1"},
        )
        self.assertEqual(
            [event.event_type for event in events],
            [ExecutionEventType.EXECUTION_WAITING, ExecutionEventType.EXECUTION_WOKEN],
        )

        with self.assertRaisesRegex(ExecutionWaitConflictError, "already resolved"):
            await self.service.resolve_wait(
                execution_id=self.execution.id,
                wait_id=wait.id,
                resolution_key="message:competing",
                resolution_payload={"region": "us-east-1"},
                resume=False,
            )

    async def test_due_sleep_wait_is_claimed_once_and_resumed(self) -> None:
        class RecordingControlPlane:
            def __init__(self, store) -> None:
                self.store = store
                self.resume_calls: list[str] = []

            async def resume(self, execution_id: str):
                self.resume_calls.append(execution_id)
                execution = await self.store.get_execution(execution_id)
                execution.status = ExecutionStatus.QUEUED
                await self.store.update_execution(execution)
                return execution

        control_plane = RecordingControlPlane(self.context.execution_store)
        self.context.control_plane = control_plane
        wait, _ = await self.service.create_wait(
            execution_id=self.execution.id,
            kind=ExecutionWaitKind.SLEEP,
            idempotency_key="sleep:cycle-2",
            wake_at=utc_now() - timedelta(seconds=1),
        )

        first = await self.service.wake_due_waits()
        second = await self.service.wake_due_waits()

        self.assertEqual(first["scanned"], 1)
        self.assertEqual(first["items"][0]["wait_id"], wait.id)
        self.assertTrue(first["items"][0]["resumed"])
        self.assertEqual(second["scanned"], 0)
        self.assertEqual(control_plane.resume_calls, [self.execution.id])

    async def test_event_wake_is_correlated_and_consumed_once(self) -> None:
        class RecordingControlPlane:
            def __init__(self, store) -> None:
                self.store = store
                self.resume_calls: list[str] = []

            async def resume(self, execution_id: str):
                self.resume_calls.append(execution_id)
                execution = await self.store.get_execution(execution_id)
                execution.status = ExecutionStatus.QUEUED
                await self.store.update_execution(execution)
                return execution

        control_plane = RecordingControlPlane(self.context.execution_store)
        self.context.control_plane = control_plane
        wait, _ = await self.service.create_wait(
            execution_id=self.execution.id,
            kind=ExecutionWaitKind.EVENT,
            idempotency_key="event:deployment-finished",
            correlation_key="deployment:123",
        )

        unrelated = await self.service.wake_event(
            correlation_key="deployment:other",
            event_id="event-0",
        )
        first = await self.service.wake_event(
            correlation_key="deployment:123",
            event_id="event-1",
            event_payload={"result": "healthy"},
        )
        repeated = await self.service.wake_event(
            correlation_key="deployment:123",
            event_id="event-1",
            event_payload={"result": "healthy"},
        )

        resolved = await self.service.get_wait(self.execution.id, wait.id)
        self.assertEqual(unrelated["matched"], 0)
        self.assertEqual(first["matched"], 1)
        self.assertTrue(first["items"][0]["claimed"])
        self.assertEqual(repeated["matched"], 0)
        self.assertEqual(resolved.resolution_payload["payload"], {"result": "healthy"})
        self.assertEqual(control_plane.resume_calls, [self.execution.id])

    async def test_wait_kind_contract_rejects_unwakeable_or_ambiguous_waits(self) -> None:
        with self.assertRaisesRegex(ValueError, "Event waits require correlation_key"):
            await self.service.create_wait(
                execution_id=self.execution.id,
                kind=ExecutionWaitKind.EVENT,
                idempotency_key="event:missing-correlation",
            )
        with self.assertRaisesRegex(ValueError, "Only sleep waits may define wake_at"):
            await self.service.create_wait(
                execution_id=self.execution.id,
                kind=ExecutionWaitKind.INPUT,
                idempotency_key="input:unexpected-wake-time",
                wake_at=utc_now() + timedelta(minutes=1),
            )

    async def test_unsupported_auto_resume_does_not_claim_pending_wait(self) -> None:
        execution = self.execution.model_copy(
            update={
                "id": "execution-wait-non-native",
                "runtime_adapter_id": "crewai",
                "status": ExecutionStatus.PAUSED,
            }
        )
        await self.context.execution_store.save_execution(execution)
        wait, _ = await self.service.create_wait(
            execution_id=execution.id,
            kind=ExecutionWaitKind.INPUT,
            idempotency_key="input:non-native",
        )

        with self.assertRaisesRegex(ExecutionWaitConflictError, "requires the native runtime"):
            await self.service.resolve_wait(
                execution_id=execution.id,
                wait_id=wait.id,
                resolution_key="message:non-native",
                resume=True,
            )

        pending = await self.service.get_wait(execution.id, wait.id)
        current = await self.context.execution_store.get_execution(execution.id)
        self.assertEqual(pending.status.value, "pending")
        self.assertEqual(current.status, ExecutionStatus.WAITING_FOR_INPUT)

    async def test_expired_wait_is_paused_without_resuming(self) -> None:
        wait, _ = await self.service.create_wait(
            execution_id=self.execution.id,
            kind=ExecutionWaitKind.INPUT,
            idempotency_key="input:deadline",
            deadline_at=utc_now() - timedelta(seconds=1),
        )

        result = await self.service.wake_due_waits()

        expired = await self.service.get_wait(self.execution.id, wait.id)
        current = await self.context.execution_store.get_execution(self.execution.id)
        self.assertEqual(result["expired_scanned"], 1)
        self.assertEqual(result["due_scanned"], 0)
        self.assertEqual(expired.status.value, "expired")
        self.assertEqual(current.status, ExecutionStatus.PAUSED)

    async def test_native_resume_reconstructs_checkpoint_after_runtime_state_loss(self) -> None:
        class CompletingModelClient:
            provider_key = "wait-test"

            def __init__(self, profile, env) -> None:
                self.profile = profile

            def generate_text(self, messages, **kwargs):
                return ModelResponse(
                    content="second node complete",
                    provider=self.profile.provider,
                    model=self.profile.model,
                    latency_ms=1,
                )

            def count_tokens(self, messages, **kwargs):
                return 1

            def health_check(self):
                return {"ok": True}

        profile = ModelProfileDefinition(
            id="profile-wait-resume",
            name="Wait Resume",
            provider="wait-test",
            model="wait-model",
        )
        agent = AgentDefinition(
            id="agent-wait-resume",
            name="Wait Resumer",
            instructions="Complete the remaining task.",
            model_profile_id=profile.id,
        )
        first_task = TaskDefinition(
            id="task-wait-first",
            name="First",
            description="Already completed",
            agent_id=agent.id,
        )
        second_task = TaskDefinition(
            id="task-wait-second",
            name="Second",
            description="Complete after wake",
            agent_id=agent.id,
        )
        first_node = WorkflowNodeDefinition(
            id="node-wait-first",
            name="First",
            node_type="task",
            task_id=first_task.id,
            agent_id=agent.id,
        )
        second_node = WorkflowNodeDefinition(
            id="node-wait-second",
            name="Second",
            node_type="task",
            task_id=second_task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-wait-resume",
            name="Wait Resume",
            nodes=[first_node, second_node],
            edges=[WorkflowEdgeDefinition(source_node_id=first_node.id, target_node_id=second_node.id)],
            entrypoint=first_node.id,
            task_definitions=[first_task, second_task],
            agent_definitions=[agent],
            default_runtime_adapter_id="native",
        )
        self.context.llm_provider_registry.register(
            "wait_test",
            lambda selected_profile, env: CompletingModelClient(selected_profile, env),
        )
        await self.context.runtime_registry.register_model_profile(profile)
        await self.context.runtime_registry.register_workflow(workflow)
        execution = Execution(
            id="execution-wait-resume",
            workflow_id=workflow.id,
            runtime_adapter="native",
            status=ExecutionStatus.PAUSED,
            output_json={
                "node_outputs": {first_node.id: {"content": "first node complete"}},
                "checkpoint": {
                    "current_node_id": first_node.id,
                    "current_task_id": first_task.id,
                    "planned_node_ids": [first_node.id, second_node.id],
                    "terminal_node_ids": [second_node.id],
                    "completed_node_ids": [first_node.id],
                },
            },
        )
        await self.context.execution_store.save_execution(execution)
        wait, _ = await self.service.create_wait(
            execution_id=execution.id,
            kind=ExecutionWaitKind.INPUT,
            idempotency_key="input:resume-after-restart",
        )
        native_adapter = self.context.runtime_registry.get("native")
        native_adapter.engine._states.pop(execution.id, None)

        result = await self.service.resolve_wait(
            execution_id=execution.id,
            wait_id=wait.id,
            resolution_key="message:resume-after-restart",
            resolution_payload={"answer": "continue"},
            resume=True,
        )
        for _ in range(100):
            current = await self.context.execution_store.get_execution(execution.id)
            if current.status == ExecutionStatus.COMPLETED:
                break
            await asyncio.sleep(0.01)

        current = await self.context.execution_store.get_execution(execution.id)
        self.assertTrue(result["resumed"])
        self.assertEqual(current.id, execution.id)
        self.assertEqual(current.status, ExecutionStatus.COMPLETED)
        self.assertEqual(current.output_payload["node_outputs"][first_node.id], {"content": "first node complete"})
        self.assertIn("second node complete", str(current.output_payload["node_outputs"][second_node.id]))


class ExecutionWaitApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        app = FastAPI()
        app.include_router(create_executions_router(self.context))
        self.client = TestClient(app)
        self.client.headers.update(
            {
                "x-agency-user-id": "wait-operator",
                "x-agency-user-email": "waits@example.com",
            }
        )
        asyncio.run(
            self.context.user_repo.create(
                UserDefinition(id="wait-operator", email="waits@example.com", display_name="Wait Operator")
            )
        )
        asyncio.run(
            self.context.execution_store.save_execution(
                Execution(
                    id="execution-wait-api",
                    workflow_id="workflow-wait-api",
                    runtime_adapter="native",
                    status=ExecutionStatus.PAUSED,
                    created_by="wait-operator",
                    output_json={"node_outputs": {"node-1": {"ok": True}}},
                )
            )
        )

    def test_wait_api_create_list_resolve_and_conflict(self) -> None:
        created = self.client.post(
            "/executions/execution-wait-api/waits",
            json={
                "kind": "input",
                "idempotency_key": "input:api",
                "request_payload": {"question": "Continue?"},
                "policy": {"on_timeout": "pause"},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        wait_id = created.json()["wait"]["id"]
        self.assertTrue(created.json()["created"])
        self.assertEqual(created.json()["wait"]["policy"], {"on_timeout": "pause"})

        duplicate = self.client.post(
            "/executions/execution-wait-api/waits",
            json={"kind": "input", "idempotency_key": "input:api"},
        )
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertFalse(duplicate.json()["created"])

        listed = self.client.get("/executions/execution-wait-api/waits?status=pending")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["count"], 1)

        bypass_resume = self.client.post("/executions/execution-wait-api/resume")
        bypass_start = self.client.post("/executions/execution-wait-api/start")
        self.assertEqual(bypass_resume.status_code, 409, bypass_resume.text)
        self.assertEqual(bypass_start.status_code, 409, bypass_start.text)

        resolved = self.client.post(
            f"/executions/execution-wait-api/waits/{wait_id}/resolve",
            json={
                "resolution_key": "message:api-1",
                "resolution_payload": {"continue": True},
                "resume": False,
            },
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertTrue(resolved.json()["claimed"])

        conflicting = self.client.post(
            f"/executions/execution-wait-api/waits/{wait_id}/resolve",
            json={"resolution_key": "message:api-2", "resume": False},
        )
        self.assertEqual(conflicting.status_code, 409, conflicting.text)


class ExecutionWaitLifespanTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("EXECUTION_WAIT_POLL_INTERVAL_SECONDS", None)
        os.environ.pop("RUNTIME_RECONCILER_ENABLED", None)
        reset_settings_cache()

    def test_due_wait_polling_does_not_depend_on_runtime_reconciler(self) -> None:
        wake_due_waits = AsyncMock(return_value={"scanned": 0, "items": []})
        with patch.dict(
                os.environ,
                {
                    "EXECUTION_WAIT_POLL_INTERVAL_SECONDS": "0.01",
                    "RUNTIME_RECONCILER_ENABLED": "false",
                },
                clear=False,
        ):
            reset_settings_cache()
            with patch("app.api.main.ExecutionWaitService.wake_due_waits", wake_due_waits):
                with TestClient(create_app(context=create_test_api_context())):
                    time.sleep(0.05)

        self.assertGreaterEqual(wake_due_waits.await_count, 2)


if __name__ == "__main__":
    unittest.main()
