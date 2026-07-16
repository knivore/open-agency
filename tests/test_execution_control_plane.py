from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.api.routes.executions import create_executions_router
from app.core.config import reset_settings_cache
from app.core.time import utc_now
from app.domain import AgentDefinition, Execution, ExecutionArtifact, ExecutionEvent, ExecutionEventType, \
    ExecutionStatus, \
    ExecutionWait, ExecutionWaitKind, ExecutionWaitStatus, \
    ModelProfileDefinition, RuntimeRevision, RuntimeRevisionStatus, TaskDefinition, ToolDefinition, UserDefinition, \
    WorkflowDefinition, WorkflowEdgeDefinition, WorkflowNodeDefinition
from app.domain.models import FrameworkHints, MCPExposureSettings, MemorySettings, SecuritySettings, \
    ToolImplementationReference
from app.llm.base import ModelResponse, ModelToolCall
from app.runtime.control_plane import ExecutionControlPlane
from app.runtime.native.approvals import ApprovalManager
from app.runtime.containers import RuntimeContainerState


class ApprovalAwareModelClient:
    provider_key = "fake"

    def __init__(self, profile, env, *, mode="approval"):
        self.profile = profile
        self.mode = mode
        self.calls = 0

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        self.calls += 1
        if any(message.role == "tool" for message in messages):
            content = "Click completed" if self.mode == "computer_use" else "Approved answer"
            return ModelResponse(
                content=content,
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        if self.mode == "multi_tool_approval":
            return ModelResponse(
                content="Read first, then request approval",
                tool_calls=[
                    ModelToolCall(id="tool-read", name="Read Tool", arguments={"text": "already done"}),
                    ModelToolCall(
                        id="tool-approval",
                        name="Approval Tool",
                        arguments={"text": "approve me"},
                    ),
                ],
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        if self.mode == "computer_use":
            if self.calls == 1:
                return ModelResponse(
                    content="Need desktop mutation approval",
                    tool_calls=[
                        ModelToolCall(
                            id="tool-click",
                            name="click",
                            arguments={"x": 42, "y": 24, "token": "secret-token"},
                        )
                    ],
                    provider=self.profile.provider,
                    model=self.profile.model,
                    latency_ms=1,
                )
            return ModelResponse(content="Click completed", provider=self.profile.provider, model=self.profile.model,
                                 latency_ms=1)
        if self.mode == "approval":
            if self.calls == 1:
                return ModelResponse(
                    content="Need approval",
                    tool_calls=[
                        ModelToolCall(id="tool-approval", name="Approval Tool", arguments={"text": "approve me"})],
                    provider=self.profile.provider,
                    model=self.profile.model,
                    latency_ms=1,
                )
            return ModelResponse(content="Approved answer", provider=self.profile.provider, model=self.profile.model,
                                 latency_ms=1)
        return ModelResponse(content="Immediate answer", provider=self.profile.provider, model=self.profile.model,
                             latency_ms=1)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content={"ok": True}, provider=self.profile.provider, model=self.profile.model,
                             latency_ms=1)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "chunk"

    def count_tokens(self, messages, **kwargs):
        return 1

    def health_check(self):
        return {"ok": True}


class HangingModelClient(ApprovalAwareModelClient):
    async def agenerate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        await asyncio.sleep(0.05)
        return ModelResponse(content="done", provider=self.profile.provider, model=self.profile.model, latency_ms=1)


class ExecutionControlPlaneAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake", lambda profile, env: ApprovalAwareModelClient(profile, env))
        self.profile = ModelProfileDefinition(
            id="profile-approval",
            name="Approval Profile",
            provider="fake",
            model="fake-model",
            supports_tools=True,
        )
        asyncio.run(self._initialize_runtime_fixture())

    async def _initialize_runtime_fixture(self):
        await self.context.runtime_registry.register_model_profile(self.profile)

        tool = ToolDefinition(
            id="tool-approval",
            name="Approval Tool",
            description="Requires approval",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(
                approval_required=True,
                sandbox_required=True,
                module_allowlist=["tests.native_test_tools"],
                function_allowlist=["echo_tool"],
            ),
            mcp_exposure=MCPExposureSettings(),
        )
        agent = AgentDefinition(
            id="agent-approval",
            name="Approver",
            instructions="Use the tool when needed.",
            model_profile_id=self.profile.id,
            tool_ids=[tool.id],
            memory=MemorySettings(enabled=False),
            framework_hints=FrameworkHints(adapter_config={"max_iterations": 3}),
        )
        task = TaskDefinition(
            id="task-approval",
            name="Approval Task",
            description="Need approval",
            agent_id=agent.id,
            tool_ids=[tool.id],
        )
        node = WorkflowNodeDefinition(
            id="node-approval",
            name="Approval Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        self.workflow = WorkflowDefinition(
            id="workflow-approval",
            name="Approval Workflow",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[tool],
            default_runtime_adapter_id="native",
        )
        await self.context.runtime_registry.register_workflow(self.workflow)

    async def _wait_for_execution_status(self, execution_id: str, expected_status: str):
        async with asyncio.timeout(2):
            while True:
                execution = await self.context.execution_store.get_execution(execution_id)
                if execution is not None and execution.status.value == expected_status:
                    return execution
                if execution is not None and execution.status.value in {"completed", "failed", "cancelled"}:
                    events = await self.context.execution_store.list_events(execution_id)
                    self.fail(
                        f"Execution reached {execution.status.value} while waiting for {expected_status}: "
                        f"{execution.error}; events="
                        f"{[(event.event_type.value, event.payload.get('tool_id')) for event in events]}"
                    )
                await asyncio.sleep(0.01)

    async def test_terminal_executions_cannot_start_resume_or_requeue(self):
        for terminal_status in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        ):
            with self.subTest(status=terminal_status.value):
                execution = await self.context.runtime_registry.create_execution(
                    self.workflow.id,
                    {"topic": "terminal replay guard"},
                    {"created_by": "tester"},
                    runtime_adapter_id="native",
                )
                execution.status = terminal_status
                await self.context.execution_store.update_execution(execution)
                state = self.context.execution_engine._states[execution.id]  # noqa: SLF001
                state.paused = True
                state.cancelled = True

                for transition in (
                    self.context.execution_engine.start_execution,
                    self.context.execution_engine.resume_execution,
                    self.context.control_plane.queue_start,
                    self.context.control_plane.resume,
                ):
                    with self.subTest(status=terminal_status.value, transition=transition.__name__):
                        with self.assertRaisesRegex(ValueError, "must be retried through a replacement execution"):
                            await transition(execution.id)
                        current = await self.context.execution_store.get_execution(execution.id)
                        self.assertEqual(current.status, terminal_status)
                        self.assertTrue(state.paused)
                        self.assertTrue(state.cancelled)
                        self.assertNotIn(execution.id, self.context.control_plane._tasks)  # noqa: SLF001

    async def test_native_resume_preserves_valid_paused_transition(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "valid resume"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.status = ExecutionStatus.PAUSED
        await self.context.execution_store.update_execution(execution)
        state = self.context.execution_engine._states[execution.id]  # noqa: SLF001
        state.paused = True

        resumed = await self.context.execution_engine.resume_execution(execution.id)

        self.assertEqual(resumed.status, ExecutionStatus.QUEUED)
        self.assertFalse(state.paused)

    async def test_approval_flow(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "approval"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        await self.context.control_plane.queue_start(execution.id)
        await asyncio.sleep(0.05)

        current = await self.context.execution_store.get_execution(execution.id)
        self.assertIsNotNone(current)
        self.assertEqual(current.status.value, "waiting_for_approval")
        async with asyncio.timeout(2):
            while current.worker_id is not None:
                await asyncio.sleep(0.01)
                current = await self.context.execution_store.get_execution(execution.id)
        self.assertIsNone(current.worker_id)
        self.assertNotIn(execution.id, self.context.control_plane._tasks)  # noqa: SLF001

        # Isolated workers can still be unwinding after the wait becomes
        # visible. The approval path must not queue the replacement until that
        # durable lock is released.
        current.worker_id = "container-worker-finishing"
        current.last_heartbeat_at = utc_now()
        await self.context.execution_store.update_execution(current)

        async def release_finishing_worker() -> None:
            await asyncio.sleep(0.05)
            await self.context.execution_store.release_lock(
                execution.id,
                "container-worker-finishing",
            )

        release_task = asyncio.create_task(release_finishing_worker())

        approved = await self.context.control_plane.approve(execution.id, "tool-approval", "approved in test")
        await release_task
        self.assertTrue(approved)
        await asyncio.sleep(0.05)

        final = await self.context.execution_store.get_execution(execution.id)
        self.assertIsNotNone(final)
        self.assertEqual(final.status.value, "completed")
        event_types = [
            event.event_type.value
            for event in await self.context.execution_store.list_events(execution.id)
        ]
        self.assertIn("execution.waiting", event_types)
        self.assertIn("execution.woken", event_types)

    async def test_low_risk_approval_can_be_delegated_to_main_agent(self):
        workflow = self.workflow.model_copy(
            deep=True,
            update={
                "id": "workflow-approval-delegated",
                "metadata": {
                    "main_agent_monitoring": {
                        "delegate_hitl_to_main_agent": True,
                    }
                },
            },
        )
        await self.context.runtime_registry.register_workflow(workflow)

        execution = await self.context.runtime_registry.create_execution(
            workflow.id,
            {"topic": "delegated-approval"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        await self.context.control_plane.queue_start(execution.id)
        final = await self._wait_for_execution_status(execution.id, "completed")
        events = await self.context.execution_store.list_events(execution.id)

        assert final is not None
        self.assertEqual(final.status.value, "completed")
        self.assertNotIn("pending_approval", final.metadata)
        approval_grants = [event for event in events if event.event_type.value == "approval.granted"]
        self.assertTrue(approval_grants)
        decision_metadata = approval_grants[-1].payload["decision_metadata"]
        self.assertEqual(decision_metadata["mode"], "delegated")
        self.assertEqual(decision_metadata["delegate"], "main_agent")
        self.assertEqual(decision_metadata["risk_gate"], "low_risk_only")
        approval_requests = await self.context.execution_store.list_approval_requests(execution.id)
        self.assertEqual(len(approval_requests), 1)
        self.assertEqual(approval_requests[0]["status"], "approved")
        self.assertEqual(approval_requests[0]["responded_by"], "main_agent")
        self.assertEqual(approval_requests[0]["response_payload"]["metadata"]["mode"], "delegated")

    async def test_workerless_approval_resume_does_not_repeat_prior_tool_calls(self):
        self.context.llm_provider_registry.register(
            "fake",
            lambda profile, env: ApprovalAwareModelClient(profile, env, mode="multi_tool_approval"),
        )
        read_tool = self.workflow.tool_definitions[0].model_copy(
            deep=True,
            update={
                "id": "tool-read",
                "name": "Read Tool",
                "display_name": "Read Tool",
                "security": SecuritySettings(
                    approval_required=False,
                    sandbox_required=True,
                    module_allowlist=["tests.native_test_tools"],
                    function_allowlist=["echo_tool"],
                ),
            },
        )
        approval_tool = self.workflow.tool_definitions[0]
        agent = self.workflow.agent_definitions[0].model_copy(
            deep=True,
            update={"id": "agent-multi-approval", "tool_ids": [read_tool.id, approval_tool.id]},
        )
        task = self.workflow.task_definitions[0].model_copy(
            deep=True,
            update={
                "id": "task-multi-approval",
                "agent_id": agent.id,
                "tool_ids": [read_tool.id, approval_tool.id],
            },
        )
        node = self.workflow.nodes[0].model_copy(
            deep=True,
            update={"id": "node-multi-approval", "task_id": task.id, "agent_id": agent.id},
        )
        workflow = self.workflow.model_copy(
            deep=True,
            update={
                "id": "workflow-multi-approval",
                "nodes": [node],
                "entrypoint": node.id,
                "task_definitions": [task],
                "agent_definitions": [agent],
                "tool_definitions": [read_tool, approval_tool],
            },
        )
        await self.context.runtime_registry.register_workflow(workflow)
        execution = await self.context.runtime_registry.create_execution(
            workflow.id,
            {"topic": "multi-tool-approval"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        await self.context.control_plane.queue_start(execution.id)
        waiting = await self._wait_for_execution_status(execution.id, "waiting_for_approval")
        async with asyncio.timeout(2):
            while waiting.worker_id is not None:
                await asyncio.sleep(0.01)
                waiting = await self.context.execution_store.get_execution(execution.id)

        before_events = await self.context.execution_store.list_events(execution.id)
        read_completions = [
            event for event in before_events
            if event.event_type == ExecutionEventType.TOOL_CALL_COMPLETED
            and event.payload.get("tool_id") == read_tool.id
        ]
        self.assertEqual(len(read_completions), 1)

        # Rebuild the process-local pieces while retaining only the durable
        # execution, event, wait, and approval records.
        self.context.execution_engine._states.clear()  # noqa: SLF001
        restarted_manager = ApprovalManager(self.context.execution_store, poll_interval_seconds=0.01)
        self.context.execution_engine.approval_manager = restarted_manager
        self.context.execution_engine.agent_executor.tool_executor.approval_manager = restarted_manager
        restarted_control_plane = ExecutionControlPlane(
            runtime_registry=self.context.runtime_registry,
            execution_store=self.context.execution_store,
            approval_manager=restarted_manager,
            execution_isolation_enabled=False,
            worker_id="restarted-test-worker",
        )
        self.assertTrue(await restarted_control_plane.approve(
            execution.id,
            approval_tool.id,
            "approved after restart",
        ))
        final = await self._wait_for_execution_status(execution.id, "completed")
        self.assertEqual(final.status.value, "completed")

        final_events = await self.context.execution_store.list_events(execution.id)
        read_completions = [
            event for event in final_events
            if event.event_type == ExecutionEventType.TOOL_CALL_COMPLETED
            and event.payload.get("tool_id") == read_tool.id
        ]
        approval_completions = [
            event for event in final_events
            if event.event_type == ExecutionEventType.TOOL_CALL_COMPLETED
            and event.payload.get("tool_id") == approval_tool.id
        ]
        self.assertEqual(len(read_completions), 1)
        self.assertEqual(len(approval_completions), 1)

    async def test_computer_use_mutation_requires_approval_and_completes_after_approval(self):
        self.context.llm_provider_registry.register(
            "fake",
            lambda profile, env: ApprovalAwareModelClient(profile, env, mode="computer_use"),
        )
        tool = ToolDefinition(
            id="tool-click",
            name="click",
            description="Computer use click",
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
                target="tests.native_test_tools",
                callable_name="computer_use_click",
                config={"tool_family": "computer_use", "canonical_tool_name": "click"},
            ),
            security=SecuritySettings(
                approval_required=True,
                sandbox_required=True,
                redaction_enabled=True,
                redaction_rules=["token"],
                module_allowlist=["tests.native_test_tools"],
                function_allowlist=["computer_use_click"],
            ),
            mcp_exposure=MCPExposureSettings(),
        )
        agent = AgentDefinition(
            id="agent-click",
            name="Desktop Agent",
            instructions="Use the click tool.",
            model_profile_id=self.profile.id,
            tool_ids=[tool.id],
            memory=MemorySettings(enabled=False),
            framework_hints=FrameworkHints(adapter_config={"max_iterations": 3}),
        )
        task = TaskDefinition(
            id="task-click",
            name="Click Task",
            description="Click the desktop",
            agent_id=agent.id,
            tool_ids=[tool.id],
        )
        node = WorkflowNodeDefinition(
            id="node-click",
            name="Click Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-click",
            name="Computer Use Click Workflow",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[tool],
            default_runtime_adapter_id="native",
            metadata={"main_agent_monitoring": {"delegate_hitl_to_main_agent": True}},
        )
        await self.context.runtime_registry.register_workflow(workflow)

        execution = await self.context.runtime_registry.create_execution(
            workflow.id,
            {"topic": "computer-use-click"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        await self.context.control_plane.queue_start(execution.id)
        await asyncio.sleep(0.05)

        waiting = await self.context.execution_store.get_execution(execution.id)
        events = await self.context.execution_store.list_events(execution.id)

        assert waiting is not None
        self.assertIn(waiting.status.value, {"waiting_for_approval", "running", "completed"})
        approval_events = [event for event in events if event.event_type.value == "approval.requested"]
        self.assertTrue(approval_events)
        self.assertEqual(approval_events[-1].payload["tool_name"], "click")
        self.assertEqual(approval_events[-1].payload["arguments"]["token"], "[REDACTED]")
        self.assertIn("browser", approval_events[-1].payload["risk_labels"])
        self.assertIn("local_privileged_execution", approval_events[-1].payload["risk_labels"])
        self.assertTrue(approval_events[-1].payload["local_privileged_execution"])
        approval_grants_before_manual = [event for event in events if event.event_type.value == "approval.granted"]
        self.assertEqual(approval_grants_before_manual, [])

        approved = await self.context.control_plane.approve(execution.id, "tool-click", "approved")
        self.assertTrue(approved)
        await asyncio.sleep(0.05)

        final = await self.context.execution_store.get_execution(execution.id)
        final_events = await self.context.execution_store.list_events(execution.id)

        assert final is not None
        self.assertEqual(final.status.value, "completed")
        self.assertIn("tool.call.completed", [event.event_type.value for event in final_events])
        completed_payloads = [
            event.payload for event in final_events if event.event_type.value == "tool.call.completed"
        ]
        self.assertTrue(completed_payloads)
        self.assertEqual(completed_payloads[-1]["output"]["x"], 42)
        self.assertEqual(completed_payloads[-1]["output"]["y"], 24)

    async def test_rejected_computer_use_mutation_blocks_execution_cleanly(self):
        self.context.llm_provider_registry.register(
            "fake",
            lambda profile, env: ApprovalAwareModelClient(profile, env, mode="computer_use"),
        )
        tool = ToolDefinition(
            id="tool-click",
            name="click",
            description="Computer use click",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                "required": ["x", "y"],
            },
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(
                target="tests.native_test_tools",
                callable_name="computer_use_click",
                config={"tool_family": "computer_use", "canonical_tool_name": "click"},
            ),
            security=SecuritySettings(
                approval_required=True,
                sandbox_required=True,
                module_allowlist=["tests.native_test_tools"],
                function_allowlist=["computer_use_click"],
            ),
            mcp_exposure=MCPExposureSettings(),
        )
        agent = AgentDefinition(
            id="agent-click-reject",
            name="Desktop Agent",
            instructions="Use the click tool.",
            model_profile_id=self.profile.id,
            tool_ids=[tool.id],
            memory=MemorySettings(enabled=False),
            framework_hints=FrameworkHints(adapter_config={"max_iterations": 3}),
        )
        task = TaskDefinition(
            id="task-click-reject",
            name="Click Task",
            description="Click the desktop",
            agent_id=agent.id,
            tool_ids=[tool.id],
        )
        node = WorkflowNodeDefinition(
            id="node-click-reject",
            name="Click Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-click-reject",
            name="Rejected Computer Use Click Workflow",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[tool],
            default_runtime_adapter_id="native",
        )
        await self.context.runtime_registry.register_workflow(workflow)

        execution = await self.context.runtime_registry.create_execution(
            workflow.id,
            {"topic": "computer-use-click-reject"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        await self.context.control_plane.queue_start(execution.id)
        await asyncio.sleep(0.05)

        rejected = await self.context.control_plane.reject(execution.id, "tool-click", "denied")
        self.assertTrue(rejected)
        await asyncio.sleep(0.05)

        final = await self.context.execution_store.get_execution(execution.id)
        final_events = await self.context.execution_store.list_events(execution.id)

        assert final is not None
        self.assertEqual(final.status.value, "failed")
        self.assertIn("Approval rejected", final.error or "")
        event_types = [event.event_type.value for event in final_events]
        self.assertIn("approval.rejected", event_types)
        self.assertIn("tool.call.failed", event_types)
        self.assertNotIn("tool.call.completed", event_types)

    async def test_cancellation(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "cancel"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        await self.context.control_plane.cancel(execution.id)
        final = await self.context.execution_store.get_execution(execution.id)
        await self.context.control_plane.cancel(execution.id)
        final_events = await self.context.execution_store.list_events(execution.id)

        self.assertEqual(final.status.value, "cancelled")
        self.assertEqual(
            [event.event_type for event in final_events].count(ExecutionEventType.EXECUTION_CANCELLED),
            1,
        )

    async def test_cancellation_after_state_reload_keeps_event_sequence(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "cancel"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        adapter = self.context.runtime_registry.get("native")
        adapter.engine._states.pop(execution.id, None)

        await self.context.control_plane.cancel(execution.id)

        events = await self.context.execution_store.list_events(execution.id)
        self.assertEqual([event.sequence for event in events], [1, 2])
        self.assertEqual(events[-1].event_type, ExecutionEventType.EXECUTION_CANCELLED)
        self.assertEqual(events[-1].parent_event_id, events[0].id)

    async def test_pause_resume(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "pause"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        paused = await self.context.control_plane.pause(execution.id)
        self.assertEqual(paused.status.value, "paused")
        resumed = await self.context.control_plane.resume(execution.id)
        self.assertIn(resumed.status.value, {"queued", "running", "completed"})

    async def test_pause_sleeping_execution_cancels_cycle_timer(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "pause cycle"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        wait = await self.context.execution_store.create_execution_wait(
            ExecutionWait(
                execution_id=execution.id,
                kind=ExecutionWaitKind.SLEEP,
                idempotency_key="persistent-cycle:2",
                wake_at=utc_now() + timedelta(minutes=5),
                metadata={"source": "persistent_cycle"},
            )
        )
        execution.status = execution.status.__class__.SLEEPING
        execution.metadata["active_wait"] = {"wait_id": wait.id, "kind": "sleep"}
        await self.context.execution_store.update_execution(execution)

        paused = await self.context.control_plane.pause(execution.id)
        waits = await self.context.execution_store.list_execution_waits(execution.id)

        self.assertEqual(paused.status.value, "paused")
        self.assertEqual(waits[0].status, ExecutionWaitStatus.CANCELLED)
        self.assertNotIn("active_wait", paused.metadata)

    async def test_cancel_sleeping_execution_cancels_cycle_timer(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "cancel cycle"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        wait = await self.context.execution_store.create_execution_wait(
            ExecutionWait(
                execution_id=execution.id,
                kind=ExecutionWaitKind.SLEEP,
                idempotency_key="persistent-cycle:2",
                wake_at=utc_now() + timedelta(minutes=5),
                metadata={"source": "persistent_cycle"},
            )
        )
        execution.status = execution.status.__class__.SLEEPING
        execution.metadata["active_wait"] = {"wait_id": wait.id, "kind": "sleep"}
        await self.context.execution_store.update_execution(execution)

        cancelled = await self.context.control_plane.cancel(execution.id)
        waits = await self.context.execution_store.list_execution_waits(execution.id)

        self.assertEqual(cancelled.status.value, "cancelled")
        self.assertEqual(waits[0].status, ExecutionWaitStatus.CANCELLED)
        self.assertNotIn("active_wait", cancelled.metadata)

    async def test_stale_execution_recovery(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "stale"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.status = execution.status.__class__.RUNNING
        execution.worker_id = "old-worker"
        execution.last_heartbeat_at = utc_now() - timedelta(seconds=10)
        await self.context.execution_store.update_execution(execution)

        recovered = await self.context.control_plane.recover_stale_executions()
        current = await self.context.execution_store.get_execution(execution.id)
        self.assertIn(execution.id, recovered)
        self.assertEqual(current.status.value, "queued")
        self.assertIsNone(current.worker_id)

    async def test_stale_approval_wait_is_failed_and_its_request_is_expired(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "abandoned approval"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.status = execution.status.__class__.WAITING_FOR_APPROVAL
        execution.worker_id = "dead-approval-worker"
        execution.last_heartbeat_at = utc_now() - timedelta(seconds=10)
        execution.metadata = {
            **execution.metadata,
            "pending_approval": {"tool_id": "tool-approval", "payload": {"text": "approve me"}},
        }
        await self.context.execution_store.update_execution(execution)
        request_id = await self.context.execution_store.create_approval_request(
            execution_id=execution.id,
            event_id=None,
            tool_id="tool-approval",
            status="pending",
            payload={"arguments": {"text": "approve me"}},
        )

        repaired = await self.context.control_plane.repair_stale_executions(execution_id=execution.id)

        current = await self.context.execution_store.get_execution(execution.id)
        approval = await self.context.execution_store.get_approval_request(request_id)
        events = await self.context.execution_store.list_events(execution.id)
        self.assertEqual(repaired[0]["repair_action"], "failed_abandoned_approval")
        self.assertEqual(current.status.value, "failed")
        self.assertIsNotNone(current.completed_at)
        self.assertIn("worker stopped heartbeating", current.error)
        self.assertNotIn("pending_approval", current.metadata)
        self.assertEqual(current.metadata["stale_repair"]["expired_approval_request_ids"], [request_id])
        self.assertEqual(approval["status"], "expired")
        self.assertFalse(approval["response_payload"]["granted"])
        self.assertEqual(approval["responded_by"], "runtime_reconciler")
        self.assertEqual(events[-1].event_type, ExecutionEventType.EXECUTION_FAILED)

    async def test_live_approval_wait_is_not_repaired(self):
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "live approval"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.status = execution.status.__class__.WAITING_FOR_APPROVAL
        execution.worker_id = "live-approval-worker"
        execution.last_heartbeat_at = utc_now()
        await self.context.execution_store.update_execution(execution)

        repaired = await self.context.control_plane.repair_stale_executions(execution_id=execution.id)

        current = await self.context.execution_store.get_execution(execution.id)
        self.assertEqual(repaired, [])
        self.assertEqual(current.status.value, "waiting_for_approval")
        self.assertEqual(current.worker_id, "live-approval-worker")

    async def test_stale_execution_repair_handles_queued_running_and_cancelling(self):
        old_heartbeat = utc_now() - timedelta(seconds=10)
        executions = {}
        for status in ("queued", "running", "cancelling"):
            execution = await self.context.runtime_registry.create_execution(
                self.workflow.id,
                {"topic": status},
                {"created_by": "tester"},
                runtime_adapter_id="native",
            )
            execution.status = execution.status.__class__(status)
            execution.worker_id = f"old-worker-{status}"
            execution.last_heartbeat_at = old_heartbeat
            if status == "cancelling":
                execution.output_payload = {
                    "node_outputs": {"node-a": {"partial": True}},
                    "checkpoint": {"current_node_id": "node-b"},
                }
            await self.context.execution_store.update_execution(execution)
            if status == "cancelling":
                await self.context.execution_store.save_artifact(
                    ExecutionArtifact(
                        id="artifact-stale-partial",
                        execution_id=execution.id,
                        artifact_type="log",
                        name="partial.log",
                        content_text="partial",
                        size_bytes=7,
                    )
                )
            executions[status] = execution

        repaired = await self.context.control_plane.repair_stale_executions()

        self.assertEqual(
            {item["previous_status"]: item["repair_action"] for item in repaired},
            {
                "queued": "requeued",
                "running": "requeued",
                "cancelling": "marked_cancelled",
            },
        )
        for status in ("queued", "running"):
            current = await self.context.execution_store.get_execution(executions[status].id)
            self.assertEqual(current.status.value, "queued")
            self.assertIsNone(current.worker_id)
            self.assertIsNone(current.last_heartbeat_at)
            self.assertEqual(current.metadata["stale_repair"]["previous_status"], status)
            events = await self.context.execution_store.list_events(current.id)
            self.assertEqual(events[-1].event_type, ExecutionEventType.EXECUTION_REPAIRED)

        cancelling = await self.context.execution_store.get_execution(executions["cancelling"].id)
        self.assertEqual(cancelling.status.value, "cancelled")
        self.assertIsNotNone(cancelling.completed_at)
        self.assertEqual(cancelling.output_payload["node_outputs"]["node-a"], {"partial": True})
        cancelling_artifacts = await self.context.execution_store.list_artifacts(cancelling.id)
        self.assertEqual([artifact.id for artifact in cancelling_artifacts], ["artifact-stale-partial"])
        preservation = cancelling.metadata["partial_result_preservation"]
        self.assertEqual(preservation["reason"], "stale_execution_marked_cancelled")
        self.assertEqual(preservation["artifact_count"], 1)
        self.assertEqual(preservation["artifacts"][0]["artifact_id"], "artifact-stale-partial")
        self.assertEqual(preservation["node_output_ids"], ["node-a"])
        cancelling_events = await self.context.execution_store.list_events(cancelling.id)
        self.assertEqual(cancelling_events[-1].event_type, ExecutionEventType.EXECUTION_CANCELLED)
        self.assertEqual(cancelling_events[-1].payload["partial_result_preservation"], preservation)

        metrics = self.context.runtime_operations.snapshot_dict()
        self.assertEqual(metrics["counters"]["stale_execution_repairs"], 3)
        self.assertEqual(metrics["counters"]["stale_execution_repairs.requeued"], 2)
        self.assertEqual(metrics["counters"]["stale_execution_repairs.marked_cancelled"], 1)

    async def test_stale_repair_preserves_paused_and_durable_waits(self):
        old_heartbeat = utc_now() - timedelta(seconds=10)
        executions = []
        for status in (
                "paused",
                "waiting_for_input",
                "waiting_for_event",
                "sleeping",
        ):
            execution = await self.context.runtime_registry.create_execution(
                self.workflow.id,
                {"topic": status},
                {"created_by": "tester"},
                runtime_adapter_id="native",
            )
            execution.status = execution.status.__class__(status)
            execution.worker_id = f"worker-{status}"
            execution.last_heartbeat_at = old_heartbeat
            await self.context.execution_store.update_execution(execution)
            executions.append(execution)

        repaired = await self.context.control_plane.repair_stale_executions()

        self.assertEqual(repaired, [])
        for execution in executions:
            current = await self.context.execution_store.get_execution(execution.id)
            self.assertEqual(current.status, execution.status)
            self.assertNotIn("stale_repair", current.metadata)

    async def test_stale_execution_repair_marks_completed_when_checkpoint_finished_terminal_nodes(self):
        agent = self.workflow.agent_definitions[0]
        first_task = TaskDefinition(
            id="task-stale-first",
            name="First Task",
            description="First step",
            agent_id=agent.id,
        )
        final_task = TaskDefinition(
            id="task-stale-final",
            name="Final Task",
            description="Final step",
            agent_id=agent.id,
        )
        first_node = WorkflowNodeDefinition(
            id="node-stale-first",
            name="First Node",
            node_type="task",
            task_id=first_task.id,
            agent_id=agent.id,
        )
        final_node = WorkflowNodeDefinition(
            id="node-stale-final",
            name="Final Node",
            node_type="task",
            task_id=final_task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-stale-completed-checkpoint",
            name="Stale Completed Checkpoint Workflow",
            nodes=[first_node, final_node],
            edges=[
                WorkflowEdgeDefinition(
                    id="edge-stale-final",
                    source_node_id=first_node.id,
                    target_node_id=final_node.id,
                )
            ],
            entrypoint=first_node.id,
            task_definitions=[first_task, final_task],
            agent_definitions=[agent],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.context.runtime_registry.register_workflow(workflow)
        execution = await self.context.runtime_registry.create_execution(
            workflow.id,
            {"topic": "stale-completed"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.status = execution.status.__class__.RUNNING
        execution.worker_id = "old-worker-completed"
        execution.last_heartbeat_at = utc_now() - timedelta(seconds=60)
        execution.output_payload = {
            "checkpoint": {
                "current_node_id": final_node.id,
                "current_task_id": final_task.id,
                "completed_node_ids": [first_node.id, final_node.id],
                "planned_node_ids": [first_node.id, final_node.id],
                "terminal_node_ids": [final_node.id],
            },
            "final_output": "External delivery already succeeded",
            "node_outputs": {
                first_node.id: "first output",
                final_node.id: "External delivery already succeeded",
            },
        }
        await self.context.execution_store.update_execution(execution)
        future_task = TaskDefinition(
            id="task-stale-future",
            name="Future Task",
            description="Task added after this execution started",
            agent_id=agent.id,
        )
        future_node = WorkflowNodeDefinition(
            id="node-stale-future",
            name="Future Node",
            node_type="task",
            task_id=future_task.id,
            agent_id=agent.id,
        )
        await self.context.runtime_registry.register_workflow(
            workflow.model_copy(
                deep=True,
                update={
                    "nodes": [*workflow.nodes, future_node],
                    "edges": [
                        *workflow.edges,
                        WorkflowEdgeDefinition(
                            id="edge-stale-future",
                            source_node_id=final_node.id,
                            target_node_id=future_node.id,
                        ),
                    ],
                    "task_definitions": [*workflow.task_definitions, future_task],
                },
            )
        )

        repaired = await self.context.control_plane.repair_stale_executions(execution_id=execution.id)

        self.assertEqual(repaired[0]["repair_action"], "marked_completed")
        self.assertEqual(repaired[0]["new_status"], "completed")
        current = await self.context.execution_store.get_execution(execution.id)
        self.assertEqual(current.status.value, "completed")
        self.assertIsNotNone(current.completed_at)
        self.assertIsNone(current.worker_id)
        self.assertIsNone(current.last_heartbeat_at)
        self.assertIsNone(current.error)
        self.assertNotIn("partial_result_preservation", current.metadata)
        self.assertEqual(current.metadata["stale_repair"]["repair_action"], "marked_completed")
        self.assertEqual(current.output_payload["final_output"], "External delivery already succeeded")
        events = await self.context.execution_store.list_events(current.id)
        self.assertEqual(events[-1].event_type, ExecutionEventType.EXECUTION_COMPLETED)
        self.assertEqual(events[-1].payload["repair_action"], "marked_completed")
        self.assertEqual(events[-1].payload["output"]["final_output"], "External delivery already succeeded")

    async def test_stale_execution_repair_removes_stale_container_before_requeue(self):
        class FakeRuntimeContainerManager:
            def __init__(self):
                self.stopped = []
                self.removed = []

            def inspect_container(self, container_id: str):
                return RuntimeContainerState(
                    container_id=container_id,
                    name="agency-execution-container-stale",
                    image="agency-runtime:stale",
                    status="running",
                    labels={"agency.execution_id": "execution-stale-container"},
                    started_at=utc_now() - timedelta(seconds=30),
                )

            def stop_container(self, container_id: str):
                self.stopped.append(container_id)
                return RuntimeContainerState(
                    container_id=container_id,
                    name="agency-execution-container-stale",
                    image="agency-runtime:stale",
                    status="exited",
                    labels={"agency.execution_id": "execution-stale-container"},
                    started_at=utc_now() - timedelta(seconds=30),
                    finished_at=utc_now(),
                    exit_code=0,
                )

            def remove_container(self, container_id: str, *, force: bool = False):
                self.removed.append((container_id, force))

        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "stale-container"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.status = execution.status.__class__.RUNNING
        execution.worker_id = "old-worker-container"
        execution.last_heartbeat_at = utc_now() - timedelta(seconds=10)
        execution.runtime_revision_id = "runtime-rev-stale"
        execution.container_id = "container-stale"
        execution.container_name = "agency-execution-container-stale"
        execution.container_image = "agency-runtime:stale"
        execution.container_status = "running"
        await self.context.execution_store.update_execution(execution)

        manager = FakeRuntimeContainerManager()
        self.context.control_plane.runtime_container_manager = manager

        repaired = await self.context.control_plane.repair_stale_executions(execution_id=execution.id)

        current = await self.context.execution_store.get_execution(execution.id)
        events = await self.context.execution_store.list_events(execution.id)
        self.assertEqual(repaired[0]["repair_action"], "requeued")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status.value, "queued")
        self.assertEqual(current.container_status, "removed")
        self.assertIsNone(current.worker_id)
        self.assertIsNone(current.last_heartbeat_at)
        self.assertEqual(manager.stopped, ["container-stale"])
        self.assertEqual(manager.removed, [("container-stale", True)])
        container_repair = current.metadata["stale_repair"]["container_repair"]
        self.assertEqual(container_repair["action"], "stopped_and_removed")
        self.assertEqual(container_repair["container_id"], "container-stale")
        event_types = [event.event_type for event in events]
        self.assertIn(ExecutionEventType.CONTAINER_STOPPED, event_types)
        self.assertEqual(events[-1].event_type, ExecutionEventType.EXECUTION_REPAIRED)
        self.assertEqual(events[-1].payload["container_repair"]["action"], "stopped_and_removed")

    async def test_queue_start_updates_heartbeat_during_local_execution(self):
        self.context.llm_provider_registry.register("fake", lambda profile, env: HangingModelClient(profile, env))
        self.context.control_plane.stale_after_seconds = 1
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "heartbeat"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )

        await self.context.control_plane.queue_start(execution.id)
        await asyncio.sleep(0.6)

        current = await self.context.execution_store.get_execution(execution.id)
        self.assertIsNotNone(current)
        self.assertIsNotNone(current.last_heartbeat_at)
        self.assertGreaterEqual(current.last_heartbeat_at, current.started_at)

    async def test_global_isolation_cannot_be_downgraded_by_execution_metadata(self):
        self.context.control_plane.execution_isolation_enabled = True
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "isolation-boundary"},
            {"created_by": "tester", "execution_host": "local"},
            runtime_adapter_id="native",
        )

        self.assertEqual(execution.metadata["execution_host"], "local")
        self.assertEqual(self.context.control_plane._execution_host_for(execution), "docker")

    async def test_queue_start_prepares_isolated_runtime_when_enabled(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                _ = (mark_ready, strict)
                return RuntimeRevision(
                    id="runtime-rev-1",
                    fingerprint="fp-1",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="rev-1",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
                _ = active_revision_id
                return []

        class FakeRuntimeContainerManager:
            class Config:
                runtime_base_image = "agency-runtime-base:latest"

            config = Config()

            def __init__(self):
                self.created_spec = None

            def create_execution_container(self, spec):
                self.created_spec = spec
                return type(
                    "State",
                    (),
                    {
                        "container_id": "container-1",
                        "name": "agency-execution-container-1",
                        "image": spec.image,
                        "status": "created",
                    },
                )()

            def start_container(self, container_id: str):
                return type(
                    "State",
                    (),
                    {
                        "container_id": container_id,
                        "name": "agency-execution-container-1",
                        "image": "agency-runtime:rev-1",
                        "status": "running",
                        "started_at": utc_now(),
                        "finished_at": None,
                        "exit_code": None,
                    },
                )()

        async def fail_start_execution(execution_id: str):
            raise AssertionError("Host runtime_registry.start_execution should not be called for isolated runs")

        self.context.control_plane.execution_isolation_enabled = True
        self.context.control_plane.runtime_revision_service = FakeRuntimeRevisionService()
        container_manager = FakeRuntimeContainerManager()
        self.context.control_plane.runtime_container_manager = container_manager
        self.context.control_plane.runtime_registry.start_execution = fail_start_execution

        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "isolated"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
            goal_id="goal-isolated",
        )
        await self.context.control_plane.queue_start(execution.id)
        await asyncio.sleep(0.05)

        current = await self.context.execution_store.get_execution(execution.id)
        events = await self.context.execution_store.list_events(execution.id)

        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.runtime_revision_id, "runtime-rev-1")
        self.assertEqual(current.runtime_fingerprint, "fp-1")
        self.assertEqual(current.goal_id, "goal-isolated")
        self.assertEqual(current.metadata["worker_context"]["goal_id"], "goal-isolated")
        self.assertEqual(current.metadata["worker_context"]["execution_id"], execution.id)
        self.assertEqual(current.metadata["worker_context"]["workflow_id"], self.workflow.id)
        self.assertEqual(current.container_id, "container-1")
        self.assertEqual(current.container_status, "running")
        self.assertEqual(current.status.value, "queued")
        self.assertIsNotNone(container_manager.created_spec)
        assert container_manager.created_spec is not None
        self.assertEqual(container_manager.created_spec.goal_id, "goal-isolated")
        self.assertEqual(container_manager.created_spec.env["AGENCY_GOAL_ID"], "goal-isolated")
        event_types = [event.event_type.value for event in events]
        self.assertIn("runtime.revision.resolved", event_types)
        self.assertIn("container.created", event_types)
        self.assertIn("container.started", event_types)

    async def test_queue_start_prepares_isolated_runtime_when_execution_requests_docker_host(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                _ = (mark_ready, strict)
                return RuntimeRevision(
                    id="runtime-rev-host",
                    fingerprint="fp-host",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="host",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
                _ = active_revision_id
                return []

        class FakeRuntimeContainerManager:
            class Config:
                runtime_base_image = "agency-runtime-base:latest"

            config = Config()

            def create_execution_container(self, spec):
                return type(
                    "State",
                    (),
                    {
                        "container_id": "container-host",
                        "name": "agency-execution-container-host",
                        "image": spec.image,
                        "status": "created",
                    },
                )()

            def start_container(self, container_id: str):
                return type(
                    "State",
                    (),
                    {
                        "container_id": container_id,
                        "name": "agency-execution-container-host",
                        "image": "agency-runtime:host",
                        "status": "running",
                        "started_at": utc_now(),
                        "finished_at": None,
                        "exit_code": None,
                    },
                )()

        async def fail_start_execution(execution_id: str):
            raise AssertionError("Host runtime_registry.start_execution should not be called for docker-hosted runs")

        self.context.control_plane.execution_isolation_enabled = False
        self.context.control_plane.runtime_revision_service = FakeRuntimeRevisionService()
        self.context.control_plane.runtime_container_manager = FakeRuntimeContainerManager()
        self.context.control_plane.runtime_registry.start_execution = fail_start_execution

        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "docker-host"},
            {"created_by": "tester", "execution_host": "docker"},
            runtime_adapter_id="native",
        )
        await self.context.control_plane.queue_start(execution.id)
        await asyncio.sleep(0.05)

        current = await self.context.execution_store.get_execution(execution.id)

        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.metadata.get("execution_host"), "docker")
        self.assertEqual(current.runtime_revision_id, "runtime-rev-host")
        self.assertEqual(current.runtime_fingerprint, "fp-host")
        self.assertEqual(current.container_id, "container-host")
        self.assertEqual(current.container_status, "running")

    async def test_host_backend_mode_keeps_worker_codex_cwd_inside_container(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                _ = (mark_ready, strict)
                return RuntimeRevision(
                    id="runtime-rev-host-backend",
                    fingerprint="fp-host-backend",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="host-backend",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
                _ = active_revision_id
                return []

        class FakeRuntimeContainerManager:
            class Config:
                runtime_base_image = "agency-runtime-base:latest"

            config = Config()

            def __init__(self):
                self.created_spec = None

            def create_execution_container(self, spec):
                self.created_spec = spec
                return type(
                    "State",
                    (),
                    {
                        "container_id": "container-host-backend",
                        "name": "agency-execution-container-host-backend",
                        "image": spec.image,
                        "status": "created",
                    },
                )()

            def start_container(self, container_id: str):
                return type(
                    "State",
                    (),
                    {
                        "container_id": container_id,
                        "name": "agency-execution-container-host-backend",
                        "image": "agency-runtime:host-backend",
                        "status": "running",
                        "started_at": utc_now(),
                        "finished_at": None,
                        "exit_code": None,
                    },
                )()

        async def fail_start_execution(execution_id: str):
            raise AssertionError("Host runtime_registry.start_execution should not be called for isolated runs")

        manager = FakeRuntimeContainerManager()
        self.context.control_plane.execution_isolation_enabled = True
        self.context.control_plane.runtime_revision_service = FakeRuntimeRevisionService()
        self.context.control_plane.runtime_container_manager = manager
        self.context.control_plane.runtime_registry.start_execution = fail_start_execution

        with patch.dict(
            "os.environ",
            {
                "AGENCY_BACKEND_RUN_MODE": "host",
                "CODEX_HOME": "/Users/example/.codex",
                "CODEX_CLI_CWD": "/Users/example/workspace/agency",
                "EXECUTION_CODEX_CLI_CWD": "",
                "CODEX_CLI_TIMEOUT_SECONDS": "1800",
                "LLM_REQUEST_TIMEOUT_SECONDS": "45",
                "AGENCY_EXECUTION_TIMEOUT_SECONDS": "1800",
            },
            clear=False,
        ):
            execution = await self.context.runtime_registry.create_execution(
                self.workflow.id,
                {"topic": "host-backend"},
                {"created_by": "tester"},
                runtime_adapter_id="native",
            )
            await self.context.control_plane.queue_start(execution.id)
            await asyncio.sleep(0.05)

        self.assertIsNotNone(manager.created_spec)
        assert manager.created_spec is not None
        self.assertEqual(manager.created_spec.env["CODEX_HOME"], "/codex")
        self.assertEqual(manager.created_spec.env["CODEX_CLI_CWD"], "/app")
        self.assertEqual(manager.created_spec.env["CODEX_CLI_TIMEOUT_SECONDS"], "1800")
        self.assertEqual(manager.created_spec.env["LLM_REQUEST_TIMEOUT_SECONDS"], "45")
        self.assertEqual(manager.created_spec.env["AGENCY_EXECUTION_TIMEOUT_SECONDS"], "1800")

    async def test_isolated_worker_defaults_codex_sandbox_to_workspace_write(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                _ = (mark_ready, strict)
                return RuntimeRevision(
                    id="runtime-rev-sandbox-default",
                    fingerprint="fp-sandbox-default",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="sandbox-default",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
                _ = active_revision_id
                return []

        class FakeRuntimeContainerManager:
            class Config:
                runtime_base_image = "agency-runtime-base:latest"

            config = Config()

            def __init__(self):
                self.created_spec = None

            def create_execution_container(self, spec):
                self.created_spec = spec
                return type(
                    "State",
                    (),
                    {
                        "container_id": "container-sandbox-default",
                        "name": "agency-execution-container-sandbox-default",
                        "image": spec.image,
                        "status": "created",
                    },
                )()

            def start_container(self, container_id: str):
                return type(
                    "State",
                    (),
                    {
                        "container_id": container_id,
                        "name": "agency-execution-container-sandbox-default",
                        "image": "agency-runtime:sandbox-default",
                        "status": "running",
                        "started_at": utc_now(),
                        "finished_at": None,
                        "exit_code": None,
                    },
                )()

        async def fail_start_execution(execution_id: str):
            raise AssertionError("Host runtime_registry.start_execution should not be called for isolated runs")

        manager = FakeRuntimeContainerManager()
        self.context.control_plane.execution_isolation_enabled = True
        self.context.control_plane.runtime_revision_service = FakeRuntimeRevisionService()
        self.context.control_plane.runtime_container_manager = manager
        self.context.control_plane.runtime_registry.start_execution = fail_start_execution

        with patch.dict("os.environ", {"CODEX_CLI_SANDBOX": ""}, clear=False):
            execution = await self.context.runtime_registry.create_execution(
                self.workflow.id,
                {"topic": "sandbox-default"},
                {"created_by": "tester"},
                runtime_adapter_id="native",
            )
            await self.context.control_plane.queue_start(execution.id)
            await asyncio.sleep(0.05)

        self.assertIsNotNone(manager.created_spec)
        assert manager.created_spec is not None
        self.assertEqual(manager.created_spec.env["CODEX_CLI_SANDBOX"], "workspace-write")

    async def test_isolated_worker_timeout_uses_resolved_agent_policy(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                _ = (mark_ready, strict)
                return RuntimeRevision(
                    id="runtime-rev-agent-timeout",
                    fingerprint="fp-agent-timeout",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="agent-timeout",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
                _ = active_revision_id
                return []

        class FakeRuntimeContainerManager:
            class Config:
                runtime_base_image = "agency-runtime-base:latest"

            config = Config()

            def __init__(self):
                self.created_spec = None

            def create_execution_container(self, spec):
                self.created_spec = spec
                return type(
                    "State",
                    (),
                    {
                        "container_id": "container-agent-timeout",
                        "name": "agency-execution-container-agent-timeout",
                        "image": spec.image,
                        "status": "created",
                    },
                )()

            def start_container(self, container_id: str):
                return type(
                    "State",
                    (),
                    {
                        "container_id": container_id,
                        "name": "agency-execution-container-agent-timeout",
                        "image": "agency-runtime:agent-timeout",
                        "status": "running",
                        "started_at": utc_now(),
                        "finished_at": None,
                        "exit_code": None,
                    },
                )()

        async def fail_start_execution(execution_id: str):
            raise AssertionError("Host runtime_registry.start_execution should not be called for isolated runs")

        workflow = self.workflow.model_copy(deep=True, update={"id": "workflow-agent-timeout"})
        workflow.agent_definitions[0].metadata = {
            "timeout_policy": {
                "idle_timeout_seconds": 1200,
                "run_timeout_seconds": 14400,
                "codex_cli_timeout_seconds": 3600,
                "llm_request_timeout_seconds": 90,
            }
        }
        await self.context.runtime_registry.register_workflow(workflow)

        manager = FakeRuntimeContainerManager()
        self.context.control_plane.execution_isolation_enabled = True
        self.context.control_plane.runtime_revision_service = FakeRuntimeRevisionService()
        self.context.control_plane.runtime_container_manager = manager
        self.context.control_plane.runtime_registry.start_execution = fail_start_execution

        with patch.dict(
            "os.environ",
            {
                "AGENCY_EXECUTION_TIMEOUT_SECONDS": "",
                "CODEX_CLI_TIMEOUT_SECONDS": "",
                "LLM_REQUEST_TIMEOUT_SECONDS": "",
            },
            clear=False,
        ):
            execution = await self.context.runtime_registry.create_execution(
                workflow.id,
                {"topic": "agent-timeout"},
                {"created_by": "tester"},
                runtime_adapter_id="native",
            )
            await self.context.control_plane.queue_start(execution.id)
            await asyncio.sleep(0.05)

        self.assertIsNotNone(manager.created_spec)
        assert manager.created_spec is not None
        self.assertEqual(manager.created_spec.env["AGENCY_EXECUTION_TIMEOUT_SECONDS"], "14400")
        self.assertEqual(manager.created_spec.env["CODEX_CLI_TIMEOUT_SECONDS"], "3600")
        self.assertEqual(manager.created_spec.env["LLM_REQUEST_TIMEOUT_SECONDS"], "90")
        current = await self.context.execution_store.get_execution(execution.id)
        self.assertIsNotNone(current)
        assert current is not None
        runtime_policy = current.metadata["runtime_policy"]
        self.assertEqual(runtime_policy["worker_hard_timeout_seconds"], 14400)
        self.assertEqual(
            runtime_policy["source_map"]["worker_hard_timeout_seconds"],
            "agent:agent-approval.metadata.runtime_policy",
        )

    async def test_isolated_always_on_worker_omits_default_hard_timeout(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                _ = (mark_ready, strict)
                return RuntimeRevision(
                    id="runtime-rev-always-on",
                    fingerprint="fp-always-on",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="always-on",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
                _ = active_revision_id
                return []

        class FakeRuntimeContainerManager:
            class Config:
                runtime_base_image = "agency-runtime-base:latest"

            config = Config()

            def __init__(self):
                self.created_spec = None

            def create_execution_container(self, spec):
                self.created_spec = spec
                return type(
                    "State",
                    (),
                    {
                        "container_id": "container-always-on",
                        "name": "agency-execution-container-always-on",
                        "image": spec.image,
                        "status": "created",
                    },
                )()

            def start_container(self, container_id: str):
                return type(
                    "State",
                    (),
                    {
                        "container_id": container_id,
                        "name": "agency-execution-container-always-on",
                        "image": "agency-runtime:always-on",
                        "status": "running",
                        "started_at": utc_now(),
                        "finished_at": None,
                        "exit_code": None,
                    },
                )()

        async def fail_start_execution(execution_id: str):
            raise AssertionError("Host runtime_registry.start_execution should not be called for isolated runs")

        workflow = self.workflow.model_copy(
            deep=True,
            update={
                "id": "workflow-always-on-timeout",
                "metadata": {
                    "execution_lifecycle": {
                        "run_mode": "always_on",
                    }
                },
            },
        )
        await self.context.runtime_registry.register_workflow(workflow)

        manager = FakeRuntimeContainerManager()
        self.context.control_plane.execution_isolation_enabled = True
        self.context.control_plane.runtime_revision_service = FakeRuntimeRevisionService()
        self.context.control_plane.runtime_container_manager = manager
        self.context.control_plane.runtime_registry.start_execution = fail_start_execution

        with patch.dict("os.environ", {"AGENCY_EXECUTION_TIMEOUT_SECONDS": ""}, clear=False):
            execution = await self.context.runtime_registry.create_execution(
                workflow.id,
                {"topic": "always-on"},
                {"created_by": "tester"},
                runtime_adapter_id="native",
            )
            await self.context.control_plane.queue_start(execution.id)
            await asyncio.sleep(0.05)

        current = await self.context.execution_store.get_execution(execution.id)
        self.assertIsNotNone(manager.created_spec)
        assert manager.created_spec is not None
        self.assertNotIn("AGENCY_EXECUTION_TIMEOUT_SECONDS", manager.created_spec.env)
        self.assertIsNotNone(current)
        assert current is not None
        runtime_policy = current.metadata["runtime_policy"]
        self.assertIsNone(runtime_policy["worker_hard_timeout_seconds"])
        self.assertEqual(
            runtime_policy["source_map"]["worker_hard_timeout_seconds"],
            "execution_lifecycle.always_on",
        )

    async def test_isolated_worker_receives_onecli_proxy_environment_when_enforced(self):
        class FakeRuntimeRevisionService:
            def __init__(self):
                self.metadata = None

            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                self.metadata = metadata
                return RuntimeRevision(
                    id="runtime-rev-onecli-worker",
                    fingerprint="fp-onecli-worker",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="onecli-worker",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
                _ = active_revision_id
                return []

        class FakeRuntimeContainerManager:
            class Config:
                runtime_base_image = "agency-runtime-base:latest"

            config = Config()

            def __init__(self):
                self.created_spec = None

            def create_execution_container(self, spec):
                self.created_spec = spec
                return type(
                    "State",
                    (),
                    {
                        "container_id": "container-onecli-worker",
                        "name": "agency-execution-container-onecli-worker",
                        "image": spec.image,
                        "status": "created",
                    },
                )()

            def start_container(self, container_id: str):
                return type(
                    "State",
                    (),
                    {
                        "container_id": container_id,
                        "name": "agency-execution-container-onecli-worker",
                        "image": "agency-runtime:onecli-worker",
                        "status": "running",
                        "started_at": utc_now(),
                        "finished_at": None,
                        "exit_code": None,
                    },
                )()

        async def fail_start_execution(execution_id: str):
            raise AssertionError("Host runtime_registry.start_execution should not be called for isolated runs")

        manager = FakeRuntimeContainerManager()
        revision_service = FakeRuntimeRevisionService()
        self.context.control_plane.execution_isolation_enabled = True
        self.context.control_plane.runtime_revision_service = revision_service
        self.context.control_plane.runtime_container_manager = manager
        self.context.control_plane.runtime_registry.start_execution = fail_start_execution

        with patch.dict(
                "os.environ",
                {
                    "EXECUTION_ISOLATION_ENABLED": "true",
                    "ONECLI_ENABLED": "true",
                    "ONECLI_FORCE_FOR_ISOLATED_WORKERS": "true",
                    "ONECLI_GATEWAY_URL": "http://onecli:10255",
                    "ONECLI_GATEWAY_CA_BUNDLE_PATH": "/tmp/onecli-ca.pem",
                    "ONECLI_GATEWAY_CA_BUNDLE_CONTAINER_PATH": "/etc/agency/onecli/ca.pem",
                    "ONECLI_AGENT_TOKEN_SECRET_REF": "env://ONECLI_AGENT_TOKEN",
                    "ONECLI_AGENT_TOKEN": "should-not-enter-worker",
                    "ONECLI_WORKER_EGRESS_MODE": "docker_internal_network",
                    "ONECLI_WORKER_EGRESS_NETWORK": "agency_onecli_worker_egress",
                    "ONECLI_NODE_PROXY_BOOTSTRAP_PATH": "/app/app/runtime/node_onecli_proxy.cjs",
                    "ONECLI_WORKER_NO_PROXY": "postgres,redis,agency-backend,onecli",
                    "OPENAI_API_KEY": "should-not-enter-worker",
                    "ANTHROPIC_API_KEY": "should-not-enter-worker",
                    "GOOGLE_API_KEY": "should-not-enter-worker",
                    "AZURE_OPENAI_API_KEY": "should-not-enter-worker",
                },
                clear=False,
        ):
            reset_settings_cache()
            execution = await self.context.runtime_registry.create_execution(
                self.workflow.id,
                {"topic": "onecli-worker"},
                {"created_by": "tester"},
                runtime_adapter_id="native",
            )
            await self.context.control_plane.queue_start(execution.id)
            await asyncio.sleep(0.05)
        reset_settings_cache()

        self.assertIsNotNone(manager.created_spec)
        assert manager.created_spec is not None
        env = manager.created_spec.env
        self.assertEqual(env["HTTP_PROXY"], "http://onecli:10255")
        self.assertEqual(env["HTTPS_PROXY"], "http://onecli:10255")
        self.assertEqual(env["NO_PROXY"], "postgres,redis,agency-backend,onecli")
        self.assertEqual(env["REQUESTS_CA_BUNDLE"], "/etc/agency/onecli/ca.pem")
        self.assertEqual(env["NODE_EXTRA_CA_CERTS"], "/etc/agency/onecli/ca.pem")
        self.assertEqual(env["ONECLI_NODE_PROXY_BOOTSTRAP_PATH"], "/app/app/runtime/node_onecli_proxy.cjs")
        self.assertIn("--require /app/app/runtime/node_onecli_proxy.cjs", env["NODE_OPTIONS"])
        self.assertEqual(env["ONECLI_AGENT_TOKEN_SECRET_REF_CONFIGURED"], "true")
        self.assertNotIn("ONECLI_AGENT_TOKEN_SECRET_REF", env)
        self.assertNotIn("ONECLI_AGENT_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("GOOGLE_API_KEY", env)
        self.assertNotIn("AZURE_OPENAI_API_KEY", env)
        self.assertNotIn("should-not-enter-worker", str(env))
        self.assertNotIn("should-not-enter-worker", str(manager.created_spec.command))
        self.assertNotIn("should-not-enter-worker", str(manager.created_spec.labels))
        self.assertEqual(manager.created_spec.labels["agency.onecli.enabled"], "true")
        self.assertEqual(manager.created_spec.labels["agency.onecli.isolated_workers"], "true")
        self.assertEqual(manager.created_spec.labels["agency.onecli.egress_mode"], "docker_internal_network")
        self.assertEqual(manager.created_spec.network_name, "agency_onecli_worker_egress")
        self.assertIsNotNone(revision_service.metadata)
        assert revision_service.metadata is not None
        self.assertEqual(revision_service.metadata["onecli"]["gateway_url"], "http://onecli:10255")
        self.assertTrue(revision_service.metadata["onecli"]["agent_token_secret_ref_configured"])
        self.assertEqual(revision_service.metadata["onecli"]["worker_egress_mode"], "docker_internal_network")
        self.assertEqual(revision_service.metadata["onecli"]["worker_egress_network"], "agency_onecli_worker_egress")
        self.assertTrue(revision_service.metadata["onecli"]["node_proxy_bootstrap_configured"])
        self.assertNotIn("env://ONECLI_AGENT_TOKEN", str(revision_service.metadata))
        self.assertNotIn("should-not-enter-worker", str(revision_service.metadata))

        current = await self.context.execution_store.get_execution(execution.id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertNotIn("should-not-enter-worker", str(current.metadata))
        diagnostics = current.metadata["onecli_worker_enforcement"]
        self.assertEqual(diagnostics["enforcement_mode"], "docker_internal_network")
        self.assertTrue(diagnostics["proxy_env_required"])
        self.assertEqual(diagnostics["missing_proxy_env"], [])
        self.assertEqual(diagnostics["missing_ca_env"], [])
        self.assertTrue(diagnostics["direct_external_credentials_blocked"])
        self.assertEqual(diagnostics["forbidden_env_present"], [])
        self.assertEqual(diagnostics["container_level_egress_controls"], "docker_internal_network")
        self.assertEqual(diagnostics["worker_network"], "agency_onecli_worker_egress")
        self.assertTrue(diagnostics["node_proxy_bootstrap_configured"])
        self.assertEqual(diagnostics["node_proxy_bootstrap_path"], "/app/app/runtime/node_onecli_proxy.cjs")
        self.assertNotIn("env://ONECLI_AGENT_TOKEN", str(diagnostics))

        events = await self.context.execution_store.list_events(execution.id)
        onecli_events = [
            event for event in events
            if event.event_type == ExecutionEventType.ONECLI_WORKER_ENFORCEMENT_RECORDED
        ]
        self.assertTrue(onecli_events)
        self.assertEqual(onecli_events[-1].payload["enforcement_mode"], "docker_internal_network")
        self.assertTrue(onecli_events[-1].payload["agent_token_secret_ref_configured"])
        self.assertNotIn("env://ONECLI_AGENT_TOKEN", str(onecli_events[-1].payload))
        self.assertNotIn("should-not-enter-worker", str(onecli_events[-1].payload))

    async def test_queue_start_resolves_runtime_revision_in_shadow_mode_without_container_startup(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                _ = (mark_ready, strict)
                return RuntimeRevision(
                    id="runtime-rev-shadow",
                    fingerprint="fp-shadow",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="shadow",
                    metadata_json=metadata or {},
                )

        async def fake_start_execution(execution_id: str):
            execution = await self.context.execution_store.get_execution(execution_id)
            return execution

        self.context.control_plane.execution_isolation_enabled = False
        self.context.control_plane.runtime_revision_shadow_mode = True
        self.context.control_plane.runtime_revision_service = FakeRuntimeRevisionService()
        self.context.control_plane.runtime_registry.start_execution = fake_start_execution

        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "shadow"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        await self.context.control_plane.queue_start(execution.id)
        await asyncio.sleep(0.05)

        current = await self.context.execution_store.get_execution(execution.id)
        events = await self.context.execution_store.list_events(execution.id)

        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.runtime_revision_id, "runtime-rev-shadow")
        self.assertEqual(current.runtime_fingerprint, "fp-shadow")
        self.assertIsNone(current.container_id)
        self.assertEqual(current.status.value, "running")
        event_types = [event.event_type.value for event in events]
        self.assertIn("runtime.revision.resolved", event_types)
        self.assertNotIn("container.created", event_types)

    async def test_queue_start_links_outdated_execution_without_cancelling_by_default(self):
        invalidated_revision = RuntimeRevision(
            id="runtime-rev-old",
            fingerprint="fp-old",
            build_status=RuntimeRevisionStatus.INVALIDATED,
            invalidation_reason="superseded_by:runtime-rev-2",
        )

        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                _ = (mark_ready, strict)
                return RuntimeRevision(
                    id="runtime-rev-2",
                    fingerprint="fp-2",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="rev-2",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
                return [invalidated_revision]

        class FakeRuntimeContainerManager:
            class Config:
                runtime_base_image = "agency-runtime-base:latest"

            config = Config()

            def create_execution_container(self, spec):
                return type(
                    "State",
                    (),
                    {
                        "container_id": "container-new",
                        "name": "agency-execution-container-new",
                        "image": spec.image,
                        "status": "created",
                    },
                )()

            def start_container(self, container_id: str):
                return type(
                    "State",
                    (),
                    {
                        "container_id": container_id,
                        "name": "agency-execution-container-new",
                        "image": "agency-runtime:rev-2",
                        "status": "running",
                        "started_at": utc_now(),
                        "finished_at": None,
                        "exit_code": None,
                    },
                )()

        async def fail_cancel_execution(execution_id: str):
            raise AssertionError("cancel_execution should not be called when cancellation policy is disabled")

        async def fail_start_execution(execution_id: str):
            raise AssertionError("Host runtime_registry.start_execution should not be called for isolated runs")

        old_execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "old"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        old_execution.status = old_execution.status.__class__.RUNNING
        old_execution.runtime_revision_id = "runtime-rev-old"
        old_execution.runtime_fingerprint = "fp-old"
        old_execution.container_id = "container-old"
        old_execution.container_name = "agency-execution-container-old"
        old_execution.container_image = "agency-runtime:rev-old"
        old_execution.container_status = "running"
        await self.context.execution_store.update_execution(old_execution)

        self.context.control_plane.execution_isolation_enabled = True
        self.context.control_plane.cancel_outdated_executions = False
        self.context.control_plane.runtime_revision_service = FakeRuntimeRevisionService()
        self.context.control_plane.runtime_container_manager = FakeRuntimeContainerManager()
        self.context.control_plane.runtime_registry.start_execution = fail_start_execution
        self.context.control_plane.runtime_registry.cancel_execution = fail_cancel_execution

        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "new"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        await self.context.control_plane.queue_start(execution.id)
        await asyncio.sleep(0.05)

        current = await self.context.execution_store.get_execution(execution.id)
        existing = await self.context.execution_store.get_execution(old_execution.id)
        events = await self.context.execution_store.list_events(execution.id)

        assert current is not None
        assert existing is not None
        self.assertEqual(current.runtime_revision_id, "runtime-rev-2")
        self.assertEqual(current.replacement_of_execution_id, old_execution.id)
        self.assertEqual(current.restart_reason, "runtime_revision_superseded")
        self.assertEqual(current.status.value, "queued")
        self.assertEqual(existing.status.value, "running")
        self.assertEqual(existing.container_status, "running")
        event_types = [event.event_type.value for event in events]
        self.assertIn("runtime.revision.invalidated", event_types)

    async def test_queue_start_replaces_outdated_execution_when_cancellation_enabled(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                _ = (mark_ready, strict)
                return RuntimeRevision(
                    id="runtime-rev-2",
                    fingerprint="fp-2",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="rev-2",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
                _ = active_revision_id
                return []

        class FakeRuntimeContainerManager:
            class Config:
                runtime_base_image = "agency-runtime-base:latest"

            config = Config()

            def __init__(self):
                self.stopped = []
                self.removed = []

            def create_execution_container(self, spec):
                return type(
                    "State",
                    (),
                    {
                        "container_id": "container-new",
                        "name": "agency-execution-container-new",
                        "image": spec.image,
                        "status": "created",
                    },
                )()

            def start_container(self, container_id: str):
                return type(
                    "State",
                    (),
                    {
                        "container_id": container_id,
                        "name": "agency-execution-container-new",
                        "image": "agency-runtime:rev-2",
                        "status": "running",
                        "started_at": utc_now(),
                        "finished_at": None,
                        "exit_code": None,
                    },
                )()

            def inspect_container(self, container_id: str):
                return RuntimeContainerState(
                    container_id=container_id,
                    name="agency-execution-container-old",
                    image="agency-runtime:rev-old",
                    status="running",
                    labels={"agency.execution_id": "old"},
                    started_at=utc_now(),
                )

            def stop_container(self, container_id: str):
                self.stopped.append(container_id)
                return RuntimeContainerState(
                    container_id=container_id,
                    name="agency-execution-container-old",
                    image="agency-runtime:rev-old",
                    status="exited",
                    labels={"agency.execution_id": "old"},
                    started_at=utc_now(),
                    finished_at=utc_now(),
                    exit_code=0,
                )

            def remove_container(self, container_id: str, *, force: bool = False):
                self.removed.append((container_id, force))

        async def fake_cancel_execution(execution_id: str):
            execution = await self.context.execution_store.get_execution(execution_id)
            assert execution is not None
            execution.status = execution.status.__class__.CANCELLED
            execution.completed_at = utc_now()
            await self.context.execution_store.update_execution(execution)
            return execution

        async def fail_start_execution(execution_id: str):
            raise AssertionError("Host runtime_registry.start_execution should not be called for isolated runs")

        old_execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "old"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        old_execution.status = old_execution.status.__class__.RUNNING
        old_execution.runtime_revision_id = "runtime-rev-old"
        old_execution.runtime_fingerprint = "fp-old"
        old_execution.container_id = "container-old"
        old_execution.container_name = "agency-execution-container-old"
        old_execution.container_image = "agency-runtime:rev-old"
        old_execution.container_status = "running"
        await self.context.execution_store.update_execution(old_execution)

        manager = FakeRuntimeContainerManager()
        self.context.control_plane.execution_isolation_enabled = True
        self.context.control_plane.cancel_outdated_executions = True
        self.context.control_plane.runtime_revision_service = FakeRuntimeRevisionService()
        self.context.control_plane.runtime_container_manager = manager
        self.context.control_plane.runtime_registry.start_execution = fail_start_execution
        self.context.control_plane.runtime_registry.cancel_execution = fake_cancel_execution

        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "new"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        await self.context.control_plane.queue_start(execution.id)
        await asyncio.sleep(0.05)

        current = await self.context.execution_store.get_execution(execution.id)
        existing = await self.context.execution_store.get_execution(old_execution.id)
        old_events = await self.context.execution_store.list_events(old_execution.id)

        assert current is not None
        assert existing is not None
        self.assertEqual(current.replacement_of_execution_id, old_execution.id)
        self.assertEqual(current.status.value, "queued")
        self.assertEqual(existing.status.value, "cancelled")
        self.assertEqual(existing.container_status, "removed")
        self.assertEqual(existing.restart_reason, "runtime_revision_superseded")
        self.assertEqual(manager.stopped, ["container-old"])
        self.assertEqual(manager.removed, [("container-old", True)])
        self.assertIn("container.replaced", [event.event_type.value for event in old_events])

    async def test_workflow_revision_replacement_cancels_active_container_and_queues_replacement(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                _ = (mark_ready, strict)
                return RuntimeRevision(
                    id="runtime-rev-workflow",
                    fingerprint="fp-workflow",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="workflow",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
                _ = active_revision_id
                return []

        class FakeRuntimeContainerManager:
            class Config:
                runtime_base_image = "agency-runtime-base:latest"

            config = Config()

            def __init__(self):
                self.stopped = []
                self.removed = []

            def create_execution_container(self, spec):
                return RuntimeContainerState(
                    container_id=f"container-{spec.execution_id}",
                    name=f"agency-execution-{spec.execution_id}",
                    image=spec.image,
                    status="created",
                    labels={"agency.execution_id": spec.execution_id},
                )

            def start_container(self, container_id: str):
                return RuntimeContainerState(
                    container_id=container_id,
                    name="agency-execution-replacement",
                    image="agency-runtime:workflow",
                    status="running",
                    labels={"agency.execution_id": "replacement"},
                    started_at=utc_now(),
                )

            def inspect_container(self, container_id: str):
                return RuntimeContainerState(
                    container_id=container_id,
                    name="agency-execution-old",
                    image="agency-runtime:old",
                    status="running",
                    labels={"agency.execution_id": "old"},
                    started_at=utc_now(),
                )

            def stop_container(self, container_id: str):
                self.stopped.append(container_id)
                return RuntimeContainerState(
                    container_id=container_id,
                    name="agency-execution-old",
                    image="agency-runtime:old",
                    status="exited",
                    labels={"agency.execution_id": "old"},
                    started_at=utc_now(),
                    finished_at=utc_now(),
                    exit_code=0,
                )

            def remove_container(self, container_id: str, *, force: bool = False):
                self.removed.append((container_id, force))

        async def fail_start_execution(execution_id: str):
            raise AssertionError("Host runtime_registry.start_execution should not be called for isolated runs")

        old_execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "old"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        old_execution.status = old_execution.status.__class__.RUNNING
        old_execution.runtime_revision_id = "runtime-rev-old"
        old_execution.container_id = "container-old"
        old_execution.container_name = "agency-execution-container-old"
        old_execution.container_image = "agency-runtime:old"
        old_execution.container_status = "running"
        old_execution.output_payload = {
            "node_outputs": {"node-old": {"partial": "value"}},
            "checkpoint": {"current_node_id": "node-next"},
        }
        await self.context.execution_store.update_execution(old_execution)
        await self.context.execution_store.save_artifact(
            ExecutionArtifact(
                id="artifact-workflow-replacement",
                execution_id=old_execution.id,
                artifact_type="json",
                name="partial-result.json",
                content_json={"partial": True},
                mime_type="application/json",
            )
        )

        manager = FakeRuntimeContainerManager()
        self.context.control_plane.execution_isolation_enabled = True
        self.context.control_plane.runtime_revision_service = FakeRuntimeRevisionService()
        self.context.control_plane.runtime_container_manager = manager
        self.context.control_plane.runtime_registry.start_execution = fail_start_execution

        replacements = await self.context.control_plane.replace_active_executions_for_workflow_revision(
            workflow_id=self.workflow.id,
            previous_revision=1,
            replacement_revision=2,
            source="workflow_publish",
        )
        await asyncio.sleep(0.05)

        self.assertEqual(len(replacements), 1)
        replacement = await self.context.execution_store.get_execution(replacements[0])
        existing = await self.context.execution_store.get_execution(old_execution.id)
        old_events = await self.context.execution_store.list_events(old_execution.id)

        assert replacement is not None
        assert existing is not None
        self.assertEqual(replacement.replacement_of_execution_id, old_execution.id)
        self.assertEqual(replacement.restart_reason, "workflow_revision_superseded")
        self.assertEqual(replacement.trigger_payload["replacement_workflow_revision"], 2)
        self.assertEqual(existing.status.value, "cancelled")
        self.assertEqual(existing.restart_reason, "workflow_revision_superseded")
        self.assertEqual(existing.container_status, "removed")
        self.assertEqual(existing.output_payload["node_outputs"]["node-old"], {"partial": "value"})
        existing_artifacts = await self.context.execution_store.list_artifacts(existing.id)
        self.assertEqual([artifact.id for artifact in existing_artifacts], ["artifact-workflow-replacement"])
        preservation = existing.metadata["partial_result_preservation"]
        self.assertEqual(preservation["reason"], "workflow_revision_superseded")
        self.assertEqual(preservation["artifact_count"], 1)
        self.assertEqual(preservation["artifacts"][0]["artifact_id"], "artifact-workflow-replacement")
        self.assertEqual(preservation["node_output_ids"], ["node-old"])
        self.assertEqual(replacement.metadata["source_partial_result_preservation"], preservation)
        self.assertEqual(manager.stopped, ["container-old"])
        self.assertEqual(manager.removed, [("container-old", True)])
        container_replaced = [
            event for event in old_events if event.event_type.value == "container.replaced"
        ][0]
        self.assertEqual(container_replaced.payload["partial_result_preservation"], preservation)


class ExecutionControlPlaneStreamingTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        app = FastAPI()
        app.include_router(create_executions_router(self.context))
        self.client = TestClient(app)
        self.client.headers.update(
            {
                "x-agency-user-id": "user-execution-stream",
                "x-agency-user-email": "execution-stream@example.com",
            }
        )
        asyncio.run(
            self.context.user_repo.create(
                UserDefinition(
                    id="user-execution-stream",
                    email="execution-stream@example.com",
                    display_name="Execution Stream User",
                    roles=["admin"],
                )
            )
        )

    def test_event_replay_endpoint(self):
        self.context.execution_store._executions.setdefault(  # noqa: SLF001
            "execution-stream",
            self.context.execution_store._executions.get("execution-stream")
            or Execution(
                id="execution-stream",
                workflow_id="workflow-x",
                runtime_adapter_id="native",
                status="completed",
                input_payload={},
            ),
        )
        self.context.execution_store._events["execution-stream"] = [  # noqa: SLF001
            ExecutionEvent(execution_id="execution-stream", event_type=ExecutionEventType.EXECUTION_CREATED,
                           sequence=1),
            ExecutionEvent(execution_id="execution-stream", event_type=ExecutionEventType.EXECUTION_COMPLETED,
                           sequence=2),
        ]

        response = self.client.get("/executions/execution-stream/events?after_sequence=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 1)

    def test_event_replay_endpoint_filters_event_types(self):
        self.context.execution_store._executions["execution-stream"] = Execution(  # noqa: SLF001
            id="execution-stream",
            workflow_id="workflow-x",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
        )
        self.context.execution_store._events["execution-stream"] = [  # noqa: SLF001
            ExecutionEvent(
                execution_id="execution-stream",
                event_type=ExecutionEventType.EXECUTION_CREATED,
                sequence=1,
            ),
            ExecutionEvent(
                execution_id="execution-stream",
                event_type=ExecutionEventType.TOKEN_BUDGET_WARNING,
                sequence=2,
            ),
            ExecutionEvent(
                execution_id="execution-stream",
                event_type=ExecutionEventType.CONTEXT_COMPACTION_COMPLETED,
                sequence=3,
            ),
        ]

        response = self.client.get(
            "/executions/execution-stream/events"
            "?event_type=token.budget.warning"
            "&event_type=context.compaction.completed"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            [item["event_type"] for item in body["items"]],
            ["token.budget.warning", "context.compaction.completed"],
        )
        self.assertEqual(
            body["filters"]["event_types"],
            ["context.compaction.completed", "token.budget.warning"],
        )

    def test_event_replay_endpoint_filters_comma_separated_event_types_after_sequence(self):
        self.context.execution_store._executions["execution-stream"] = Execution(  # noqa: SLF001
            id="execution-stream",
            workflow_id="workflow-x",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
        )
        self.context.execution_store._events["execution-stream"] = [  # noqa: SLF001
            ExecutionEvent(
                execution_id="execution-stream",
                event_type=ExecutionEventType.TOKEN_BUDGET_WARNING,
                sequence=1,
            ),
            ExecutionEvent(
                execution_id="execution-stream",
                event_type=ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
                sequence=2,
            ),
            ExecutionEvent(
                execution_id="execution-stream",
                event_type=ExecutionEventType.CONTEXT_HEALTH_RECORDED,
                sequence=3,
            ),
        ]

        response = self.client.get(
            "/executions/execution-stream/events"
            "?after_sequence=1"
            "&event_types=token.budget.warning,context.health.recorded"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["event_type"] for item in response.json()["items"]],
            ["context.health.recorded"],
        )

    def test_event_replay_endpoint_rejects_unknown_event_type_filter(self):
        self.context.execution_store._executions["execution-stream"] = Execution(  # noqa: SLF001
            id="execution-stream",
            workflow_id="workflow-x",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
        )

        response = self.client.get("/executions/execution-stream/events?event_type=not.real")

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported execution event type filter", response.json()["detail"])

    def test_usage_endpoint_returns_runtime_governance_snapshot(self):
        self.context.execution_store._executions["execution-usage"] = Execution(  # noqa: SLF001
            id="execution-usage",
            workflow_id="workflow-x",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
            metadata={
                "runtime_governance": {
                    "token_usage": {
                        "total": {
                            "prompt_tokens": 20,
                            "completion_tokens": 5,
                            "total_tokens": 25,
                            "estimated_cost": 0.0001,
                            "currency": "USD",
                        },
                        "by_agent": {"agent-1": {"total_tokens": 25}},
                        "by_task": {"task-1": {"total_tokens": 25}},
                        "by_model": {"fake:fake-model": {"total_tokens": 25}},
                        "processed_event_ids": ["internal-event-id"],
                        "updated_at": "2026-05-25T06:00:00Z",
                    },
                    "budget_warnings_emitted": {
                        "run:25:warning": {
                            "scope": "run",
                            "used_tokens": 25,
                            "budget_tokens": 30,
                            "status": "warning",
                            "emitted_at": "2026-05-25T06:00:01Z",
                        }
                    },
                }
            },
        )

        response = self.client.get("/executions/execution-usage/usage")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["execution_id"], "execution-usage")
        self.assertEqual(payload["token_usage"]["total"]["total_tokens"], 25)
        self.assertNotIn("processed_event_ids", payload["token_usage"])
        self.assertEqual(payload["budget_warnings"][0]["status"], "warning")

    def test_usage_endpoint_falls_back_to_token_usage_events(self):
        self.context.execution_store._executions["execution-usage-events"] = Execution(  # noqa: SLF001
            id="execution-usage-events",
            workflow_id="workflow-x",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
            metadata={},
        )
        asyncio.run(
            self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id="execution-usage-events",
                    workflow_id="workflow-x",
                    agent_id="agent-1",
                    task_id="task-1",
                    model_request_id="request-1",
                    event_type=ExecutionEventType.TOKEN_USAGE_RECORDED,
                    payload={
                        "usage": {
                            "provider": "fake",
                            "model": "fake-model",
                            "prompt_tokens": 20,
                            "completion_tokens": 5,
                            "total_tokens": 25,
                            "estimated_cost": 0.0001,
                            "currency": "USD",
                        }
                    },
                )
            )
        )

        response = self.client.get("/executions/execution-usage-events/usage")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "execution.events")
        self.assertEqual(payload["token_usage"]["total"]["total_tokens"], 25)
        self.assertEqual(payload["token_usage"]["by_agent"]["agent-1"]["prompt_tokens"], 20)
        self.assertEqual(payload["token_usage"]["by_task"]["task-1"]["completion_tokens"], 5)
        self.assertEqual(payload["token_usage"]["by_model"]["fake:fake-model"]["total_tokens"], 25)
        self.assertEqual(payload["token_usage"]["last_model_request_id"], "request-1")

    def test_context_usage_endpoint_returns_health_and_compaction(self):
        self.context.execution_store._executions["execution-context"] = Execution(  # noqa: SLF001
            id="execution-context",
            workflow_id="workflow-x",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
            metadata={
                "runtime_governance": {
                    "context_health": {
                        "last": {
                            "status": "critical",
                            "estimated_total_context_tokens": 9000,
                            "context_window": 10000,
                            "usage_ratio": 0.9,
                            "updated_at": "2026-05-25T06:00:00Z",
                        }
                    },
                    "context_compaction": {
                        "last": {
                            "compacted": True,
                            "reason": "context_health_threshold",
                            "memory_id": "memory-compact",
                            "estimated_tokens_saved": 1200,
                            "metadata": {
                                "protected_context_retained": True,
                                "protected_message_count": 3,
                                "protected_message_roles": ["system", "user", "tool"],
                                "protected_message_reasons": {
                                    "0": "system_message",
                                    "1": "user_message",
                                    "3": "pending_human_decision",
                                },
                            },
                            "updated_at": "2026-05-25T06:00:01Z",
                        },
                        "records": [
                            {
                                "compacted": True,
                                "reason": "context_health_threshold",
                                "memory_id": "memory-compact",
                                "estimated_tokens_saved": 1200,
                                "metadata": {
                                    "protected_context_retained": True,
                                    "protected_message_count": 3,
                                    "protected_message_roles": ["system", "user", "tool"],
                                    "protected_message_reasons": {
                                        "0": "system_message",
                                        "1": "user_message",
                                        "3": "pending_human_decision",
                                    },
                                },
                                "updated_at": "2026-05-25T06:00:01Z",
                            }
                        ],
                        "count": 1,
                        "compacted_count": 1,
                    },
                }
            },
        )

        response = self.client.get("/executions/execution-context/context-usage")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["latest_context_health"]["status"], "critical")
        self.assertTrue(payload["latest_compaction"]["compacted"])
        self.assertEqual(payload["compaction_records"][0]["memory_id"], "memory-compact")
        self.assertTrue(payload["protected_context"]["retained"])
        self.assertEqual(payload["protected_context"]["protected_message_count"], 3)
        self.assertEqual(payload["protected_context"]["protected_message_roles"], ["system", "user", "tool"])
        self.assertEqual(payload["protected_context"]["protected_message_reasons"]["3"], "pending_human_decision")

    def test_context_usage_endpoint_falls_back_to_governance_events(self):
        self.context.execution_store._executions["execution-context-events"] = Execution(  # noqa: SLF001
            id="execution-context-events",
            workflow_id="workflow-x",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
            metadata={},
        )
        asyncio.run(
            self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id="execution-context-events",
                    workflow_id="workflow-x",
                    agent_id="agent-1",
                    task_id="task-1",
                    event_type=ExecutionEventType.CONTEXT_HEALTH_RECORDED,
                    payload={
                        "status": "warning",
                        "estimated_total_context_tokens": 7500,
                        "context_window": 10000,
                        "usage_ratio": 0.75,
                    },
                )
            )
        )
        asyncio.run(
            self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id="execution-context-events",
                    workflow_id="workflow-x",
                    agent_id="agent-1",
                    task_id="task-1",
                    event_type=ExecutionEventType.CONTEXT_COMPACTION_COMPLETED,
                    payload={
                        "record": {
                            "compacted": True,
                            "reason": "context_health_threshold",
                            "estimated_tokens_saved": 1200,
                        }
                    },
                )
            )
        )

        response = self.client.get("/executions/execution-context-events/context-usage")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], "execution.events")
        self.assertEqual(payload["latest_context_health"]["status"], "warning")
        self.assertEqual(payload["latest_context_health"]["event_id"], payload["context_health"]["last"]["event_id"])
        self.assertTrue(payload["latest_compaction"]["compacted"])
        self.assertEqual(payload["context_compaction"]["count"], 1)
        self.assertEqual(payload["context_compaction"]["estimated_tokens_saved"], 1200)
        self.assertEqual(payload["compaction_records"][0]["reason"], "context_health_threshold")

    def test_approval_requests_endpoint(self):
        self.context.execution_store._executions["execution-approval"] = Execution(  # noqa: SLF001
            id="execution-approval",
            workflow_id="workflow-x",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
        )
        request_id = asyncio.run(
            self.context.execution_store.create_approval_request(
                execution_id="execution-approval",
                event_id="event-approval-requested",
                tool_id="tool-approval",
                status="pending",
                payload={
                    "arguments": {"text": "approve me"},
                    "approval_metadata": {"risk_labels": ["low_risk"]},
                },
            )
        )
        asyncio.run(
            self.context.execution_store.update_approval_request(
                request_id,
                status="approved",
                response_payload={
                    "granted": True,
                    "reason": "Delegated approval",
                    "metadata": {"mode": "delegated", "delegate": "main_agent"},
                },
                responded_by="main_agent",
            )
        )

        response = self.client.get("/executions/execution-approval/approvals")

        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], request_id)
        self.assertEqual(items[0]["event_id"], "event-approval-requested")
        self.assertEqual(items[0]["tool_id"], "tool-approval")
        self.assertEqual(items[0]["status"], "approved")
        self.assertEqual(items[0]["responded_by"], "main_agent")
        self.assertEqual(items[0]["request_payload"]["approval_metadata"]["risk_labels"], ["low_risk"])
        self.assertEqual(items[0]["response_payload"]["metadata"]["delegate"], "main_agent")

    def test_streaming_endpoint(self):
        self.context.execution_store._executions["execution-stream"] = Execution(  # noqa: SLF001
            id="execution-stream",
            workflow_id="workflow-x",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
        )
        self.context.execution_store._events["execution-stream"] = [  # noqa: SLF001
            ExecutionEvent(execution_id="execution-stream", event_type=ExecutionEventType.EXECUTION_CREATED,
                           sequence=1),
        ]

        with self.client.stream("GET", "/executions/execution-stream/stream") as response:
            body = b"".join(response.iter_raw())
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"execution.created", body)


if __name__ == "__main__":
    unittest.main()
