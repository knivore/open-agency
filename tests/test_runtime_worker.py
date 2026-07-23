from __future__ import annotations

import asyncio
from datetime import timedelta
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.context import create_test_api_context
from app.core.time import utc_now
from app.domain import AgentDefinition, ExecutionStatus, ModelProfileDefinition, TaskDefinition, WorkflowDefinition, \
    WorkflowNodeDefinition
from app.domain.models import FrameworkHints, MemorySettings
from app.llm.base import ModelResponse
from app.runtime.worker import (
    WORKER_EXIT_INFRA_FAILED,
    WORKER_EXIT_SUCCESS,
    WORKER_EXIT_SUSPENDED,
    load_worker_environment,
    run_execution_worker,
    _close_browser_sessions,
)
from app.services.execution_classification import classify_execution_staleness


class _SimpleModelClient:
    provider_key = "fake"

    def __init__(self, profile, env):
        self.profile = profile

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content="done", provider=self.profile.provider, model=self.profile.model, latency_ms=1)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content={"ok": True}, provider=self.profile.provider, model=self.profile.model,
                             latency_ms=1)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "done"

    def count_tokens(self, messages, **kwargs):
        return 1

    def health_check(self):
        return {"ok": True}


class RuntimeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake", lambda profile, env: _SimpleModelClient(profile, env))
        profile = ModelProfileDefinition(
            id="profile-worker",
            name="Worker Profile",
            provider="fake",
            model="fake-model",
            supports_tools=True,
        )
        await self.context.runtime_registry.register_model_profile(profile)
        agent = AgentDefinition(
            id="agent-worker",
            name="Worker Agent",
            instructions="Answer directly.",
            model_profile_id=profile.id,
            tool_ids=[],
            memory=MemorySettings(enabled=False),
            framework_hints=FrameworkHints(adapter_config={"max_iterations": 1}),
        )
        task = TaskDefinition(
            id="task-worker",
            name="Worker Task",
            description="Do the work",
            agent_id=agent.id,
            tool_ids=[],
        )
        node = WorkflowNodeDefinition(
            id="node-worker",
            name="Worker Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        self.workflow = WorkflowDefinition(
            id="workflow-worker",
            name="Worker Workflow",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.context.runtime_registry.register_workflow(self.workflow)

    async def test_runtime_worker_executes_native_workflow(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "worker"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
            goal_id="goal-worker",
        )
        execution.runtime_revision_id = "runtime-rev-1"
        execution.runtime_fingerprint = "fp-1"
        await self.context.execution_store.update_execution(execution)

        exit_code = await run_execution_worker(
            context=self.context,
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            runtime_revision_id="runtime-rev-1",
            runtime_adapter_id="native",
            worker_id="worker-test",
            goal_id="goal-worker",
        )

        current = await self.context.execution_store.get_execution(execution.id)
        events = await self.context.execution_store.list_events(execution.id)
        self.assertEqual(exit_code, WORKER_EXIT_SUCCESS)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status.value, "completed")
        self.assertIsNone(current.worker_id)
        self.assertEqual(current.goal_id, "goal-worker")
        self.assertEqual(current.metadata["worker_context"]["goal_id"], "goal-worker")
        self.assertEqual(current.metadata["worker_context"]["execution_id"], execution.id)
        self.assertEqual(current.metadata["worker_context"]["workflow_id"], self.workflow.id)
        self.assertIn("execution.started", [event.event_type.value for event in events])
        self.assertIn("execution.completed", [event.event_type.value for event in events])

    async def test_terminal_worker_closes_execution_browser_sessions(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "browser cleanup"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.runtime_revision_id = "runtime-rev-cleanup"
        execution.runtime_fingerprint = "fp-cleanup"
        await self.context.execution_store.update_execution(execution)

        with patch("app.runtime.worker._close_browser_sessions", new_callable=AsyncMock) as close_sessions:
            exit_code = await run_execution_worker(
                context=self.context,
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                runtime_revision_id="runtime-rev-cleanup",
                runtime_adapter_id="native",
                worker_id="worker-cleanup",
            )

        self.assertEqual(exit_code, WORKER_EXIT_SUCCESS)
        close_sessions.assert_awaited_once_with(execution.id)

    async def test_suspended_worker_preserves_browser_session_for_human_wait(self):
        workflow = self.workflow.model_copy(deep=True, update={"id": "workflow-worker-browser-wait"})
        workflow.metadata["execution_lifecycle"] = {
            "persistent_cycle": {"enabled": True, "interval_seconds": 60}
        }
        await self.context.runtime_registry.register_workflow(workflow)
        execution = await self.context.runtime_registry.create_execution(
            workflow.id,
            {"topic": "browser handoff"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.runtime_revision_id = "runtime-rev-wait"
        execution.runtime_fingerprint = "fp-wait"
        await self.context.execution_store.update_execution(execution)

        with patch("app.runtime.worker._close_browser_sessions", new_callable=AsyncMock) as close_sessions:
            exit_code = await run_execution_worker(
                context=self.context,
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                runtime_revision_id="runtime-rev-wait",
                runtime_adapter_id="native",
                worker_id="worker-wait",
            )

        self.assertEqual(exit_code, WORKER_EXIT_SUSPENDED)
        close_sessions.assert_not_awaited()

    async def test_worker_cleanup_uses_execution_scoped_runtime_capability(self):
        client = MagicMock()
        with patch.dict(os.environ, {"BROWSER_RUNTIME_EXECUTION_SECRET": "execution-secret"}, clear=False), \
                patch("app.browser_runtime.client.BrowserRuntimeClient", return_value=client):
            await _close_browser_sessions("execution-1")

        client.close_execution.assert_called_once_with(
            "execution-1",
            owner={"execution_id": "execution-1"},
        )
        client.close_client.assert_called_once_with()

    async def test_runtime_worker_exits_cleanly_when_persistent_cycle_sleeps(self):
        workflow = self.workflow.model_copy(deep=True, update={"id": "workflow-worker-cycle"})
        workflow.metadata["execution_lifecycle"] = {
            "persistent_cycle": {"enabled": True, "interval_seconds": 60}
        }
        await self.context.runtime_registry.register_workflow(workflow)
        execution = await self.context.runtime_registry.create_execution(
            workflow.id,
            {"topic": "worker cycle"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.runtime_revision_id = "runtime-rev-cycle"
        execution.runtime_fingerprint = "fp-cycle"
        await self.context.execution_store.update_execution(execution)

        exit_code = await run_execution_worker(
            context=self.context,
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            runtime_revision_id="runtime-rev-cycle",
            runtime_adapter_id="native",
            worker_id="worker-cycle",
        )

        current = await self.context.execution_store.get_execution(execution.id)
        assert current is not None
        self.assertEqual(exit_code, WORKER_EXIT_SUSPENDED)
        self.assertEqual(current.status, ExecutionStatus.SLEEPING)
        self.assertIsNotNone(current.metadata.get("active_wait"))

    def test_load_worker_environment_requires_core_values(self):
        payload = load_worker_environment(
            {
                "AGENCY_EXECUTION_ID": "execution-1",
                "AGENCY_WORKFLOW_ID": "workflow-1",
                "AGENCY_GOAL_ID": "goal-1",
                "AGENCY_RUNTIME_REVISION_ID": "runtime-rev-1",
                "AGENCY_RUNTIME_ADAPTER_ID": "native",
                "AGENCY_HEARTBEAT_INTERVAL_SECONDS": "0.5",
                "AGENCY_EXECUTION_TIMEOUT_SECONDS": "30",
            }
        )

        self.assertEqual(payload["execution_id"], "execution-1")
        self.assertEqual(payload["goal_id"], "goal-1")
        self.assertEqual(payload["worker_id"], "container-worker-execution-1")
        self.assertEqual(payload["heartbeat_interval_seconds"], 0.5)
        self.assertEqual(payload["execution_timeout_seconds"], 30.0)

    async def test_runtime_worker_updates_heartbeat_while_running(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "heartbeat"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.runtime_revision_id = "runtime-rev-1"
        execution.runtime_fingerprint = "fp-1"
        await self.context.execution_store.update_execution(execution)

        original_start = self.context.runtime_registry.start_execution

        async def slow_start(execution_id: str):
            await asyncio.sleep(0.05)
            return await original_start(execution_id)

        self.context.runtime_registry.start_execution = slow_start

        exit_code = await run_execution_worker(
            context=self.context,
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            runtime_revision_id="runtime-rev-1",
            runtime_adapter_id="native",
            worker_id="worker-heartbeat",
            heartbeat_interval_seconds=0.01,
        )

        current = await self.context.execution_store.get_execution(execution.id)
        self.assertEqual(exit_code, WORKER_EXIT_SUCCESS)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertIsNotNone(current.last_heartbeat_at)

    async def test_runtime_worker_records_initial_heartbeat_before_first_interval(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "initial-heartbeat"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.runtime_revision_id = "runtime-rev-1"
        execution.runtime_fingerprint = "fp-1"
        await self.context.execution_store.update_execution(execution)

        exit_code = await run_execution_worker(
            context=self.context,
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            runtime_revision_id="runtime-rev-1",
            runtime_adapter_id="native",
            worker_id="worker-initial-heartbeat",
            heartbeat_interval_seconds=60.0,
        )

        current = await self.context.execution_store.get_execution(execution.id)
        self.assertEqual(exit_code, WORKER_EXIT_SUCCESS)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertIsNotNone(current.last_heartbeat_at)

    async def test_long_running_worker_heartbeat_integrates_with_stale_detection(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "long-running-heartbeat"},
            {"created_by": "tester", "run_mode": "always_on"},
            runtime_adapter_id="native",
        )
        execution.runtime_revision_id = "runtime-rev-1"
        execution.runtime_fingerprint = "fp-1"
        execution.metadata["execution_lifecycle"]["run_mode"] = "always_on"
        await self.context.execution_store.update_execution(execution)

        finish_execution = asyncio.Event()

        async def long_running_start(execution_id: str):
            running = await self.context.execution_store.get_execution(execution_id)
            assert running is not None
            running.status = ExecutionStatus.RUNNING
            running.started_at = running.started_at or utc_now()
            await self.context.execution_store.update_execution(running)
            await finish_execution.wait()
            running.status = ExecutionStatus.COMPLETED
            running.completed_at = utc_now()
            await self.context.execution_store.update_execution(running)
            return running

        self.context.runtime_registry.start_execution = long_running_start
        worker_task = asyncio.create_task(
            run_execution_worker(
                context=self.context,
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                runtime_revision_id="runtime-rev-1",
                runtime_adapter_id="native",
                worker_id="worker-long-running",
                heartbeat_interval_seconds=0.01,
            )
        )
        try:
            for _ in range(50):
                current = await self.context.execution_store.get_execution(execution.id)
                if (
                        current is not None
                        and current.status == ExecutionStatus.RUNNING
                        and current.last_heartbeat_at is not None
                ):
                    break
                await asyncio.sleep(0.01)

            current = await self.context.execution_store.get_execution(execution.id)
            self.assertIsNotNone(current)
            assert current is not None
            live_classification = classify_execution_staleness(
                current,
                stale_after_seconds=1,
                idle_timeout_seconds=600,
                run_timeout_seconds=1,
            )

            self.assertFalse(live_classification["is_stale"])
            self.assertTrue(live_classification["intentionally_long_running"])
            self.assertIsNotNone(current.last_heartbeat_at)

            current.last_heartbeat_at = utc_now() - timedelta(seconds=5)
            current.updated_at = current.last_heartbeat_at
            await self.context.execution_store.update_execution(current)
            stale_classification = classify_execution_staleness(
                current,
                stale_after_seconds=1,
                idle_timeout_seconds=600,
                run_timeout_seconds=1,
            )

            self.assertTrue(stale_classification["is_stale"])
            self.assertEqual(stale_classification["stale_kind"], "worker_unresponsive")
            self.assertTrue(stale_classification["intentionally_long_running"])
        finally:
            finish_execution.set()

        exit_code = await worker_task
        self.assertEqual(exit_code, WORKER_EXIT_SUCCESS)

    async def test_runtime_worker_times_out_long_running_execution(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "timeout"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.runtime_revision_id = "runtime-rev-1"
        execution.runtime_fingerprint = "fp-1"
        await self.context.execution_store.update_execution(execution)

        async def hanging_start(execution_id: str):
            await asyncio.sleep(0.1)
            execution = await self.context.execution_store.get_execution(execution_id)
            return execution

        self.context.runtime_registry.start_execution = hanging_start

        exit_code = await run_execution_worker(
            context=self.context,
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            runtime_revision_id="runtime-rev-1",
            runtime_adapter_id="native",
            worker_id="worker-timeout",
            heartbeat_interval_seconds=0.01,
            execution_timeout_seconds=0.02,
        )

        current = await self.context.execution_store.get_execution(execution.id)
        self.assertEqual(exit_code, WORKER_EXIT_INFRA_FAILED)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status.value, "failed")
        self.assertIn("timed out", current.error or "")

    async def test_runtime_worker_hard_timeout_pauses_during_approval_wait(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "approval wait"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.runtime_revision_id = "runtime-rev-1"
        execution.runtime_fingerprint = "fp-1"
        await self.context.execution_store.update_execution(execution)
        approval_requested = asyncio.Event()
        approval_granted = asyncio.Event()

        async def approval_waiting_start(execution_id: str):
            current = await self.context.execution_store.get_execution(execution_id)
            current.status = ExecutionStatus.WAITING_FOR_APPROVAL
            await self.context.execution_store.update_execution(current)
            approval_requested.set()
            await approval_granted.wait()
            current = await self.context.execution_store.get_execution(execution_id)
            current.status = ExecutionStatus.COMPLETED
            await self.context.execution_store.update_execution(current)
            return current

        self.context.runtime_registry.start_execution = approval_waiting_start
        worker_task = asyncio.create_task(
            run_execution_worker(
                context=self.context,
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                runtime_revision_id="runtime-rev-1",
                runtime_adapter_id="native",
                worker_id="worker-approval-wait",
                heartbeat_interval_seconds=0.01,
                execution_timeout_seconds=0.02,
            )
        )

        await asyncio.wait_for(approval_requested.wait(), timeout=1)
        await asyncio.sleep(0.06)
        waiting = await self.context.execution_store.get_execution(execution.id)
        self.assertFalse(worker_task.done())
        self.assertEqual(waiting.status, ExecutionStatus.WAITING_FOR_APPROVAL)
        self.assertIsNotNone(waiting.last_heartbeat_at)

        approval_granted.set()
        exit_code = await asyncio.wait_for(worker_task, timeout=1)
        self.assertEqual(exit_code, WORKER_EXIT_SUCCESS)
