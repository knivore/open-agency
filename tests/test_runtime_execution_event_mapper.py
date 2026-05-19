from __future__ import annotations

import unittest

from app.domain import (
    AgentDefinition,
    Execution,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    ModelProfileDefinition,
    TaskDefinition,
    WorkflowDefinition,
    WorkflowNodeDefinition,
)
from app.llm.base import ModelResponse
from app.llm.registry import ModelProviderRegistry
from app.runtime.native.engine import ExecutionEngine
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import (
    InMemoryExecutionStore,
    InMemoryModelProfileRepository,
    InMemoryWorkflowRepository,
    NativeExecutionState,
)
from app.runtime.streaming import (
    RuntimeEventBus,
    RuntimeEventType,
    map_execution_event_to_runtime_events,
    set_default_runtime_event_bus,
)


class _RuntimeStreamFakeModelClient:
    provider_key = "fake"

    def __init__(self, profile: ModelProfileDefinition, env):
        self.profile = profile

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content="Final visible answer", provider="fake", model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content={"ok": True}, provider="fake", model=self.profile.model)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "unused"

    def count_tokens(self, messages, **kwargs):
        return 1

    def health_check(self):
        return {"ok": True}


class RuntimeExecutionEventMapperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        set_default_runtime_event_bus(None)

    def test_task_started_maps_to_agent_status_task_started_and_progress(self):
        event = ExecutionEvent(
            id="event-task-started",
            execution_id="execution-1",
            workflow_id="workflow-1",
            agent_id="agent-1",
            task_id="task-1",
            actor="Agent One",
            event_type=ExecutionEventType.TASK_STARTED,
            payload={"task_id": "task-1", "task_name": "Task One"},
        )

        runtime_events = map_execution_event_to_runtime_events(event)
        runtime_types = [item.type for item in runtime_events]

        self.assertIn(RuntimeEventType.AGENT_STATUS_CHANGED, runtime_types)
        self.assertIn(RuntimeEventType.TASK_STARTED, runtime_types)
        self.assertIn(RuntimeEventType.TASK_PROGRESS, runtime_types)
        self.assertEqual(runtime_events[0].actor.id, "agent-1")
        self.assertEqual(runtime_events[1].task.title, "Task One")

    def test_runtime_mapper_covers_c4_mvp_event_types(self):
        execution_events = [
            ExecutionEvent(
                id="event-task-started",
                execution_id="execution-1",
                workflow_id="workflow-1",
                agent_id="agent-1",
                task_id="task-1",
                actor="Agent One",
                event_type=ExecutionEventType.TASK_STARTED,
                payload={"task_id": "task-1", "task_name": "Task One"},
            ),
            ExecutionEvent(
                id="event-llm-response",
                execution_id="execution-1",
                workflow_id="workflow-1",
                agent_id="agent-1",
                task_id="task-1",
                actor="Agent One",
                event_type=ExecutionEventType.LLM_RESPONSE_CREATED,
                payload={"iteration": 1},
            ),
            ExecutionEvent(
                id="event-execution-completed",
                execution_id="execution-1",
                workflow_id="workflow-1",
                agent_id="agent-1",
                task_id="task-1",
                actor="Agent One",
                event_type=ExecutionEventType.EXECUTION_COMPLETED,
            ),
        ]

        runtime_types = {
            runtime_event.type
            for execution_event in execution_events
            for runtime_event in map_execution_event_to_runtime_events(execution_event)
        }

        self.assertTrue(
            {
                RuntimeEventType.AGENT_STATUS_CHANGED,
                RuntimeEventType.TASK_STARTED,
                RuntimeEventType.TASK_PROGRESS,
                RuntimeEventType.TASK_COMPLETED,
                RuntimeEventType.LOG_RECEIVED,
            }.issubset(runtime_types)
        )

    async def test_execution_emitter_publishes_runtime_events_to_default_bus(self):
        bus = RuntimeEventBus()
        set_default_runtime_event_bus(bus)
        subscriber = await bus.subscribe()
        store = InMemoryExecutionStore()
        await store.save_execution(
            Execution(
                id="execution-1",
                workflow_id="workflow-1",
                runtime_adapter_id="native",
                status=ExecutionStatus.RUNNING,
                input_payload={},
            )
        )
        emitter = ExecutionEventEmitter(store)
        state = NativeExecutionState(execution_id="execution-1", workflow_id="workflow-1")
        state.current_agent_id = "agent-1"
        state.current_task_id = "task-1"

        await emitter.emit(
            state,
            ExecutionEventType.TASK_STARTED,
            actor="Agent One",
            payload={"task_id": "task-1", "task_name": "Task One"},
        )
        await emitter.emit(
            state,
            ExecutionEventType.LLM_REQUEST_CREATED,
            actor="Agent One",
            payload={"iteration": 1},
        )
        await emitter.emit(
            state,
            ExecutionEventType.EXECUTION_COMPLETED,
            actor="Agent One",
        )

        runtime_types: list[RuntimeEventType] = []
        while not subscriber.empty():
            runtime_types.append((await subscriber.get()).type)

        self.assertIn(RuntimeEventType.AGENT_STATUS_CHANGED, runtime_types)
        self.assertIn(RuntimeEventType.TASK_STARTED, runtime_types)
        self.assertIn(RuntimeEventType.TASK_PROGRESS, runtime_types)
        self.assertIn(RuntimeEventType.TASK_COMPLETED, runtime_types)
        self.assertIn(RuntimeEventType.LOG_RECEIVED, runtime_types)

    async def test_native_task_run_emits_c4_runtime_events(self):
        bus = RuntimeEventBus()
        set_default_runtime_event_bus(bus)
        subscriber = await bus.subscribe()
        model_registry = ModelProviderRegistry()
        model_registry.register("fake", lambda profile, env: _RuntimeStreamFakeModelClient(profile, env))
        engine = ExecutionEngine(
            workflow_repository=InMemoryWorkflowRepository(),
            model_profile_repository=InMemoryModelProfileRepository(),
            execution_store=InMemoryExecutionStore(),
            model_provider_registry=model_registry,
        )
        profile = ModelProfileDefinition(id="profile-1", name="Fake", provider="fake", model="fake-model")
        await engine.register_model_profile(profile)
        agent = AgentDefinition(
            id="agent-1",
            name="Agent One",
            instructions="Be concise",
            model_profile_id=profile.id,
        )
        task = TaskDefinition(
            id="task-1",
            name="Task One",
            description="Do visible runtime work",
            agent_id=agent.id,
        )
        node = WorkflowNodeDefinition(
            id="node-1",
            name="Task Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-1",
            name="Workflow One",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await engine.register_workflow(workflow)
        execution = await engine.create_execution(workflow.id, {"prompt": "hi"}, {"source": "test"})

        result = await engine.start_execution(execution.id)
        runtime_types: list[RuntimeEventType] = []
        while not subscriber.empty():
            runtime_types.append((await subscriber.get()).type)

        self.assertEqual(result.status.value, "completed")
        self.assertTrue(
            {
                RuntimeEventType.AGENT_STATUS_CHANGED,
                RuntimeEventType.TASK_STARTED,
                RuntimeEventType.TASK_PROGRESS,
                RuntimeEventType.TASK_COMPLETED,
                RuntimeEventType.LOG_RECEIVED,
            }.issubset(runtime_types)
        )


if __name__ == "__main__":
    unittest.main()
