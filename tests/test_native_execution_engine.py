from __future__ import annotations

import unittest

from app.domain import (
    AgentDefinition,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    ModelProfileDefinition,
    TaskDefinition,
    ToolDefinition,
    UserDefinition,
    WorkflowDefinition,
    WorkflowNodeDefinition,
)
from app.domain.models import (
    FrameworkHints,
    MCPExposureSettings,
    MemorySettings,
    SecuritySettings,
    ToolImplementationReference,
)
from app.llm.base import ModelResponse, ModelToolCall
from app.llm.registry import ModelProviderRegistry
from app.api.context import create_test_api_context
from app.runtime.adapters import NativeRuntimeAdapter, RuntimeAdapterRegistry
from app.runtime.native.engine import ExecutionEngine
from app.runtime.native.state import InMemoryExecutionStore, InMemoryModelProfileRepository, InMemoryWorkflowRepository


class FakeModelClient:
    provider_key = "fake"
    last_messages = None

    def __init__(self, profile, env, *, scenario="success"):
        self.profile = profile
        self.scenario = scenario
        self.calls = 0

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        FakeModelClient.last_messages = messages
        self.calls += 1
        if self.scenario == "no_tool":
            return ModelResponse(
                content="Final answer",
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        if self.scenario == "tool_fail":
            return ModelResponse(
                content="Need tool",
                tool_calls=[ModelToolCall(id="tool-fail", name="Fail Tool", arguments={"text": "boom"})],
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        if self.scenario == "artifact":
            if self.calls == 1:
                return ModelResponse(
                    content="Create artifact",
                    tool_calls=[ModelToolCall(id="tool-artifact", name="Artifact Tool", arguments={"name": "report"})],
                    provider=self.profile.provider,
                    model=self.profile.model,
                    latency_ms=1,
                )
            return ModelResponse(content="Artifact created", provider=self.profile.provider, model=self.profile.model,
                                 latency_ms=1)
        if self.scenario == "max_iterations":
            return ModelResponse(
                content="Use tool again",
                tool_calls=[ModelToolCall(id="tool-echo", name="Echo Tool", arguments={"text": "loop"})],
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        if self.scenario == "pause":
            return ModelResponse(
                content="Need tool",
                tool_calls=[ModelToolCall(id="tool-echo", name="Echo Tool", arguments={"text": "pause"})],
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        if self.scenario == "cancel":
            return ModelResponse(
                content="Need tool",
                tool_calls=[ModelToolCall(id="tool-echo", name="Echo Tool", arguments={"text": "cancel"})],
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        if self.calls == 1:
            return ModelResponse(
                content="Calling tool",
                tool_calls=[ModelToolCall(id="tool-echo", name="Echo Tool", arguments={"text": "hello"})],
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        return ModelResponse(
            content="Final answer",
            provider=self.profile.provider,
            model=self.profile.model,
            latency_ms=1,
        )

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content={"ok": True}, provider=self.profile.provider, model=self.profile.model,
                             latency_ms=1)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "chunk"

    def count_tokens(self, messages, **kwargs):
        return 1

    def health_check(self):
        return {"ok": True}


class PausingModelClient(FakeModelClient):
    def __init__(self, profile, env, engine):
        super().__init__(profile, env, scenario="pause")
        self.engine = engine

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        if self.calls == 0 and self.engine._last_execution_id:
            self.engine._states[self.engine._last_execution_id].paused = True
        return super().generate_text(messages, temperature=temperature, max_tokens=max_tokens, **kwargs)


class CancellingModelClient(FakeModelClient):
    def __init__(self, profile, env, engine):
        super().__init__(profile, env, scenario="cancel")
        self.engine = engine

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        if self.calls == 0 and self.engine._last_execution_id:
            self.engine._states[self.engine._last_execution_id].cancelled = True
        return super().generate_text(messages, temperature=temperature, max_tokens=max_tokens, **kwargs)


class NativeExecutionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeModelClient.last_messages = None
        self.model_registry = ModelProviderRegistry()
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env))
        self.workflow_repository = InMemoryWorkflowRepository()
        self.profile_repository = InMemoryModelProfileRepository()
        self.execution_store = InMemoryExecutionStore()
        self.engine = ExecutionEngine(
            workflow_repository=self.workflow_repository,
            model_profile_repository=self.profile_repository,
            execution_store=self.execution_store,
            model_provider_registry=self.model_registry,
        )
        self.engine._last_execution_id = None
        self.runtime_registry = RuntimeAdapterRegistry(
            workflow_repository=self.workflow_repository,
            model_profile_repository=self.profile_repository,
            execution_store=self.execution_store,
        )
        self.runtime_registry.register(NativeRuntimeAdapter(self.engine))

        self.profile = ModelProfileDefinition(
            id="profile-1",
            name="Fake Profile",
            provider="fake",
            model="fake-model",
            supports_tools=True,
        )
        await self.runtime_registry.register_model_profile(self.profile)

    def _workflow(self, *, tool: ToolDefinition, max_iterations: int = 5) -> WorkflowDefinition:
        agent = AgentDefinition(
            id="agent-1",
            name="Agent One",
            instructions="Be helpful",
            model_profile_id=self.profile.id,
            tool_ids=[tool.id],
            memory=MemorySettings(enabled=True),
            framework_hints=FrameworkHints(adapter_config={"max_iterations": max_iterations}),
        )
        task = TaskDefinition(
            id="task-1",
            name="Task One",
            description="Do the work",
            agent_id=agent.id,
            tool_ids=[tool.id],
        )
        node = WorkflowNodeDefinition(
            id="node-1",
            name="Task Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        return WorkflowDefinition(
            id="workflow-1",
            name="Workflow One",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[tool],
            default_runtime_adapter_id="native",
        )

    async def test_successful_execution(self):
        tool = ToolDefinition(
            id="tool-echo",
            name="Echo Tool",
            description="Echoes text",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})
        self.engine._last_execution_id = execution.id

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)

        self.assertEqual(result.status.value, "completed")
        self.assertEqual(result.output_payload["final_output"], "Final answer")
        self.assertEqual(events[0].event_type.value, "execution.created")
        self.assertEqual(events[-1].event_type.value, "execution.completed")

    async def test_native_runtime_injects_shared_workflow_memory(self):
        context = create_test_api_context()
        context.llm_provider_registry.register(
            "fake",
            lambda profile, env: FakeModelClient(profile, env, scenario="no_tool"),
        )
        profile = ModelProfileDefinition(
            id="profile-shared-memory",
            name="Shared Memory Profile",
            provider="fake",
            model="fake-model",
        )
        await context.runtime_registry.register_model_profile(profile)
        await context.user_repo.save(UserDefinition(id="user-1", email="user-1@example.test"))

        agent = AgentDefinition(
            id="agent-shared-memory",
            name="Shared Memory Agent",
            instructions="Use relevant context.",
            model_profile_id=profile.id,
            memory=MemorySettings(enabled=True, scope="workflow"),
        )
        task = TaskDefinition(
            id="task-shared-memory",
            name="Shared Memory Task",
            description="Prepare the release plan",
            agent_id=agent.id,
        )
        node = WorkflowNodeDefinition(
            id="node-shared-memory",
            name="Shared Memory Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-shared-memory",
            name="Shared Memory Workflow",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[],
            default_runtime_adapter_id="native",
            metadata={
                "created_by": "user-1",
                "owner_ids": ["user-1"],
                "shared_memory": {"enabled": True},
            },
        )
        await context.runtime_registry.register_workflow(workflow)
        await context.memory_repo.create(
            MemoryRecord(
                scope=MemoryScope.WORKFLOW,
                workflow_id=workflow.id,
                content="Release plans must include QA signoff before launch.",
                summary="QA signoff is required before launch.",
                memory_kind=MemoryKind.DECISION,
                status=MemoryStatus.ACTIVE,
            )
        )

        execution = await context.runtime_registry.create_execution(
            workflow.id,
            {"prompt": "ship it"},
            {"source": "test", "created_by": "user-1"},
        )
        result = await context.runtime_registry.start_execution(execution.id)

        self.assertEqual(result.status.value, "completed")
        system_message = FakeModelClient.last_messages[0].content
        self.assertIn("Relevant operational memory", system_message)
        self.assertIn("QA signoff is required before launch.", system_message)

    async def test_failed_tool_call(self):
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env, scenario="tool_fail"))
        tool = ToolDefinition(
            id="tool-fail",
            name="Fail Tool",
            description="Fails",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="failing_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {}, {"source": "test"})
        self.engine._last_execution_id = execution.id

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)

        self.assertEqual(result.status.value, "failed")
        self.assertTrue(any(event.event_type.value == "tool.call.failed" for event in events))

    async def test_paused_execution(self):
        self.model_registry.register("fake", lambda profile, env: PausingModelClient(profile, env, self.engine))
        tool = ToolDefinition(
            id="tool-echo",
            name="Echo Tool",
            description="Echoes text",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {}, {"source": "test"})
        self.engine._last_execution_id = execution.id

        result = await self.runtime_registry.start_execution(execution.id)

        self.assertEqual(result.status.value, "paused")

    async def test_cancelled_execution(self):
        self.model_registry.register("fake", lambda profile, env: CancellingModelClient(profile, env, self.engine))
        tool = ToolDefinition(
            id="tool-echo",
            name="Echo Tool",
            description="Echoes text",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {}, {"source": "test"})
        self.engine._last_execution_id = execution.id

        result = await self.runtime_registry.start_execution(execution.id)

        self.assertEqual(result.status.value, "cancelled")

    async def test_max_iterations_reached(self):
        self.model_registry.register("fake",
                                     lambda profile, env: FakeModelClient(profile, env, scenario="max_iterations"))
        tool = ToolDefinition(
            id="tool-echo",
            name="Echo Tool",
            description="Echoes text",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool, max_iterations=2)
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {}, {"source": "test"})
        self.engine._last_execution_id = execution.id

        result = await self.runtime_registry.start_execution(execution.id)

        self.assertEqual(result.status.value, "failed")
        self.assertIn("Max iterations reached", result.error)

    async def test_event_sequence_validation(self):
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env, scenario="artifact"))
        tool = ToolDefinition(
            id="tool-artifact",
            name="Artifact Tool",
            description="Creates artifact",
            input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="artifact_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {}, {"source": "test"})
        self.engine._last_execution_id = execution.id

        await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        artifacts = await self.execution_store.list_artifacts(execution.id)

        event_types = [event.event_type.value for event in events]
        self.assertEqual(event_types[0], "execution.created")
        self.assertIn("task.started", event_types)
        self.assertIn("tool.call.started", event_types)
        self.assertIn("artifact.created", event_types)
        self.assertEqual(event_types[-1], "execution.completed")
        self.assertEqual(len(artifacts), 1)
