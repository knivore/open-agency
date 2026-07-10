from __future__ import annotations

import asyncio
from datetime import timedelta
import unittest

from app.domain import (
    AgentDefinition,
    ConnectorBindingDefinition,
    ContextHealth,
    Execution,
    ExecutionStatus,
    ExecutionEventType,
    GraphContextSettings,
    MemoryType,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    ModelProfileDefinition,
    TaskDefinition,
    ToolDefinition,
    TokenBudgetStatus,
    UserDefinition,
    WorkflowDefinition,
    WorkflowEdgeDefinition,
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
from app.runtime.adapters.native_adapter import NativeRuntimeAdapter
from app.runtime.governance.compaction import RuntimeContextCompactor
from app.runtime.native.approvals import ApprovalDecision
from app.runtime.native.engine import ExecutionEngine
from app.runtime.native.state import (
    InMemoryExecutionStore,
    InMemoryModelProfileRepository,
    InMemoryWorkflowRepository,
    NativeExecutionState,
    prune_expired_graph_working_sets,
    record_graph_context_working_set_entry,
)
from app.runtime.registry import RuntimeAdapterRegistry
from app.services.agent_tools import graph_system_tool_definitions
from app.tools.cli_discovery import resolve_tool


class FakeModelClient:
    provider_key = "fake"
    last_messages = None
    last_max_tokens = None
    last_profile_id = None
    last_tools = None

    def __init__(self, profile, env, *, scenario="success"):
        self.profile = profile
        self.scenario = scenario
        self.calls = 0

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        FakeModelClient.last_messages = messages
        FakeModelClient.last_max_tokens = max_tokens
        FakeModelClient.last_profile_id = self.profile.id
        FakeModelClient.last_tools = kwargs.get("tools")
        self.calls += 1
        if self.scenario == "fail_once" and self.calls == 1:
            raise RuntimeError("transient model failure")
        if self.scenario == "no_tool":
            return ModelResponse(
                content="Final answer",
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        if self.scenario == "usage_budget":
            return ModelResponse(
                content="Final answer",
                usage={"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25},
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        if self.scenario == "budget_compaction":
            if self.calls == 1:
                return ModelResponse(
                    content="Need tool output before continuing.",
                    tool_calls=[
                        ModelToolCall(
                            id="tool-large-budget",
                            name="Echo Tool",
                            arguments={"text": "budget-context " * 700},
                        )
                    ],
                    usage={"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25},
                    provider=self.profile.provider,
                    model=self.profile.model,
                    latency_ms=1,
                )
            return ModelResponse(
                content="Final answer after budget compaction",
                usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        if self.scenario == "large_tool_compaction":
            if self.calls == 1:
                return ModelResponse(
                    content="Need tool output",
                    tool_calls=[
                        ModelToolCall(
                            id="tool-large",
                            name="Echo Tool",
                            arguments={"text": "large-context " * 700},
                        )
                    ],
                    provider=self.profile.provider,
                    model=self.profile.model,
                    latency_ms=1,
                )
            return ModelResponse(
                content="Final answer after compacted context",
                usage={"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220},
                provider=self.profile.provider,
                model=self.profile.model,
                latency_ms=1,
            )
        if self.scenario == "context_length_retry":
            if self.calls == 1:
                return ModelResponse(
                    content="Need tool output before retry.",
                    tool_calls=[
                        ModelToolCall(
                            id="tool-context-length",
                            name="Echo Tool",
                            arguments={"text": "retry-context " * 700},
                        )
                    ],
                    provider=self.profile.provider,
                    model=self.profile.model,
                    latency_ms=1,
                )
            if self.calls == 2:
                raise RuntimeError("maximum context length exceeded for this model")
            return ModelResponse(
                content="Final answer after context-length compaction",
                usage={"prompt_tokens": 150, "completion_tokens": 15, "total_tokens": 165},
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


class SlowAsyncModelClient(FakeModelClient):
    async def agenerate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        await asyncio.sleep(2)
        return ModelResponse(
            content="Too late",
            provider=self.profile.provider,
            model=self.profile.model,
            latency_ms=2000,
        )


class FallbackSwitchingModelClient(FakeModelClient):
    calls: list[str] = []

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        self.__class__.calls.append(self.profile.model)
        if self.profile.model == "primary-timeout-model":
            raise TimeoutError("primary model timed out")
        return ModelResponse(
            content="Final answer from fallback",
            usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            provider=self.profile.provider,
            model=self.profile.model,
            latency_ms=1,
        )


class NativeExecutionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeModelClient.last_messages = None
        FakeModelClient.last_max_tokens = None
        FakeModelClient.last_profile_id = None
        FakeModelClient.last_tools = None
        FallbackSwitchingModelClient.calls = []
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

    async def test_builtin_tool_id_without_embedded_definition_is_exposed_to_model(self):
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env, scenario="no_tool"))
        voice_tool = resolve_tool("agency.voice.generate")
        agent = AgentDefinition(
            id="agent-voice",
            name="Voice Agent",
            instructions="Generate audio when requested.",
            model_profile_id=self.profile.id,
            tool_ids=[voice_tool.id],
        )
        task = TaskDefinition(
            id="task-voice",
            name="Generate lesson audio",
            description="Generate a voice version of the daily lesson.",
            agent_id=agent.id,
            tool_ids=[voice_tool.id],
        )
        node = WorkflowNodeDefinition(
            id="node-voice",
            name="Generate lesson audio",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-voice-builtin-id",
            name="Voice builtin ID workflow",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        exposed_tool_names = {
            item["function"]["name"]
            for item in FakeModelClient.last_tools or []
            if isinstance(item, dict) and isinstance(item.get("function"), dict)
        }

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        self.assertIn("generate_voice", exposed_tool_names)

    async def test_builtin_tool_id_without_embedded_definition_resolves_for_execution(self):
        voice_tool = resolve_tool("agency.voice.generate")
        workflow = WorkflowDefinition(
            id="workflow-voice-resolve",
            name="Voice builtin resolution workflow",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-voice",
                    name="Generate lesson audio",
                    node_type="task",
                    task_id="task-voice",
                    agent_id="agent-voice",
                )
            ],
            edges=[],
            entrypoint="node-voice",
            task_definitions=[
                TaskDefinition(
                    id="task-voice",
                    name="Generate lesson audio",
                    description="Generate a voice version of the daily lesson.",
                    agent_id="agent-voice",
                    tool_ids=[voice_tool.id],
                )
            ],
            agent_definitions=[
                AgentDefinition(
                    id="agent-voice",
                    name="Voice Agent",
                    instructions="Generate audio when requested.",
                    model_profile_id=self.profile.id,
                    tool_ids=[voice_tool.id],
                )
            ],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )

        resolved = self.engine.agent_executor.tool_executor.resolve_tool(
            workflow,
            voice_tool.id,
            tool_name="generate_voice",
        )

        self.assertEqual(resolved.id, "agency.voice.generate")
        self.assertEqual(resolved.implementation.callable_name, "generate_voice")

    async def test_task_runtime_overrides_model_profile_and_max_tokens(self):
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env, scenario="no_tool"))
        override_profile = ModelProfileDefinition(
            id="profile-task-override",
            name="Task Override Profile",
            provider="fake",
            model="task-override-model",
            max_tokens=999,
            supports_tools=True,
        )
        await self.runtime_registry.register_model_profile(override_profile)
        tool = ToolDefinition(
            id="tool-echo",
            name="Echo Tool",
            description="Echoes text",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        workflow.task_definitions[0].metadata = {
            "task_runtime_overrides": {
                "model_profile_id": override_profile.id,
                "max_tokens": 123,
            }
        }
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        request_event = next(event for event in events if event.event_type == ExecutionEventType.LLM_REQUEST_CREATED)
        task_event = next(event for event in events if event.event_type == ExecutionEventType.TASK_STARTED)
        step_started = next(event for event in events if event.event_type == ExecutionEventType.AGENT_STEP_STARTED)
        step_completed = next(event for event in events if event.event_type == ExecutionEventType.AGENT_STEP_COMPLETED)

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        self.assertEqual(FakeModelClient.last_profile_id, override_profile.id)
        self.assertEqual(FakeModelClient.last_max_tokens, 123)
        self.assertEqual(request_event.payload["model_profile_id"], override_profile.id)
        self.assertEqual(request_event.metrics["reserved_completion_tokens"], 123)
        self.assertEqual(task_event.payload["runtime_overrides"]["max_tokens"], 123)
        self.assertEqual(step_started.payload["step_kind"], "task_execution")
        self.assertEqual(step_started.payload["task_id"], workflow.task_definitions[0].id)
        self.assertEqual(step_started.payload["agent_id"], workflow.agent_definitions[0].id)
        self.assertEqual(step_started.payload["model_profile_id"], override_profile.id)
        self.assertIn("executing task", step_started.payload["summary"])
        self.assertEqual(step_completed.payload["step_kind"], "task_execution")
        self.assertEqual(step_completed.payload["task_id"], workflow.task_definitions[0].id)
        self.assertIn("completed task", step_completed.payload["summary"])

    async def test_task_runtime_overrides_retry_transient_task_failure(self):
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env, scenario="fail_once"))
        tool = ToolDefinition(
            id="tool-echo",
            name="Echo Tool",
            description="Echoes text",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        workflow.task_definitions[0].max_retries = 1
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        retry_event = next(event for event in events if event.event_type == ExecutionEventType.AGENT_STEP_FAILED)

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        self.assertEqual(result.output_payload["final_output"], "Final answer")
        self.assertEqual(retry_event.payload["attempt"], 1)
        self.assertEqual(retry_event.payload["max_retries"], 1)
        self.assertTrue(retry_event.payload["will_retry"])
        self.assertEqual(retry_event.payload["error_type"], "RuntimeError")

    async def test_workflow_runtime_policy_supplies_default_task_retries(self):
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env, scenario="fail_once"))
        tool = ToolDefinition(
            id="tool-echo",
            name="Echo Tool",
            description="Echoes text",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        workflow.max_retries = 1
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        retry_event = next(event for event in events if event.event_type == ExecutionEventType.AGENT_STEP_FAILED)

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        self.assertEqual(retry_event.payload["max_retries"], 1)
        self.assertTrue(retry_event.payload["will_retry"])

    async def test_workflow_approval_mode_before_run_requests_approval(self):
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env, scenario="no_tool"))

        async def approve_workflow(**_kwargs):
            return ApprovalDecision(granted=True, metadata={"mode": "test"})

        self.engine.approval_manager.delegate_decision_provider = approve_workflow
        agent = AgentDefinition(
            id="agent-1",
            name="Agent One",
            instructions="Be helpful",
            model_profile_id=self.profile.id,
        )
        task = TaskDefinition(
            id="task-1",
            name="Task One",
            description="Do the work",
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-approval-policy",
            name="Approval Policy Workflow",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-1",
                    name="Task One",
                    node_type="task",
                    task_id=task.id,
                    agent_id=agent.id,
                )
            ],
            edges=[],
            entrypoint="node-1",
            task_definitions=[task],
            agent_definitions=[agent],
            default_runtime_adapter_id="native",
            approval_mode="before_run",
        )
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        approval_events = [
            event for event in events
            if event.event_type in {
                ExecutionEventType.APPROVAL_REQUESTED,
                ExecutionEventType.APPROVAL_GRANTED,
            }
        ]

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        self.assertEqual(
            [event.event_type for event in approval_events],
            [ExecutionEventType.APPROVAL_REQUESTED, ExecutionEventType.APPROVAL_GRANTED],
        )
        self.assertEqual([event.payload["approval_type"] for event in approval_events], ["workflow", "workflow"])

    async def test_workflow_max_runtime_bounds_task_execution(self):
        self.model_registry.register("fake", lambda profile, env: SlowAsyncModelClient(profile, env))
        agent = AgentDefinition(
            id="agent-1",
            name="Agent One",
            instructions="Be helpful",
            model_profile_id=self.profile.id,
        )
        task = TaskDefinition(
            id="task-1",
            name="Task One",
            description="Do the work",
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-runtime-deadline",
            name="Runtime Deadline Workflow",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-1",
                    name="Task One",
                    node_type="task",
                    task_id=task.id,
                    agent_id=agent.id,
                )
            ],
            edges=[],
            entrypoint="node-1",
            task_definitions=[task],
            agent_definitions=[agent],
            default_runtime_adapter_id="native",
            max_runtime_seconds=1,
        )
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        failure_event = next(event for event in events if event.event_type == ExecutionEventType.AGENT_STEP_FAILED)

        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error, "Workflow max runtime exceeded.")
        self.assertEqual(failure_event.payload["error"], "Workflow max runtime exceeded.")
        self.assertFalse(failure_event.payload["will_retry"])

    async def test_task_timeout_retries_when_workflow_deadline_has_time_remaining(self):
        self.model_registry.register("fake", lambda profile, env: SlowAsyncModelClient(profile, env))
        agent = AgentDefinition(
            id="agent-1",
            name="Agent One",
            instructions="Be helpful",
            model_profile_id=self.profile.id,
        )
        task = TaskDefinition(
            id="task-1",
            name="Task One",
            description="Do the work",
            agent_id=agent.id,
            timeout_seconds=1,
            max_retries=1,
        )
        workflow = WorkflowDefinition(
            id="workflow-task-timeout-with-deadline",
            name="Task Timeout With Deadline Workflow",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-1",
                    name="Task One",
                    node_type="task",
                    task_id=task.id,
                    agent_id=agent.id,
                )
            ],
            edges=[],
            entrypoint="node-1",
            task_definitions=[task],
            agent_definitions=[agent],
            default_runtime_adapter_id="native",
            max_runtime_seconds=10,
        )
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        failure_events = [
            event for event in await self.execution_store.list_events(execution.id)
            if event.event_type == ExecutionEventType.AGENT_STEP_FAILED
        ]

        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error, "Task execution timed out.")
        self.assertEqual(len(failure_events), 2)
        self.assertEqual([event.payload["will_retry"] for event in failure_events], [True, False])
        self.assertEqual(
            [event.payload["error"] for event in failure_events],
            ["Task execution timed out.", "Task execution timed out."],
        )

    async def test_task_retry_metadata_resumes_from_failed_task_with_prior_outputs(self):
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env, scenario="no_tool"))
        agent = AgentDefinition(
            id="agent-1",
            name="Agent One",
            instructions="Be helpful",
            model_profile_id=self.profile.id,
        )
        first_task = TaskDefinition(
            id="task-1",
            name="First Task",
            description="Already completed task.",
            agent_id=agent.id,
        )
        retry_task = TaskDefinition(
            id="task-2",
            name="Retry Task",
            description="Task retried from the graph.",
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-task-retry",
            name="Task Retry Workflow",
            nodes=[
                WorkflowNodeDefinition(id="node-1", name="First Task", node_type="task", task_id=first_task.id),
                WorkflowNodeDefinition(id="node-2", name="Retry Task", node_type="task", task_id=retry_task.id),
            ],
            edges=[
                WorkflowEdgeDefinition(
                    id="edge-1-2",
                    source_node_id="node-1",
                    target_node_id="node-2",
                )
            ],
            entrypoint="node-1",
            task_definitions=[first_task, retry_task],
            agent_definitions=[agent],
            default_runtime_adapter_id="native",
        )
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})
        execution.metadata = {
            **(execution.metadata or {}),
            "task_retry": {
                "source_execution_id": "execution-failed",
                "task_id": retry_task.id,
                "node_id": "node-2",
                "reason": "Retry from graph",
                "prior_node_outputs": {"node-1": "Prior answer"},
            },
        }
        await self.execution_store.update_execution(execution)

        result = await self.runtime_registry.start_execution(execution.id)
        task_events = [
            event for event in await self.execution_store.list_events(execution.id)
            if event.event_type == ExecutionEventType.TASK_STARTED
        ]

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        self.assertEqual([event.task_id for event in task_events], ["task-2"])
        self.assertEqual(result.output_payload["node_outputs"]["node-1"], "Prior answer")
        self.assertEqual(result.output_payload["node_outputs"]["node-2"], "Final answer")

    async def test_replacement_execution_resumes_from_source_checkpoint(self):
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env, scenario="no_tool"))
        agent = AgentDefinition(
            id="agent-1",
            name="Agent One",
            instructions="Be helpful",
            model_profile_id=self.profile.id,
        )
        first_task = TaskDefinition(
            id="task-1",
            name="First Task",
            description="Already completed task.",
            agent_id=agent.id,
        )
        retry_task = TaskDefinition(
            id="task-2",
            name="Retry Task",
            description="Task continued from the source checkpoint.",
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-replacement-resume",
            name="Replacement Resume Workflow",
            nodes=[
                WorkflowNodeDefinition(id="node-1", name="First Task", node_type="task", task_id=first_task.id),
                WorkflowNodeDefinition(id="node-2", name="Retry Task", node_type="task", task_id=retry_task.id),
            ],
            edges=[
                WorkflowEdgeDefinition(
                    id="edge-1-2",
                    source_node_id="node-1",
                    target_node_id="node-2",
                )
            ],
            entrypoint="node-1",
            task_definitions=[first_task, retry_task],
            agent_definitions=[agent],
            default_runtime_adapter_id="native",
        )
        await self.runtime_registry.register_workflow(workflow)
        source = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})
        source.status = ExecutionStatus.FAILED
        source.output_payload = {
            "node_outputs": {"node-1": "Prior answer"},
            "final_output": "Prior answer",
            "checkpoint": {
                "current_node_id": "node-2",
                "current_task_id": "task-2",
                "completed_node_ids": ["node-1"],
            },
        }
        await self.execution_store.update_execution(source)

        replacement = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})
        replacement.replacement_of_execution_id = source.id
        await self.execution_store.update_execution(replacement)

        result = await self.runtime_registry.start_execution(replacement.id)
        events = await self.execution_store.list_events(replacement.id)
        task_events = [event for event in events if event.event_type == ExecutionEventType.TASK_STARTED]
        started = next(event for event in events if event.event_type == ExecutionEventType.EXECUTION_STARTED)

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        self.assertEqual([event.task_id for event in task_events], ["task-2"])
        self.assertEqual(result.output_payload["node_outputs"]["node-1"], "Prior answer")
        self.assertEqual(result.output_payload["node_outputs"]["node-2"], "Final answer")
        self.assertEqual(started.payload["resume_checkpoint"]["source_execution_id"], source.id)
        self.assertEqual(started.payload["resume_checkpoint"]["completed_node_ids"], ["node-1"])

    async def test_handoff_edge_emits_runtime_events_with_graph_references(self):
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env, scenario="no_tool"))
        source_agent = AgentDefinition(
            id="agent-1",
            name="Research Agent",
            instructions="Research the topic.",
            model_profile_id=self.profile.id,
            handoff_agent_ids=["agent-2"],
        )
        target_agent = AgentDefinition(
            id="agent-2",
            name="Review Agent",
            instructions="Review the handoff.",
            model_profile_id=self.profile.id,
        )
        source_task = TaskDefinition(
            id="task-1",
            name="Research",
            description="Collect evidence.",
            agent_id=source_agent.id,
        )
        target_task = TaskDefinition(
            id="task-2",
            name="Review",
            description="Review evidence.",
            agent_id=target_agent.id,
            depends_on_task_ids=[source_task.id],
        )
        workflow = WorkflowDefinition(
            id="workflow-handoff-runtime",
            name="Handoff Runtime Workflow",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-1",
                    name="Research",
                    node_type="task",
                    task_id=source_task.id,
                    agent_id=source_agent.id,
                ),
                WorkflowNodeDefinition(
                    id="node-2",
                    name="Review",
                    node_type="task",
                    task_id=target_task.id,
                    agent_id=target_agent.id,
                ),
            ],
            edges=[
                WorkflowEdgeDefinition(
                    id="edge-handoff-1-2",
                    source_node_id="node-1",
                    target_node_id="node-2",
                    edge_type="handoff",
                )
            ],
            entrypoint="node-1",
            task_definitions=[source_task, target_task],
            agent_definitions=[source_agent, target_agent],
            default_runtime_adapter_id="native",
        )
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        handoff_events = [
            event for event in events
            if event.event_type in {
                ExecutionEventType.HANDOFF_REQUESTED,
                ExecutionEventType.HANDOFF_COMPLETED,
            }
        ]

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        self.assertEqual(
            [event.event_type for event in handoff_events],
            [ExecutionEventType.HANDOFF_REQUESTED, ExecutionEventType.HANDOFF_COMPLETED],
        )
        self.assertEqual([event.agent_id for event in handoff_events], ["agent-2", "agent-2"])
        self.assertEqual([event.task_id for event in handoff_events], ["task-2", "task-2"])
        for event in handoff_events:
            self.assertEqual(event.payload["edge_id"], "edge-handoff-1-2")
            self.assertEqual(event.payload["source_node_id"], "node-1")
            self.assertEqual(event.payload["target_node_id"], "node-2")
            self.assertEqual(event.payload["source_task_id"], "task-1")
            self.assertEqual(event.payload["target_task_id"], "task-2")
            self.assertEqual(event.payload["source_agent_id"], "agent-1")
            self.assertEqual(event.payload["target_agent_id"], "agent-2")
            self.assertTrue(event.payload["allowed"])
            self.assertEqual(event.payload["handoff_relationship"], "declared")

    async def test_failed_execution_persists_checkpoint_node_outputs(self):
        class FailSecondTaskModelClient(FakeModelClient):
            calls = 0

            def __init__(self, profile, env):
                super().__init__(profile, env)

            def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
                self.__class__.calls += 1
                if self.__class__.calls == 2:
                    raise RuntimeError("second task failed")
                return ModelResponse(
                    content="First task output",
                    provider=self.profile.provider,
                    model=self.profile.model,
                    latency_ms=1,
                )

        self.model_registry.register("fake", lambda profile, env: FailSecondTaskModelClient(profile, env))
        agent = AgentDefinition(
            id="agent-1",
            name="Agent One",
            instructions="Be helpful",
            model_profile_id=self.profile.id,
        )
        first_task = TaskDefinition(
            id="task-1",
            name="First Task",
            description="Task that completes.",
            agent_id=agent.id,
        )
        failing_task = TaskDefinition(
            id="task-2",
            name="Failing Task",
            description="Task that fails.",
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-checkpoint-failure",
            name="Checkpoint Failure Workflow",
            nodes=[
                WorkflowNodeDefinition(id="node-1", name="First Task", node_type="task", task_id=first_task.id),
                WorkflowNodeDefinition(id="node-2", name="Failing Task", node_type="task", task_id=failing_task.id),
            ],
            edges=[
                WorkflowEdgeDefinition(
                    id="edge-1-2",
                    source_node_id="node-1",
                    target_node_id="node-2",
                )
            ],
            entrypoint="node-1",
            task_definitions=[first_task, failing_task],
            agent_definitions=[agent],
            default_runtime_adapter_id="native",
        )
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)

        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.output_payload["node_outputs"], {"node-1": "First task output"})
        self.assertEqual(result.output_payload["checkpoint"]["current_node_id"], "node-2")
        self.assertEqual(result.output_payload["checkpoint"]["current_task_id"], "task-2")
        self.assertEqual(result.output_payload["checkpoint"]["completed_node_ids"], ["node-1"])

    async def test_connector_binding_is_recorded_on_tool_events(self):
        tool = ToolDefinition(
            id="tool-echo",
            name="Echo Tool",
            description="Echoes text through a connector",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(
                connector_bindings=[
                    ConnectorBindingDefinition(
                        provider="discord",
                        credential_id="credential-discord-support",
                        purpose="support_delivery",
                        target_scope={"channel_id": "channel-123", "guild_id": "guild-456"},
                        identity_summary="Support Discord / #triage",
                    )
                ]
            ),
            tags=["connector"],
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})
        self.engine._last_execution_id = execution.id

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        started_event = next(event for event in events if event.event_type == ExecutionEventType.TOOL_CALL_STARTED)
        completed_event = next(event for event in events if event.event_type == ExecutionEventType.TOOL_CALL_COMPLETED)

        self.assertEqual(result.status.value, "completed")
        self.assertEqual(started_event.payload["connector_binding"]["provider"], "discord-bot")
        self.assertEqual(
            started_event.payload["connector_binding"]["credential_id"],
            "credential-discord-support",
        )
        self.assertEqual(
            completed_event.payload["connector_binding"]["target_scope"]["channel_id"],
            "channel-123",
        )

    async def test_connector_tool_without_binding_fails_before_delivery(self):
        tool = ToolDefinition(
            id="tool-echo",
            name="Echo Tool",
            description="Echoes text through a connector",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            tags=["connector"],
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})
        self.engine._last_execution_id = execution.id

        result = await self.runtime_registry.start_execution(execution.id)

        self.assertEqual(result.status.value, "failed")
        self.assertIn("is connector-backed but has no connector binding", result.error)

    async def test_connector_tool_provider_alias_matches_canonical_workflow_binding(self):
        tool = ToolDefinition(
            id="tool-echo",
            name="Echo Tool",
            description="Echoes text through a connector",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(
                target="tests.native_test_tools",
                callable_name="echo_tool",
                config={"provider": "discord"},
            ),
            security=SecuritySettings(),
            tags=["connector"],
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        workflow.metadata = {
            "connector_bindings": [
                {
                    "provider": "discord-bot",
                    "credential_id": "credential-discord-support",
                    "target_scope": {"channel_id": "channel-123"},
                }
            ]
        }
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})
        self.engine._last_execution_id = execution.id

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        started_event = next(event for event in events if event.event_type == ExecutionEventType.TOOL_CALL_STARTED)

        self.assertEqual(result.status.value, "completed")
        self.assertEqual(started_event.payload["connector_binding"]["provider"], "discord-bot")
        self.assertEqual(
            started_event.payload["connector_binding"]["credential_id"],
            "credential-discord-support",
        )

    async def test_connector_tool_with_multiple_bindings_requires_explicit_target(self):
        tool = ToolDefinition(
            id="tool-echo",
            name="Echo Tool",
            description="Echoes text through a connector",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            tags=["connector"],
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        workflow.metadata = {
            "connector_bindings": [
                {
                    "provider": "discord",
                    "credential_id": "credential-discord-support",
                    "target_scope": {"channel_id": "channel-123"},
                },
                {
                    "provider": "discord",
                    "credential_id": "credential-discord-alerts",
                    "target_scope": {"channel_id": "channel-999"},
                },
            ]
        }
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})
        self.engine._last_execution_id = execution.id

        result = await self.runtime_registry.start_execution(execution.id)

        self.assertEqual(result.status.value, "failed")
        self.assertIn("has multiple connector bindings", result.error)

    async def test_native_execution_emits_model_fallback_events_when_primary_times_out(self):
        self.model_registry.register(
            "fake_fallback",
            lambda profile, env: FallbackSwitchingModelClient(profile, env),
        )
        profile = ModelProfileDefinition(
            id="profile-fallback",
            name="Fallback Profile",
            provider="fake_fallback",
            model="primary-timeout-model",
            fallback_strategy="manual",
            fallback_models=[{"model": "backup-model"}],
        )
        await self.runtime_registry.register_model_profile(profile)
        agent = AgentDefinition(
            id="agent-fallback",
            name="Fallback Agent",
            instructions="Use fallback when needed.",
            model_profile_id=profile.id,
        )
        task = TaskDefinition(
            id="task-fallback",
            name="Fallback Task",
            description="Exercise model fallback.",
            agent_id=agent.id,
        )
        node = WorkflowNodeDefinition(
            id="node-fallback",
            name="Fallback Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-fallback",
            name="Fallback Workflow",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "fallback"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        event_types = [event.event_type for event in events]
        fallback_event = next(event for event in events if event.event_type == ExecutionEventType.MODEL_FALLBACK_USED)
        token_event = next(event for event in events if event.event_type == ExecutionEventType.TOKEN_USAGE_RECORDED)

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        self.assertEqual(result.output_payload["final_output"], "Final answer from fallback")
        self.assertEqual(FallbackSwitchingModelClient.calls, ["primary-timeout-model", "backup-model"])
        self.assertEqual(fallback_event.payload["primary_model"], "primary-timeout-model")
        self.assertEqual(fallback_event.payload["fallback_model"], "backup-model")
        self.assertEqual(fallback_event.metrics["fallback_attempt_count"], 1)
        self.assertEqual(fallback_event.payload["attempts"][0]["error_type"], "TimeoutError")
        self.assertEqual(token_event.payload["usage"]["provider_usage"]["model_fallback"]["fallback_model"], "backup-model")
        self.assertLess(event_types.index(ExecutionEventType.MODEL_FALLBACK_USED), event_types.index(ExecutionEventType.LLM_RESPONSE_CREATED))

    async def test_graph_working_set_tracks_context_and_expires(self):
        state = NativeExecutionState(execution_id="execution-working-set", workflow_id="workflow-working-set")
        entry = {
            "trigger": "subagent_start",
            "reason": "prepare_assigned_agent_context",
            "context": {
                "status": "ok",
                "summary": "Runtime graph context",
                "provenance": {
                    "nodes": [
                        {"id": "task-1", "type": "Task", "source_record_type": "task", "source_record_id": "task-1"},
                        {
                            "id": "decision-1",
                            "type": "Decision",
                            "source_record_type": "decision",
                            "source_record_id": "decision-1",
                        },
                    ],
                    "edges": [],
                },
                "decisions": [{"id": "decision-1", "type": "Decision"}],
                "query_meta": {
                    "intent": "handoff",
                    "budget": "brief",
                    "anchor_type": "task",
                    "anchor_id": "task-1",
                },
            },
        }

        working_set = record_graph_context_working_set_entry(
            state,
            entry,
            owner_agent_id="agent-1",
            workflow_id="workflow-working-set",
            run_id="execution-working-set",
            execution_id="execution-working-set",
            conversation_id="conversation-1",
        )

        self.assertEqual(entry["working_set_id"], working_set.working_set_id)
        self.assertEqual(working_set.conversation_id, "conversation-1")
        self.assertEqual(working_set.anchors, [{"type": "task", "id": "task-1"}])
        self.assertEqual([node["id"] for node in working_set.visited_nodes], ["task-1", "decision-1"])
        self.assertTrue(any(node.get("id") == "decision-1" for node in working_set.selected_nodes))
        working_set.expires_at = working_set.updated_at - timedelta(seconds=1)
        prune_expired_graph_working_sets(state)
        self.assertEqual(state.graph_working_sets, {})

    async def test_native_tool_executor_dispatches_graph_working_set_tool(self):
        create_tool = next(
            tool for tool in graph_system_tool_definitions() if tool.id == "agency.graph.working-set.create"
        )
        agent = AgentDefinition(
            id="agent-working-set-tool",
            name="Working Set Agent",
            instructions="Track graph exploration.",
            model_profile_id=self.profile.id,
            tool_ids=[create_tool.id],
        )
        task = TaskDefinition(
            id="task-working-set-tool",
            name="Working Set Task",
            description="Create a graph working set",
            agent_id=agent.id,
            tool_ids=[create_tool.id],
        )
        node = WorkflowNodeDefinition(
            id="node-working-set-tool",
            name="Working Set Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-working-set-tool",
            name="Working Set Workflow",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[create_tool],
        )
        execution = Execution(
            id="execution-working-set-tool",
            workflow_id=workflow.id,
            runtime_adapter="native",
            input_json={"prompt": "track graph"},
        )
        await self.execution_store.save_execution(execution)
        state = NativeExecutionState(execution_id=execution.id, workflow_id=workflow.id)
        state.current_agent_id = agent.id
        state.current_task_id = task.id
        self.engine._states[execution.id] = state
        self.engine.agent_executor.tool_executor.tool_registry.runtime_registry = self.runtime_registry
        self.engine.agent_executor.tool_executor.tool_registry.execution_store = self.execution_store

        result = await self.engine.agent_executor.tool_executor.execute(
            workflow,
            state,
            self.engine.emitter,
            tool_id=create_tool.id,
            arguments={
                "execution_id": execution.id,
                "owner_agent_id": agent.id,
                "anchors": [{"type": "task", "id": task.id}],
            },
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["working_set"]["owner_agent_id"], agent.id)
        self.assertEqual(result["working_set"]["anchors"], [{"type": "task", "id": task.id}])
        self.assertIn(result["working_set"]["working_set_id"], state.graph_working_sets)

    async def test_native_runtime_injects_auto_graph_context_before_task_start(self):
        self.model_registry.register(
            "fake_no_tool",
            lambda profile, env: FakeModelClient(profile, env, scenario="no_tool"),
        )
        profile = ModelProfileDefinition(
            id="profile-graph-context",
            name="Graph Context Profile",
            provider="fake_no_tool",
            model="fake-model",
        )
        await self.runtime_registry.register_model_profile(profile)

        async def fake_graph_context_retriever(workflow, task, agent, execution, execution_input, state):
            self.assertEqual(workflow.id, "workflow-graph-context")
            self.assertEqual(execution.id, state.execution_id)
            return {
                "trigger": "subagent_start",
                "reason": "prepare_assigned_agent_context",
                "intent": "handoff",
                "budget": "brief",
                "anchor_type": "task",
                "anchor_id": task.id,
                "workflow_id": workflow.id,
                "execution_id": execution.id,
                "task_id": task.id,
                "agent_id": agent.id,
                "context": {
                    "status": "ok",
                    "summary": "Agency Graph context for task:task-graph-context using intent handoff.",
                    "facts": ["Task task-graph-context: Draft the implementation plan"],
                    "related_memories": [],
                    "recent_events": [],
                    "failures": [],
                    "decisions": ["Decision decision-1: Keep the graph query read-only"],
                    "constraints": [],
                    "next_actions": ["NextAction next-1: Continue from the graph tool phase"],
                    "provenance": {
                        "nodes": [
                            {
                                "id": "task-graph-context",
                                "type": "Task",
                                "source_record_type": "task",
                                "source_record_id": task.id,
                            },
                            {
                                "id": "decision-1",
                                "type": "Decision",
                                "source_record_type": "decision",
                                "source_record_id": "decision-1",
                            },
                        ],
                        "edges": [],
                    },
                    "query_meta": {
                        "intent": "handoff",
                        "budget": "brief",
                        "anchor_type": "task",
                        "anchor_id": task.id,
                        "scope": {
                            "workflow_id": workflow.id,
                            "execution_id": execution.id,
                            "task_id": task.id,
                            "agent_id": agent.id,
                        },
                        "node_count": 3,
                        "edge_count": 2,
                    },
                },
            }

        self.engine.set_graph_context_retriever(fake_graph_context_retriever)

        agent = AgentDefinition(
            id="agent-graph-context",
            name="Graph Context Agent",
            instructions="Use runtime context.",
            model_profile_id=profile.id,
            graph_context=GraphContextSettings(enabled=True),
        )
        task = TaskDefinition(
            id="task-graph-context",
            name="Graph Context Task",
            description="Draft the implementation plan",
            agent_id=agent.id,
        )
        node = WorkflowNodeDefinition(
            id="node-graph-context",
            name="Graph Context Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-graph-context",
            name="Graph Context Workflow",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "continue"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        event_types = [event.event_type for event in events]
        graph_event = next(
            event
            for event in events
            if event.event_type == ExecutionEventType.AGENT_MESSAGE_CREATED
            and event.payload.get("source") == "runtime_graph_context"
        )
        task_started_index = event_types.index(ExecutionEventType.TASK_STARTED)
        graph_event_index = events.index(graph_event)
        system_prompt = FakeModelClient.last_messages[0].content

        self.assertEqual(result.status.value, "completed")
        self.assertLess(graph_event_index, task_started_index)
        self.assertEqual(graph_event.payload["trigger"], "subagent_start")
        self.assertEqual(graph_event.payload["intent"], "handoff")
        self.assertEqual(graph_event.payload["budget"], "brief")
        self.assertEqual(graph_event.payload["anchor_type"], "task")
        state = self.engine._states[execution.id]
        working_set = next(iter(state.graph_working_sets.values()))
        self.assertEqual(graph_event.payload["working_set_id"], working_set.working_set_id)
        self.assertEqual(working_set.owner_agent_id, agent.id)
        self.assertEqual(working_set.execution_id, execution.id)
        self.assertEqual(working_set.anchors[0], {"type": "task", "id": task.id})
        self.assertTrue(any(node.get("id") == "decision-1" for node in working_set.visited_nodes))
        self.assertTrue(any(node.get("id") == task.id for node in working_set.selected_nodes))
        self.assertIn("# Runtime Agency Graph Context", system_prompt)
        self.assertIn("anchor=task:task-graph-context", system_prompt)
        self.assertIn("Keep the graph query read-only", system_prompt)

    async def test_native_runtime_skips_proposal_graph_context_when_context_health_is_critical(self):
        calls = []

        async def fake_proposal_graph_context_retriever(*args):
            calls.append(args)
            return {"trigger": "proposal_tool", "context": {"status": "ok", "query_meta": {}}}

        self.engine.set_proposal_tool_graph_context_retriever(fake_proposal_graph_context_retriever)
        tool = ToolDefinition(
            id="agency.workflow.propose-update",
            name="propose_workflow_update",
            description="Propose workflow update",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="system.workflow", callable_name="propose_workflow_update"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        agent = AgentDefinition(
            id="agent-graph-health",
            name="Graph Health Agent",
            model_profile_id="profile-graph-health",
        )
        task = TaskDefinition(
            id="task-graph-health",
            name="Graph Health Task",
            description="Respect context health.",
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-graph-health",
            name="Graph Health Workflow",
            nodes=[],
            entrypoint="",
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[tool],
        )
        execution = Execution(id="run-graph-health", workflow_id=workflow.id, runtime_adapter="native")
        state = NativeExecutionState(execution_id=execution.id, workflow_id=workflow.id)
        state.current_agent_id = agent.id
        state.current_task_id = task.id
        context_health = ContextHealth(
            estimated_prompt_tokens=900,
            reserved_completion_tokens=100,
            estimated_total_context_tokens=1000,
            context_window=1000,
            remaining_context_tokens=0,
            usage_ratio=1.0,
            status="overflow",
        )

        entry = await self.engine.agent_executor._maybe_retrieve_graph_context_before_proposal_tool(
            workflow=workflow,
            task=task,
            agent=agent,
            execution=execution,
            state=state,
            emitter=self.engine.emitter,
            tool=tool,
            arguments={"workflow_id": workflow.id},
            tool_call_id="call-graph-health",
            context_health=context_health,
            budget_statuses=[],
        )
        events = await self.execution_store.list_events(execution.id)

        self.assertIsNone(entry)
        self.assertEqual(calls, [])
        self.assertTrue(
            any(
                event.event_type == ExecutionEventType.AGENT_MESSAGE_CREATED
                and event.payload.get("source") == "runtime_graph_context"
                and event.payload.get("reason") == "context_health_guard"
                and event.payload.get("skip_reason") == "context_health_critical"
                for event in events
            )
        )

    async def test_native_runtime_skips_proposal_graph_context_when_token_budget_exceeded(self):
        calls = []

        async def fake_proposal_graph_context_retriever(*args):
            calls.append(args)
            return {"trigger": "proposal_tool", "context": {"status": "ok", "query_meta": {}}}

        self.engine.set_proposal_tool_graph_context_retriever(fake_proposal_graph_context_retriever)
        tool = ToolDefinition(
            id="agency.workflow.propose-update",
            name="propose_workflow_update",
            description="Propose workflow update",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="system.workflow", callable_name="propose_workflow_update"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        agent = AgentDefinition(id="agent-graph-budget", name="Graph Budget Agent")
        task = TaskDefinition(
            id="task-graph-budget",
            name="Graph Budget Task",
            description="Respect token budget.",
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-graph-budget",
            name="Graph Budget Workflow",
            nodes=[],
            entrypoint="",
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[tool],
        )
        execution = Execution(id="run-graph-budget", workflow_id=workflow.id, runtime_adapter="native")
        state = NativeExecutionState(execution_id=execution.id, workflow_id=workflow.id)
        budget_status = TokenBudgetStatus(
            scope="run",
            used_tokens=1200,
            budget_tokens=1000,
            usage_ratio=1.2,
            status="exceeded",
            action="warn_only",
        )

        entry = await self.engine.agent_executor._maybe_retrieve_graph_context_before_proposal_tool(
            workflow=workflow,
            task=task,
            agent=agent,
            execution=execution,
            state=state,
            emitter=self.engine.emitter,
            tool=tool,
            arguments={"workflow_id": workflow.id},
            tool_call_id="call-graph-budget",
            context_health=None,
            budget_statuses=[budget_status],
        )
        events = await self.execution_store.list_events(execution.id)

        self.assertIsNone(entry)
        self.assertEqual(calls, [])
        self.assertTrue(
            any(
                event.event_type == ExecutionEventType.AGENT_MESSAGE_CREATED
                and event.payload.get("source") == "runtime_graph_context"
                and event.payload.get("reason") == "budget_limit_guard"
                and event.payload.get("skip_reason") == "budget_limit_exceeded"
                for event in events
            )
        )

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
                "shared_memory": {"enabled": True, "context_pack_limit": 0},
            },
        )
        await context.runtime_registry.register_workflow(workflow)
        await context.memory_repo.create(
            MemoryRecord(
                scope=MemoryScope.WORKFLOW,
                workflow_id=workflow.id,
                content="Release plans must include QA signoff before launch.",
                summary="QA signoff is required before launch.",
                memory_type=MemoryType.DECISION,
                status=MemoryStatus.ACTIVE,
            )
        )
        await context.memory_repo.create(
            MemoryRecord(
                id="selected-context-pack-native",
                scope=MemoryScope.USER,
                created_by_user_id="user-1",
                content="Workflow handoff: launch plan must preserve rollout checkpoints from the compact pack.",
                summary="Launch rollout context pack.",
                source="compact_tool",
                memory_type=MemoryType.CONTEXT_PACK,
                status=MemoryStatus.ACTIVE,
                tags=["context_pack", "workflow", "handoff"],
                metadata={"mode": "handoff"},
            )
        )

        execution = await context.runtime_registry.create_execution(
            workflow.id,
            {"prompt": "ship it", "context_pack_id": "selected-context-pack-native"},
            {"source": "test", "created_by": "user-1"},
        )
        result = await context.runtime_registry.start_execution(execution.id)

        self.assertEqual(result.status.value, "completed")
        system_message = FakeModelClient.last_messages[0].content
        self.assertIn("Relevant compact conversation context", system_message)
        self.assertIn("Launch rollout context pack.", system_message)
        self.assertIn("Relevant operational memory", system_message)
        self.assertIn("QA signoff is required before launch.", system_message)

    async def test_native_runtime_records_token_context_and_budget_warning(self):
        self.model_registry.register(
            "fake",
            lambda profile, env: FakeModelClient(profile, env, scenario="usage_budget"),
        )
        self.profile = ModelProfileDefinition(
            id="profile-usage-budget",
            name="Usage Budget Profile",
            provider="fake",
            model="fake-model",
            max_tokens=10,
            context_window=100,
            parameters={
                "input_token_cost_per_1m": 1.0,
                "output_token_cost_per_1m": 2.0,
                "currency": "USD",
            },
        )
        await self.runtime_registry.register_model_profile(self.profile)
        tool = ToolDefinition(
            id="tool-unused",
            name="Unused Tool",
            description="Unused",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        workflow.metadata = {
            "runtime_governance": {
                "token_budget": {
                    "run_total_tokens": 20,
                    "agent_total_tokens": 20,
                    "warn_ratio": 0.5,
                    "hard_ratio": 2.0,
                    "action": "warn_only",
                }
            }
        }
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        event_types = [event.event_type.value for event in events]
        updated = await self.execution_store.get_execution(execution.id)

        self.assertEqual(result.status.value, "completed")
        self.assertIn("context.health.recorded", event_types)
        self.assertIn("token.usage.recorded", event_types)
        self.assertIn("token.budget.warning", event_types)
        token_event = next(event for event in events if event.event_type.value == "token.usage.recorded")
        self.assertEqual(token_event.payload["usage"]["prompt_tokens"], 10)
        self.assertEqual(token_event.payload["usage"]["completion_tokens"], 15)
        self.assertEqual(token_event.payload["usage"]["total_tokens"], 25)
        self.assertEqual(token_event.payload["usage"]["estimated_cost"], 0.00004)
        governance = updated.metadata["runtime_governance"]
        self.assertEqual(governance["token_usage"]["total"]["total_tokens"], 25)
        self.assertEqual(governance["token_usage"]["by_agent"]["agent-1"]["total_tokens"], 25)
        self.assertEqual(governance["context_health"]["last"]["status"], "normal")

    async def test_native_runtime_fails_when_token_budget_policy_requests_failure(self):
        self.model_registry.register(
            "fake",
            lambda profile, env: FakeModelClient(profile, env, scenario="usage_budget"),
        )
        self.profile = ModelProfileDefinition(
            id="profile-budget-fail",
            name="Budget Fail Profile",
            provider="fake",
            model="fake-model",
            max_tokens=10,
            context_window=100,
        )
        await self.runtime_registry.register_model_profile(self.profile)
        tool = ToolDefinition(
            id="tool-unused",
            name="Unused Tool",
            description="Unused",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        workflow.metadata = {
            "runtime_governance": {
                "token_budget": {
                    "run_total_tokens": 20,
                    "warn_ratio": 0.5,
                    "hard_ratio": 1.0,
                    "action": "fail_execution",
                }
            }
        }
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        event_types = [event.event_type.value for event in events]

        self.assertEqual(result.status.value, "failed")
        self.assertIn("Token budget run exceeded", result.error)
        self.assertIn("token.budget.exceeded", event_types)
        self.assertEqual(events[-1].event_type.value, "execution.failed")

    async def test_native_runtime_pauses_when_token_budget_policy_requests_pause(self):
        self.model_registry.register(
            "fake",
            lambda profile, env: FakeModelClient(profile, env, scenario="usage_budget"),
        )
        self.profile = ModelProfileDefinition(
            id="profile-budget-pause",
            name="Budget Pause Profile",
            provider="fake",
            model="fake-model",
            max_tokens=10,
            context_window=100,
        )
        await self.runtime_registry.register_model_profile(self.profile)
        tool = ToolDefinition(
            id="tool-unused",
            name="Unused Tool",
            description="Unused",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        workflow = self._workflow(tool=tool)
        workflow.metadata = {
            "runtime_governance": {
                "token_budget": {
                    "run_total_tokens": 20,
                    "warn_ratio": 0.5,
                    "hard_ratio": 1.0,
                    "action": "pause_execution",
                }
            }
        }
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        event_types = [event.event_type.value for event in events]

        self.assertEqual(result.status.value, "paused")
        self.assertIn("token.budget.exceeded", event_types)
        self.assertEqual(events[-1].event_type.value, "execution.paused")

    async def test_native_runtime_compacts_when_token_budget_policy_requests_compaction(self):
        self.model_registry.register(
            "fake",
            lambda profile, env: FakeModelClient(profile, env, scenario="budget_compaction"),
        )
        self.engine.set_context_compactor(RuntimeContextCompactor())
        self.profile = ModelProfileDefinition(
            id="profile-budget-compaction",
            name="Budget Compaction Profile",
            provider="fake",
            model="fake-model",
            max_tokens=10,
            context_window=1000,
        )
        await self.runtime_registry.register_model_profile(self.profile)
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
        workflow = self._workflow(tool=tool, max_iterations=3)
        workflow.metadata = {
            "runtime_governance": {
                "token_budget": {
                    "run_total_tokens": 20,
                    "warn_ratio": 0.5,
                    "hard_ratio": 1.0,
                    "action": "compact_context",
                },
                "context_compaction": {
                    "min_estimated_tokens_saved": 0,
                    "oversized_message_tokens": 50,
                },
            }
        }
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        event_types = [event.event_type.value for event in events]
        compaction_event = next(event for event in events if event.event_type.value == "context.compaction.completed")

        self.assertEqual(result.status.value, "completed")
        self.assertIn("token.budget.exceeded", event_types)
        self.assertIn("context.compaction.started", event_types)
        self.assertEqual(compaction_event.payload["reason"], "budget_exceeded")
        self.assertTrue(compaction_event.payload["record"]["compacted"])

    async def test_native_runtime_compacts_context_before_oversized_model_call(self):
        self.model_registry.register(
            "fake",
            lambda profile, env: FakeModelClient(profile, env, scenario="large_tool_compaction"),
        )
        self.engine.set_context_compactor(RuntimeContextCompactor())
        self.profile = ModelProfileDefinition(
            id="profile-context-compaction",
            name="Context Compaction Profile",
            provider="fake",
            model="fake-model",
            max_tokens=10,
            context_window=1000,
        )
        await self.runtime_registry.register_model_profile(self.profile)
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
        workflow = self._workflow(tool=tool, max_iterations=3)
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        event_types = [event.event_type.value for event in events]
        updated = await self.execution_store.get_execution(execution.id)
        state = self.engine._states[execution.id]

        self.assertEqual(result.status.value, "completed")
        self.assertIn("context.compaction.started", event_types)
        self.assertIn("context.compaction.completed", event_types)
        compaction_event = next(event for event in events if event.event_type.value == "context.compaction.completed")
        self.assertTrue(compaction_event.payload["record"]["compacted"])
        self.assertGreater(compaction_event.payload["record"]["estimated_tokens_saved"], 0)
        governance = updated.metadata["runtime_governance"]
        self.assertTrue(governance["context_compaction"]["last"]["compacted"])
        self.assertGreater(governance["context_compaction"]["estimated_tokens_saved"], 0)
        self.assertTrue(state.context_compaction["last"]["compacted"])
        self.assertEqual(state.context_compaction["last"]["reason"], "context_health_threshold")
        self.assertGreater(state.context_compaction["estimated_tokens_saved"], 0)
        self.assertTrue(
            any(
                message.name == "runtime_context_compaction"
                and "Runtime Context Compaction Summary" in str(message.content)
                and "context_compacted=true" in str(message.content)
                for message in FakeModelClient.last_messages
            )
        )

    async def test_native_runtime_compacts_and_retries_after_context_length_error(self):
        self.model_registry.register(
            "fake",
            lambda profile, env: FakeModelClient(profile, env, scenario="context_length_retry"),
        )
        self.engine.set_context_compactor(RuntimeContextCompactor())
        self.profile = ModelProfileDefinition(
            id="profile-context-length-retry",
            name="Context Length Retry Profile",
            provider="fake",
            model="fake-model",
            max_tokens=10,
            context_window=10000,
        )
        await self.runtime_registry.register_model_profile(self.profile)
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
        workflow = self._workflow(tool=tool, max_iterations=3)
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(
            workflow.id,
            {"prompt": "retry on context length"},
            {"source": "test"},
        )

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        request_events = [
            event for event in events
            if event.event_type.value == "llm.request.created"
        ]
        compaction_event = next(
            event for event in events
            if (
                event.event_type.value == "context.compaction.completed"
                and event.payload.get("reason") == "context_length_error"
            )
        )

        self.assertEqual(result.status.value, "completed")
        self.assertEqual(result.output_payload["final_output"], "Final answer after context-length compaction")
        self.assertGreaterEqual(len(request_events), 3)
        self.assertTrue(request_events[-1].payload["retry_after_compaction"])
        self.assertEqual(compaction_event.payload["record"]["reason"], "context_length_error")
        self.assertEqual(
            compaction_event.payload["record"]["metadata"]["model_error"],
            "maximum context length exceeded for this model",
        )
        self.assertTrue(
            any(
                message.name == "runtime_context_compaction"
                and "context_compacted=true" in str(message.content)
                and "compaction_reason=context_length_error" in str(message.content)
                for message in FakeModelClient.last_messages
            )
        )

    async def test_native_runtime_compaction_retains_task_instructions_and_guardrails(self):
        self.model_registry.register(
            "fake",
            lambda profile, env: FakeModelClient(profile, env, scenario="large_tool_compaction"),
        )
        self.engine.set_context_compactor(RuntimeContextCompactor())
        self.profile = ModelProfileDefinition(
            id="profile-guardrail-compaction",
            name="Guardrail Compaction Profile",
            provider="fake",
            model="fake-model",
            max_tokens=10,
            context_window=1000,
        )
        await self.runtime_registry.register_model_profile(self.profile)
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
        guardrail = "CRITICAL GUARDRAIL: never bypass approval policy."
        task_instruction = "TASK INSTRUCTION: preserve audit trace references."
        agent = AgentDefinition(
            id="agent-guardrail",
            name="Guardrail Agent",
            instructions=guardrail,
            model_profile_id=self.profile.id,
            tool_ids=[tool.id],
            framework_hints=FrameworkHints(adapter_config={"max_iterations": 3}),
        )
        task = TaskDefinition(
            id="task-guardrail",
            name="Guardrail Task",
            description="Exercise guarded compaction.",
            instructions=task_instruction,
            expected_output="A guarded final answer.",
            agent_id=agent.id,
            tool_ids=[tool.id],
        )
        node = WorkflowNodeDefinition(
            id="node-guardrail",
            name="Guardrail Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-guardrail",
            name="Guardrail Workflow",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[tool],
            default_runtime_adapter_id="native",
        )
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await self.runtime_registry.start_execution(execution.id)
        raw_system_messages = [
            message for message in FakeModelClient.last_messages
            if message.role == "system" and message.name != "runtime_context_compaction"
        ]
        compaction_messages = [
            message for message in FakeModelClient.last_messages
            if message.name == "runtime_context_compaction"
        ]

        self.assertEqual(result.status.value, "completed")
        self.assertTrue(any(guardrail in str(message.content) for message in raw_system_messages))
        self.assertTrue(any(task_instruction in str(message.content) for message in raw_system_messages))
        self.assertTrue(any(guardrail in str(message.content) for message in compaction_messages))
        self.assertTrue(any(task_instruction in str(message.content) for message in compaction_messages))

    async def test_native_runtime_persists_compaction_context_pack_and_event_order(self):
        context = create_test_api_context()
        context.llm_provider_registry.register(
            "fake",
            lambda profile, env: FakeModelClient(profile, env, scenario="large_tool_compaction"),
        )
        profile = ModelProfileDefinition(
            id="profile-context-pack-compaction",
            name="Context Pack Compaction Profile",
            provider="fake",
            model="fake-model",
            supports_tools=True,
            max_tokens=10,
            context_window=1000,
        )
        await context.runtime_registry.register_model_profile(profile)
        tool = ToolDefinition(
            id="tool-context-pack-echo",
            name="Echo Tool",
            description="Echoes text",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(target="tests.native_test_tools", callable_name="echo_tool"),
            security=SecuritySettings(),
            mcp_exposure=MCPExposureSettings(),
        )
        agent = AgentDefinition(
            id="agent-context-pack-compaction",
            name="Context Pack Agent",
            instructions="Preserve compaction traceability.",
            model_profile_id=profile.id,
            tool_ids=[tool.id],
            framework_hints=FrameworkHints(adapter_config={"max_iterations": 3}),
        )
        task = TaskDefinition(
            id="task-context-pack-compaction",
            name="Context Pack Task",
            description="Run until context compaction is required.",
            agent_id=agent.id,
            tool_ids=[tool.id],
        )
        node = WorkflowNodeDefinition(
            id="node-context-pack-compaction",
            name="Context Pack Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-context-pack-compaction",
            name="Context Pack Workflow",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[tool],
            default_runtime_adapter_id="native",
            metadata={
                "runtime_governance": {
                    "context_compaction": {
                        "persist_context_pack": True,
                    }
                }
            },
        )
        await context.runtime_registry.register_workflow(workflow)
        execution = await context.runtime_registry.create_execution(
            workflow.id,
            {"prompt": "persist context pack"},
            {"source": "test"},
        )
        graph_context_calls = []

        async def fake_compaction_graph_context_retriever(
                workflow_arg,
                task_arg,
                agent_arg,
                execution_arg,
                state_arg,
                record_arg,
        ):
            graph_context_calls.append(
                {
                    "workflow_id": workflow_arg.id,
                    "task_id": task_arg.id,
                    "agent_id": agent_arg.id,
                    "execution_id": execution_arg.id,
                    "memory_id": record_arg.memory_id,
                    "compacted": record_arg.compacted,
                    "state_execution_id": state_arg.execution_id,
                }
            )
            return {
                "trigger": "context_compaction",
                "reason": "handoff_context_after_compaction",
                "intent": "handoff",
                "budget": "brief",
                "anchor_type": "run",
                "anchor_id": execution_arg.id,
                "context_pack_id": record_arg.memory_id,
                "graph_context_metadata_attached": True,
                "context": {
                    "status": "ok",
                    "summary": "Compaction handoff context",
                    "query_meta": {
                        "intent": "handoff",
                        "budget": "brief",
                        "anchor_type": "run",
                        "anchor_id": execution_arg.id,
                        "node_count": 2,
                        "edge_count": 1,
                    },
                    "decisions": [{"id": "decision-1"}],
                    "constraints": [{"id": "constraint-1"}],
                    "next_actions": [{"id": "next-action-1"}],
                },
            }

        context.execution_engine.set_context_compaction_graph_context_retriever(
            fake_compaction_graph_context_retriever
        )

        result = await context.runtime_registry.start_execution(execution.id)
        events = await context.execution_store.list_events(execution.id)
        event_types = [event.event_type.value for event in events]
        started_index = event_types.index("context.compaction.started")
        completed_index = event_types.index("context.compaction.completed")
        started_event = events[started_index]
        completed_event = events[completed_index]
        later_llm_request_index = next(
            index
            for index, event_type in enumerate(event_types[completed_index + 1:], start=completed_index + 1)
            if event_type == "llm.request.created"
        )
        context_packs = await context.memory_repo.query(
            workflow_id=workflow.id,
            source="runtime_context_compaction",
            source_execution_id=execution.id,
            memory_types=[MemoryType.CONTEXT_PACK.value],
        )

        self.assertEqual(result.status.value, "completed")
        self.assertLess(started_index, completed_index)
        self.assertLess(completed_index, later_llm_request_index)
        self.assertEqual(len(context_packs), 1)
        context_pack = context_packs[0]
        source_start = context_pack.metadata["source_event_start_sequence"]
        source_end = context_pack.metadata["source_event_end_sequence"]
        self.assertEqual(context_pack.agent_id, agent.id)
        self.assertEqual(context_pack.source_execution_id, execution.id)
        self.assertIn("Runtime Context Compaction Summary", context_pack.content)
        self.assertEqual(context_pack.metadata["task_id"], task.id)
        self.assertEqual(context_pack.metadata["agent_id"], agent.id)
        self.assertEqual(context_pack.metadata["execution_id"], execution.id)
        self.assertEqual(context_pack.metadata["compacted"], True)
        self.assertEqual(context_pack.metadata["compaction_reason"], "context_health_threshold")
        self.assertGreater(context_pack.metadata["estimated_tokens_saved"], 0)
        self.assertIsNotNone(context_pack.metadata["source_model_request_id"])
        self.assertEqual(source_start, 1)
        self.assertLessEqual(source_end, started_event.sequence - 1)
        self.assertEqual(
            completed_event.payload["record"]["metadata"]["source_event_start_sequence"],
            source_start,
        )
        self.assertEqual(
            completed_event.payload["record"]["metadata"]["source_event_end_sequence"],
            source_end,
        )
        self.assertTrue(
            any(
                message.name == "runtime_context_compaction"
                and f"context_pack_memory_id={context_pack.id}" in str(message.content)
                for message in FakeModelClient.last_messages
            )
        )
        persisted_state = context.execution_engine._states[execution.id]
        self.assertEqual(persisted_state.context_compaction["last"]["memory_id"], context_pack.id)
        self.assertEqual(persisted_state.compacted_context_packs[0]["memory_id"], context_pack.id)
        self.assertEqual(graph_context_calls[0]["memory_id"], context_pack.id)
        self.assertTrue(graph_context_calls[0]["compacted"])
        self.assertTrue(
            any(
                event.event_type == ExecutionEventType.AGENT_MESSAGE_CREATED
                and event.payload.get("source") == "runtime_graph_context"
                and event.payload.get("trigger") == "context_compaction"
                and event.payload.get("intent") == "handoff"
                and event.payload.get("context_pack_id") == context_pack.id
                and event.payload.get("graph_context_metadata_attached") is True
                for event in events
            )
        )
        self.assertTrue(
            any(
                entry.get("trigger") == "context_compaction"
                and entry.get("context_pack_id") == context_pack.id
                for entry in persisted_state.graph_context_entries
            )
        )

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

    async def test_native_runtime_retrieves_graph_context_after_execution_failure(self):
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

        async def fake_failure_graph_context_retriever(workflow, execution, state, error, failure_event_id):
            self.assertEqual(workflow.id, "workflow-1")
            self.assertEqual(execution.id, state.execution_id)
            self.assertTrue(error)
            self.assertIsNotNone(failure_event_id)
            return {
                "trigger": "execution_failed",
                "reason": "root_cause_context_after_execution_failure",
                "intent": "root_cause",
                "budget": "balanced",
                "anchor_type": "run",
                "anchor_id": execution.id,
                "failure_event_id": failure_event_id,
                "failed_events": [{"id": failure_event_id, "event_type": "execution.failed"}],
                "tool_calls": [{"id": "tool-call-event", "event_type": "tool.call.failed"}],
                "artifacts": [{"id": "artifact-1", "artifact_type": "log"}],
                "model_requests": [{"id": "model-request-1", "event_type": "llm.request.created"}],
                "prior_attempts": [{"id": "prior-run", "type": "Run"}],
                "context": {
                    "status": "ok",
                    "query_meta": {
                        "intent": "root_cause",
                        "budget": "balanced",
                        "anchor_type": "run",
                        "anchor_id": execution.id,
                        "node_count": 4,
                        "edge_count": 3,
                    },
                },
            }

        self.engine.set_execution_failure_graph_context_retriever(fake_failure_graph_context_retriever)
        workflow = self._workflow(tool=tool)
        await self.runtime_registry.register_workflow(workflow)
        execution = await self.runtime_registry.create_execution(workflow.id, {}, {"source": "test"})
        self.engine._last_execution_id = execution.id

        result = await self.runtime_registry.start_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        event_types = [event.event_type for event in events]
        graph_event = next(
            event
            for event in events
            if event.event_type == ExecutionEventType.AGENT_MESSAGE_CREATED
            and event.payload.get("source") == "runtime_graph_context"
            and event.payload.get("trigger") == "execution_failed"
            and event.payload.get("status") == "ok"
        )

        self.assertEqual(result.status.value, "failed")
        self.assertLess(event_types.index(ExecutionEventType.EXECUTION_FAILED), events.index(graph_event))
        self.assertEqual(graph_event.payload["intent"], "root_cause")
        self.assertEqual(graph_event.payload["budget"], "balanced")
        self.assertEqual(graph_event.payload["anchor_type"], "run")
        self.assertEqual(graph_event.payload["anchor_id"], execution.id)
        self.assertEqual(graph_event.payload["failed_event_count"], 1)
        self.assertEqual(graph_event.payload["tool_call_count"], 1)
        self.assertEqual(graph_event.payload["artifact_count"], 1)
        self.assertEqual(graph_event.payload["model_request_count"], 1)
        self.assertEqual(graph_event.payload["prior_attempt_count"], 1)

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

    async def test_running_execution_cancellation_is_cooperative(self):
        self.model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env, scenario="no_tool"))
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

        task_started = asyncio.Event()

        async def slow_execute_task(*args, **kwargs):
            task_started.set()
            await asyncio.sleep(0.2)
            return "Too late", []

        self.engine.agent_executor.execute_task = slow_execute_task
        run_task = asyncio.create_task(self.runtime_registry.start_execution(execution.id))
        await asyncio.wait_for(task_started.wait(), timeout=1)

        cancelling = await self.runtime_registry.cancel_execution(execution.id)
        during_cancel = await self.execution_store.get_execution(execution.id)
        cancelling_status = cancelling.status
        during_cancel_status = during_cancel.status if during_cancel is not None else None
        cancellation_mode = during_cancel.metadata["cancellation"]["mode"] if during_cancel is not None else None
        result = await run_task
        events = await self.execution_store.list_events(execution.id)

        self.assertEqual(cancelling_status, ExecutionStatus.CANCELLING)
        self.assertIsNotNone(during_cancel)
        assert during_cancel is not None
        self.assertEqual(during_cancel_status, ExecutionStatus.CANCELLING)
        self.assertEqual(cancellation_mode, "cooperative")
        self.assertEqual(result.status, ExecutionStatus.CANCELLED)
        self.assertEqual(result.output_payload["checkpoint"]["completed_node_ids"], [])
        self.assertEqual(
            [event.event_type for event in events].count(ExecutionEventType.EXECUTION_CANCELLED),
            1,
        )

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
