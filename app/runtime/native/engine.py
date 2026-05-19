from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional
from uuid import uuid4

from app.core.time import utc_now
from app.domain import (
    AgentDefinition,
    Execution,
    ExecutionEventType,
    ExecutionStatus,
    ModelProfileDefinition,
    RuntimeAdapterType,
    TaskDefinition,
    WorkflowDefinition,
)
from app.llm.registry import ModelProviderRegistry
from app.observability.metrics import collect_system_metrics
from app.runtime.native.agent_executor import AgentExecutor
from app.runtime.native.agent_executor import MemoryPromptBuilder
from app.runtime.native.approvals import ApprovalManager
from app.runtime.native.errors import (
    ExecutionCancelledError,
    ExecutionNotFoundError,
    ExecutionPausedError,
    WorkflowNotFoundError,
)
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.planner import LinearWorkflowPlanner
from app.runtime.native.state import (
    ExecutionStore,
    InMemoryModelProfileRepository,
    InMemoryWorkflowRepository,
    ModelProfileRepository,
    NativeExecutionState,
    WorkflowRepository,
)
from app.runtime.execution_lifecycle import build_execution_lifecycle_metadata
from app.runtime.native.tool_executor import ToolExecutor


@dataclass
class ExecutionStateSnapshot:
    execution: Execution
    state: Optional[NativeExecutionState]


class ExecutionEngine:
    def __init__(
            self,
            *,
            workflow_repository: WorkflowRepository,
            model_profile_repository: ModelProfileRepository,
            execution_store: ExecutionStore,
            model_provider_registry: ModelProviderRegistry,
            approval_manager: Optional[ApprovalManager] = None,
            execution_completion_handler: Optional[
                Callable[[Execution, WorkflowDefinition], Awaitable[None]]
            ] = None,
            memory_prompt_builder: MemoryPromptBuilder | None = None,
    ):
        self.workflow_repository = workflow_repository
        self.model_profile_repository = model_profile_repository
        self.execution_store = execution_store
        self.model_provider_registry = model_provider_registry
        self.approval_manager = approval_manager or ApprovalManager()
        self.execution_completion_handler = execution_completion_handler
        self.memory_prompt_builder = memory_prompt_builder
        self.planner = LinearWorkflowPlanner()
        self.emitter = ExecutionEventEmitter(execution_store)
        self.agent_executor = AgentExecutor(
            ToolExecutor(self.approval_manager),
            memory_prompt_builder=memory_prompt_builder,
        )
        self._states: Dict[str, NativeExecutionState] = {}

    @classmethod
    def create_in_memory(cls, model_provider_registry: ModelProviderRegistry) -> "ExecutionEngine":
        from app.runtime.native.state import InMemoryExecutionStore

        return cls(
            workflow_repository=InMemoryWorkflowRepository(),
            model_profile_repository=InMemoryModelProfileRepository(),
            execution_store=InMemoryExecutionStore(),
            model_provider_registry=model_provider_registry,
        )

    async def register_workflow(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        return await self.workflow_repository.save_workflow(workflow)

    async def register_model_profile(self, profile: ModelProfileDefinition) -> ModelProfileDefinition:
        return await self.model_profile_repository.save_profile(profile)

    def set_memory_prompt_builder(self, memory_prompt_builder: MemoryPromptBuilder | None) -> None:
        self.memory_prompt_builder = memory_prompt_builder
        self.agent_executor.memory_prompt_builder = memory_prompt_builder

    async def prepare_execution(self, execution: Execution) -> Execution:
        state = self._states.setdefault(
            execution.id,
            NativeExecutionState(execution_id=execution.id, workflow_id=execution.workflow_id),
        )
        await self.execution_store.save_execution(execution)
        await self.emitter.emit(
            state,
            ExecutionEventType.EXECUTION_CREATED,
            payload={"workflow_id": execution.workflow_id, "trigger": execution.metadata.get("trigger", {})},
        )
        return execution

    async def create_execution(self, workflow_id: str, input: Dict[str, Any], trigger: Dict[str, Any]) -> Execution:
        workflow = await self.workflow_repository.get_workflow(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' was not found")

        runtime_adapter_id = workflow.default_runtime_adapter_id or RuntimeAdapterType.NATIVE.value
        execution_lifecycle = build_execution_lifecycle_metadata(
            trigger=trigger,
            workflow_metadata=workflow.metadata,
        )
        execution = Execution(
            workflow_id=workflow_id,
            runtime_adapter_id=runtime_adapter_id,
            status=ExecutionStatus.CREATED,
            trigger_type=trigger.get("type", "manual"),
            trigger_payload=trigger,
            input_payload=input,
            created_by=trigger.get("created_by"),
            metadata={
                "trigger": trigger,
                "trace_id": str(uuid4()),
                "execution_lifecycle": execution_lifecycle,
            },
        )
        await self.prepare_execution(execution)
        return execution

    async def start_execution(self, execution_id: str) -> Execution:
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        workflow = await self.workflow_repository.get_workflow(execution.workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{execution.workflow_id}' was not found")

        state = self._states.setdefault(
            execution_id,
            NativeExecutionState(execution_id=execution_id, workflow_id=workflow.id),
        )
        state.trace_id = execution.metadata.get("trace_id", state.trace_id)
        state.paused = False
        state.cancelled = False
        await self._hydrate_state_position(state)

        execution.status = ExecutionStatus.RUNNING
        if execution.started_at is None:
            execution.started_at = utc_now()
        if execution.worker_id:
            execution.last_heartbeat_at = utc_now()
        await self.execution_store.update_execution(execution)
        await self.emitter.emit(
            state,
            ExecutionEventType.EXECUTION_STARTED,
            payload={"workflow_id": execution.workflow_id},
        )

        try:
            ordered_nodes = self.planner.order_nodes(workflow)
            for node in ordered_nodes:
                if execution.worker_id:
                    await self.execution_store.heartbeat(execution.id, execution.worker_id)
                if node.node_type != "task":
                    continue
                state.current_node_id = node.id
                task = self._resolve_task(workflow, node.task_id)
                agent = self._resolve_agent(workflow, node.agent_id or task.agent_id)
                state.current_agent_id = agent.id
                state.current_task_id = task.id
                profile = await self._resolve_profile(agent)

                await self.emitter.emit(
                    state,
                    ExecutionEventType.TASK_STARTED,
                    actor=agent.name,
                    payload={"task_id": task.id, "task_name": task.name, "node_id": node.id},
                    agent_id=agent.id,
                    task_id=task.id,
                )

                model_client = self.model_provider_registry.resolve(profile)
                output, _messages = await self.agent_executor.execute_task(
                    workflow,
                    task,
                    agent,
                    profile,
                    model_client,
                    state,
                    self.emitter,
                    execution,
                    execution.input_payload,
                )
                state.node_outputs[node.id] = output

            execution.status = ExecutionStatus.COMPLETED
            execution.output_payload = {
                "node_outputs": state.node_outputs,
                "final_output": next(reversed(state.node_outputs.values()), None) if state.node_outputs else None,
            }
            execution.completed_at = utc_now()
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(
                state,
                ExecutionEventType.EXECUTION_COMPLETED,
                payload={"output": execution.output_payload},
                metrics={
                    "execution_duration_ms": ((execution.completed_at - (
                                execution.started_at or execution.created_at)).total_seconds() * 1000),
                    **collect_system_metrics(),
                },
            )
            await self._maybe_handle_execution_completion(execution, workflow)
            return execution
        except ExecutionPausedError:
            execution.status = ExecutionStatus.PAUSED
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(state, ExecutionEventType.EXECUTION_PAUSED, payload={"execution_id": execution.id})
            return execution
        except ExecutionCancelledError:
            execution.status = ExecutionStatus.CANCELLED
            execution.completed_at = utc_now()
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(state, ExecutionEventType.EXECUTION_CANCELLED,
                                    payload={"execution_id": execution.id})
            return execution
        except Exception as exc:
            execution.status = ExecutionStatus.FAILED
            execution.error = str(exc)
            execution.completed_at = utc_now()
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(
                state,
                ExecutionEventType.EXECUTION_FAILED,
                payload={"error": str(exc)},
                metrics=collect_system_metrics(),
            )
            await self._maybe_handle_execution_completion(execution, workflow)
            return execution

    async def pause_execution(self, execution_id: str) -> Execution:
        execution, state = await self._require_execution_and_state(execution_id)
        state.paused = True
        execution.status = ExecutionStatus.PAUSED
        await self.execution_store.update_execution(execution)
        await self.emitter.emit(state, ExecutionEventType.EXECUTION_PAUSED, payload={"execution_id": execution_id})
        return execution

    async def resume_execution(self, execution_id: str) -> Execution:
        execution, state = await self._require_execution_and_state(execution_id)
        state.paused = False
        execution.status = ExecutionStatus.QUEUED
        await self.execution_store.update_execution(execution)
        await self.emitter.emit(state, ExecutionEventType.EXECUTION_RESUMED, payload={"execution_id": execution_id})
        return execution

    async def cancel_execution(self, execution_id: str) -> Execution:
        execution, state = await self._require_execution_and_state(execution_id)
        if execution.status == ExecutionStatus.CANCELLED:
            state.cancelled = True
            return execution
        if execution.status in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED}:
            return execution
        state.cancelled = True
        execution.status = ExecutionStatus.CANCELLED
        execution.completed_at = utc_now()
        await self.execution_store.update_execution(execution)
        await self.emitter.emit(state, ExecutionEventType.EXECUTION_CANCELLED, payload={"execution_id": execution_id})
        return execution

    async def get_execution_state(self, execution_id: str) -> ExecutionStateSnapshot:
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        return ExecutionStateSnapshot(execution=execution, state=self._states.get(execution_id))

    async def _maybe_handle_execution_completion(
            self,
            execution: Execution,
            workflow: WorkflowDefinition,
    ) -> None:
        if self.execution_completion_handler is None:
            return
        try:
            await self.execution_completion_handler(execution, workflow)
        except Exception:
            return

    async def list_events(self, execution_id: str):
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        return await self.execution_store.list_events(execution_id)

    async def list_artifacts(self, execution_id: str):
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        return await self.execution_store.list_artifacts(execution_id)

    def _resolve_task(self, workflow: WorkflowDefinition, task_id: Optional[str]) -> TaskDefinition:
        if not task_id:
            raise WorkflowNotFoundError("Workflow node is missing a task reference")
        for task in workflow.task_definitions:
            if task.id == task_id:
                return task
        raise WorkflowNotFoundError(f"Task '{task_id}' was not found in workflow '{workflow.id}'")

    def _resolve_agent(self, workflow: WorkflowDefinition, agent_id: Optional[str]) -> AgentDefinition:
        if not agent_id:
            raise WorkflowNotFoundError("Task is missing an agent reference")
        for agent in workflow.agent_definitions:
            if agent.id == agent_id:
                return agent
        raise WorkflowNotFoundError(f"Agent '{agent_id}' was not found in workflow '{workflow.id}'")

    async def _resolve_profile(self, agent: AgentDefinition) -> ModelProfileDefinition:
        if not agent.model_profile_id:
            raise WorkflowNotFoundError(f"Agent '{agent.id}' is missing model_profile_id")
        profile = await self.model_profile_repository.get_profile(agent.model_profile_id)
        if profile is None:
            raise WorkflowNotFoundError(f"Model profile '{agent.model_profile_id}' was not found")
        return profile

    async def _require_execution_and_state(self, execution_id: str):
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        state = self._states.setdefault(
            execution_id,
            NativeExecutionState(execution_id=execution_id, workflow_id=execution.workflow_id),
        )
        await self._hydrate_state_position(state)
        return execution, state

    async def _hydrate_state_position(self, state: NativeExecutionState) -> None:
        if state.sequence != 0 or state.last_event_id is not None:
            return
        prior_events = await self.execution_store.list_events(state.execution_id)
        if not prior_events:
            return
        last_event = prior_events[-1]
        state.sequence = last_event.sequence
        state.last_event_id = last_event.id
        state.trace_id = last_event.trace_id or state.trace_id
