from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.api.routes.executions import create_executions_router
from app.core.time import utc_now
from app.domain import AgentDefinition, Execution, ExecutionEvent, ExecutionEventType, ModelProfileDefinition, \
    RuntimeRevision, RuntimeRevisionStatus, TaskDefinition, ToolDefinition, UserDefinition, WorkflowDefinition, \
    WorkflowNodeDefinition
from app.domain.models import FrameworkHints, MCPExposureSettings, MemorySettings, SecuritySettings, \
    ToolImplementationReference
from app.llm.base import ModelResponse, ModelToolCall
from app.runtime.containers import RuntimeContainerState


class ApprovalAwareModelClient:
    provider_key = "fake"

    def __init__(self, profile, env, *, mode="approval"):
        self.profile = profile
        self.mode = mode
        self.calls = 0

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        self.calls += 1
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
            security=SecuritySettings(approval_required=True, sandbox_required=True),
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
        self.assertIn(current.status.value, {"waiting_for_approval", "running", "completed"})

        approved = await self.context.control_plane.approve(execution.id, "tool-approval", "approved in test")
        self.assertTrue(approved)
        await asyncio.sleep(0.05)

        final = await self.context.execution_store.get_execution(execution.id)
        self.assertIsNotNone(final)
        self.assertIn(final.status.value, {"completed", "running"})

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
            security=SecuritySettings(approval_required=True, sandbox_required=True),
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

    async def test_stale_execution_repair_handles_queued_running_paused_and_cancelling(self):
        old_heartbeat = utc_now() - timedelta(seconds=10)
        executions = {}
        for status in ("queued", "running", "paused", "cancelling"):
            execution = await self.context.runtime_registry.create_execution(
                self.workflow.id,
                {"topic": status},
                {"created_by": "tester"},
                runtime_adapter_id="native",
            )
            execution.status = execution.status.__class__(status)
            execution.worker_id = f"old-worker-{status}"
            execution.last_heartbeat_at = old_heartbeat
            await self.context.execution_store.update_execution(execution)
            executions[status] = execution

        repaired = await self.context.control_plane.repair_stale_executions()

        self.assertEqual(
            {item["previous_status"]: item["repair_action"] for item in repaired},
            {
                "queued": "requeued",
                "running": "requeued",
                "paused": "requeued",
                "cancelling": "marked_cancelled",
            },
        )
        for status in ("queued", "running", "paused"):
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
        cancelling_events = await self.context.execution_store.list_events(cancelling.id)
        self.assertEqual(cancelling_events[-1].event_type, ExecutionEventType.EXECUTION_CANCELLED)

        metrics = self.context.runtime_operations.snapshot_dict()
        self.assertEqual(metrics["counters"]["stale_execution_repairs"], 4)
        self.assertEqual(metrics["counters"]["stale_execution_repairs.requeued"], 3)
        self.assertEqual(metrics["counters"]["stale_execution_repairs.marked_cancelled"], 1)

    async def test_queue_start_prepares_isolated_runtime_when_enabled(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                return RuntimeRevision(
                    id="runtime-rev-1",
                    fingerprint="fp-1",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="rev-1",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
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
        self.context.control_plane.runtime_container_manager = FakeRuntimeContainerManager()
        self.context.control_plane.runtime_registry.start_execution = fail_start_execution

        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "isolated"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        await self.context.control_plane.queue_start(execution.id)
        await asyncio.sleep(0.05)

        current = await self.context.execution_store.get_execution(execution.id)
        events = await self.context.execution_store.list_events(execution.id)

        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.runtime_revision_id, "runtime-rev-1")
        self.assertEqual(current.runtime_fingerprint, "fp-1")
        self.assertEqual(current.container_id, "container-1")
        self.assertEqual(current.container_status, "running")
        self.assertEqual(current.status.value, "queued")
        event_types = [event.event_type.value for event in events]
        self.assertIn("runtime.revision.resolved", event_types)
        self.assertIn("container.created", event_types)
        self.assertIn("container.started", event_types)

    async def test_queue_start_prepares_isolated_runtime_when_execution_requests_docker_host(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                return RuntimeRevision(
                    id="runtime-rev-host",
                    fingerprint="fp-host",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="host",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
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
                return RuntimeRevision(
                    id="runtime-rev-host-backend",
                    fingerprint="fp-host-backend",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="host-backend",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
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
                "CODEX_CLI_CWD": "/Users/example/workspace/agency",
                "EXECUTION_CODEX_CLI_CWD": "",
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
        self.assertEqual(manager.created_spec.env["CODEX_CLI_CWD"], "/app")

    async def test_queue_start_resolves_runtime_revision_in_shadow_mode_without_container_startup(self):
        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
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
                return RuntimeRevision(
                    id="runtime-rev-2",
                    fingerprint="fp-2",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="rev-2",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
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
                return RuntimeRevision(
                    id="runtime-rev-workflow",
                    fingerprint="fp-workflow",
                    build_status=RuntimeRevisionStatus.READY,
                    image_name="agency-runtime",
                    image_tag="workflow",
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
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
        await self.context.execution_store.update_execution(old_execution)

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
        self.assertEqual(manager.stopped, ["container-old"])
        self.assertEqual(manager.removed, [("container-old", True)])
        self.assertIn("container.replaced", [event.event_type.value for event in old_events])


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
                )
            )
        )

    def test_event_replay_endpoint(self):
        execution = self.context.execution_store._executions.setdefault(  # noqa: SLF001
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
