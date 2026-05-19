from __future__ import annotations

import asyncio
import unittest

from app.api.context import create_test_api_context
from app.domain import AgentDefinition, ModelProfileDefinition, TaskDefinition, WorkflowDefinition, \
    WorkflowNodeDefinition
from app.domain.models import FrameworkHints, MemorySettings
from app.llm.base import ModelResponse
from app.runtime.worker import (
    WORKER_EXIT_INFRA_FAILED,
    WORKER_EXIT_SUCCESS,
    load_worker_environment,
    run_execution_worker,
)


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
        )

        current = await self.context.execution_store.get_execution(execution.id)
        events = await self.context.execution_store.list_events(execution.id)
        self.assertEqual(exit_code, WORKER_EXIT_SUCCESS)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status.value, "completed")
        self.assertIsNone(current.worker_id)
        self.assertIn("execution.started", [event.event_type.value for event in events])
        self.assertIn("execution.completed", [event.event_type.value for event in events])

    def test_load_worker_environment_requires_core_values(self):
        payload = load_worker_environment(
            {
                "AGENCY_EXECUTION_ID": "execution-1",
                "AGENCY_WORKFLOW_ID": "workflow-1",
                "AGENCY_RUNTIME_REVISION_ID": "runtime-rev-1",
                "AGENCY_RUNTIME_ADAPTER_ID": "native",
                "AGENCY_HEARTBEAT_INTERVAL_SECONDS": "0.5",
                "AGENCY_EXECUTION_TIMEOUT_SECONDS": "30",
            }
        )

        self.assertEqual(payload["execution_id"], "execution-1")
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
