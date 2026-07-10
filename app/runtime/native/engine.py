"""Native workflow execution engine.

The native engine executes Agency's canonical workflow graph directly: it
resolves tasks and agents, builds tool/LLM execution context, manages approval
pauses, records events and artifacts, and updates execution status without
depending on an external orchestration framework.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional
from uuid import uuid4

from app.core.time import utc_now
from app.domain import (
    AgentDefinition,
    EdgeType,
    Execution,
    ExecutionEventType,
    ExecutionStatus,
    ModelProfileDefinition,
    RuntimeAdapterType,
    TaskDefinition,
    WorkflowDefinition,
    WorkflowEdgeDefinition,
    WorkflowNodeDefinition,
)
from app.llm.registry import ModelProviderRegistry
from app.observability.metrics import collect_system_metrics
from app.runtime.execution_lifecycle import build_execution_lifecycle_metadata
from app.runtime.native.agent_executor import AgentExecutor
from app.runtime.native.agent_executor import (
    ContextCompactionGraphContextRetriever,
    ContextCompactor,
    MemoryPromptBuilder,
    ProposalToolGraphContextRetriever,
)
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
    record_graph_context_working_set_entry,
)
from app.runtime.native.tool_executor import ToolExecutor

GraphContextRetriever = Callable[
    [
        WorkflowDefinition,
        TaskDefinition,
        AgentDefinition,
        Execution,
        Dict[str, Any],
        NativeExecutionState,
    ],
    Awaitable[dict[str, Any] | None],
]
ExecutionFailureGraphContextRetriever = Callable[
    [
        WorkflowDefinition,
        Execution,
        NativeExecutionState,
        str,
        str | None,
    ],
    Awaitable[dict[str, Any] | None],
]

TASK_RUNTIME_OVERRIDES_METADATA_KEY = "task_runtime_overrides"
TASK_APPROVAL_TOOL_ID_PREFIX = "task:"
WORKFLOW_APPROVAL_TOOL_ID_PREFIX = "workflow:"
TASK_APPROVAL_POLICIES = {"none", "required", "on_failure"}
TASK_RETRY_METADATA_KEY = "task_retry"
WORKFLOW_APPROVAL_MODES = {"task_policy", "before_run", "all_tasks"}


@dataclass
class ExecutionStateSnapshot:
    execution: Execution
    state: Optional[NativeExecutionState]


def _conversation_id_from_execution(execution: Execution) -> str | None:
    for container in (execution.trigger_payload, execution.input_payload, execution.metadata):
        if not isinstance(container, dict):
            continue
        for key in ("conversation_id", "conversationId", "thread_id", "threadId"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _task_runtime_overrides(task: TaskDefinition) -> dict[str, Any]:
    metadata_overrides = task.metadata.get(TASK_RUNTIME_OVERRIDES_METADATA_KEY)
    raw_overrides = dict(metadata_overrides) if isinstance(metadata_overrides, dict) else {}
    first_class_overrides = {
        "timeout_seconds": task.timeout_seconds,
        "max_retries": task.max_retries,
        "model_profile_id": task.model_profile_id,
        "max_tokens": task.max_tokens,
        "approval_policy": task.approval_policy,
    }
    raw_overrides.update(
        {key: value for key, value in first_class_overrides.items() if value is not None}
    )

    overrides: dict[str, Any] = {}
    timeout_seconds = _positive_int(raw_overrides.get("timeout_seconds"))
    max_retries = _non_negative_int(raw_overrides.get("max_retries"))
    max_tokens = _positive_int(raw_overrides.get("max_tokens"))
    model_profile_id = raw_overrides.get("model_profile_id")
    approval_policy = raw_overrides.get("approval_policy")

    if timeout_seconds is not None:
        overrides["timeout_seconds"] = timeout_seconds
    if max_retries is not None:
        overrides["max_retries"] = max_retries
    if isinstance(model_profile_id, str) and model_profile_id.strip():
        overrides["model_profile_id"] = model_profile_id.strip()
    if max_tokens is not None:
        overrides["max_tokens"] = max_tokens
    if isinstance(approval_policy, str) and approval_policy in TASK_APPROVAL_POLICIES:
        overrides["approval_policy"] = approval_policy
    return overrides


def _task_approval_tool_id(task: TaskDefinition) -> str:
    return f"{TASK_APPROVAL_TOOL_ID_PREFIX}{task.id}"


def _task_retry_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    retry = (metadata or {}).get(TASK_RETRY_METADATA_KEY)
    return retry if isinstance(retry, dict) else {}


def _workflow_runtime_policy(workflow: WorkflowDefinition) -> dict[str, Any]:
    metadata = workflow.metadata if isinstance(workflow.metadata, dict) else {}
    runtime_governance = metadata.get("runtime_governance")
    runtime_governance = runtime_governance if isinstance(runtime_governance, dict) else {}
    metadata_policy = runtime_governance.get("execution_policy")
    metadata_policy = metadata_policy if isinstance(metadata_policy, dict) else {}
    policy: dict[str, Any] = {}

    max_runtime_seconds = workflow.max_runtime_seconds or _positive_int(
        metadata_policy.get("max_runtime_seconds")
    )
    max_retries = (
        workflow.max_retries
        if workflow.max_retries is not None
        else _non_negative_int(metadata_policy.get("max_retries"))
    )
    concurrency_limit = workflow.concurrency_limit or _positive_int(
        metadata_policy.get("concurrency_limit")
    )
    approval_mode = workflow.approval_mode or metadata_policy.get("approval_mode")

    if max_runtime_seconds is not None:
        policy["max_runtime_seconds"] = max_runtime_seconds
    if max_retries is not None:
        policy["max_retries"] = max_retries
    if concurrency_limit is not None:
        policy["concurrency_limit"] = concurrency_limit
    if isinstance(approval_mode, str) and approval_mode in WORKFLOW_APPROVAL_MODES:
        policy["approval_mode"] = approval_mode
    return policy


def _execution_checkpoint_payload(state: NativeExecutionState) -> dict[str, Any]:
    node_outputs = dict(state.node_outputs)
    return {
        "node_outputs": node_outputs,
        "final_output": next(reversed(node_outputs.values()), None) if node_outputs else None,
        "checkpoint": {
            "current_node_id": state.current_node_id,
            "current_task_id": state.current_task_id,
            "completed_node_ids": list(node_outputs.keys()),
            "planned_node_ids": list(state.planned_node_ids),
            "terminal_node_ids": list(state.terminal_node_ids),
        },
    }


def _terminal_executable_node_ids(workflow: WorkflowDefinition, ordered_nodes: list[WorkflowNodeDefinition]) -> list[str]:
    task_node_ids = {
        node.id
        for node in ordered_nodes
        if node.node_type == "task"
    }
    outgoing: dict[str, list[str]] = {}
    for edge in workflow.edges:
        outgoing.setdefault(edge.source_node_id, []).append(edge.target_node_id)

    terminal_node_ids: list[str] = []
    for node_id in task_node_ids:
        pending = list(outgoing.get(node_id, []))
        seen: set[str] = set()
        reaches_later_task = False
        while pending:
            target_id = pending.pop()
            if target_id in seen:
                continue
            seen.add(target_id)
            if target_id in task_node_ids:
                reaches_later_task = True
                break
            pending.extend(outgoing.get(target_id, []))
        if not reaches_later_task:
            terminal_node_ids.append(node_id)
    return [node.id for node in ordered_nodes if node.id in terminal_node_ids]


def _node_outputs_from_checkpoint(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    node_outputs = payload.get("node_outputs")
    return dict(node_outputs) if isinstance(node_outputs, dict) else {}


def _checkpoint_payload_summary(payload: Any) -> dict[str, Any] | None:
    node_outputs = _node_outputs_from_checkpoint(payload)
    if not node_outputs:
        return None
    checkpoint = payload.get("checkpoint") if isinstance(payload, dict) else {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    return {
        "completed_node_ids": list(node_outputs.keys()),
        "current_node_id": checkpoint.get("current_node_id"),
        "current_task_id": checkpoint.get("current_task_id"),
        "planned_node_ids": [
            item for item in checkpoint.get("planned_node_ids", [])
            if isinstance(item, str)
        ],
        "terminal_node_ids": [
            item for item in checkpoint.get("terminal_node_ids", [])
            if isinstance(item, str)
        ],
    }


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
            context_compactor: ContextCompactor | None = None,
            graph_context_retriever: GraphContextRetriever | None = None,
            execution_failure_graph_context_retriever: ExecutionFailureGraphContextRetriever | None = None,
            context_compaction_graph_context_retriever: ContextCompactionGraphContextRetriever | None = None,
            proposal_tool_graph_context_retriever: ProposalToolGraphContextRetriever | None = None,
    ):
        self.workflow_repository = workflow_repository
        self.model_profile_repository = model_profile_repository
        self.execution_store = execution_store
        self.model_provider_registry = model_provider_registry
        self.approval_manager = approval_manager or ApprovalManager()
        self.execution_completion_handler = execution_completion_handler
        self.memory_prompt_builder = memory_prompt_builder
        self.context_compactor = context_compactor
        self.graph_context_retriever = graph_context_retriever
        self.execution_failure_graph_context_retriever = execution_failure_graph_context_retriever
        self.context_compaction_graph_context_retriever = context_compaction_graph_context_retriever
        self.proposal_tool_graph_context_retriever = proposal_tool_graph_context_retriever
        self.planner = LinearWorkflowPlanner()
        self.emitter = ExecutionEventEmitter(execution_store)
        self.agent_executor = AgentExecutor(
            ToolExecutor(self.approval_manager),
            memory_prompt_builder=memory_prompt_builder,
            context_compactor=context_compactor,
            context_compaction_graph_context_retriever=context_compaction_graph_context_retriever,
            proposal_tool_graph_context_retriever=proposal_tool_graph_context_retriever,
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

    def set_context_compactor(self, context_compactor: ContextCompactor | None) -> None:
        self.context_compactor = context_compactor
        self.agent_executor.context_compactor = context_compactor

    def set_graph_context_retriever(self, graph_context_retriever: GraphContextRetriever | None) -> None:
        self.graph_context_retriever = graph_context_retriever

    def set_execution_failure_graph_context_retriever(
            self,
            graph_context_retriever: ExecutionFailureGraphContextRetriever | None,
    ) -> None:
        self.execution_failure_graph_context_retriever = graph_context_retriever

    def set_context_compaction_graph_context_retriever(
            self,
            graph_context_retriever: ContextCompactionGraphContextRetriever | None,
    ) -> None:
        self.context_compaction_graph_context_retriever = graph_context_retriever
        self.agent_executor.context_compaction_graph_context_retriever = graph_context_retriever

    def set_proposal_tool_graph_context_retriever(
            self,
            graph_context_retriever: ProposalToolGraphContextRetriever | None,
    ) -> None:
        self.proposal_tool_graph_context_retriever = graph_context_retriever
        self.agent_executor.proposal_tool_graph_context_retriever = graph_context_retriever

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

        execution_lifecycle = build_execution_lifecycle_metadata(
            trigger=trigger,
            workflow_metadata=workflow.metadata,
        )
        execution = Execution(
            workflow_id=workflow_id,
            runtime_adapter_id=RuntimeAdapterType.NATIVE.value,
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
        workflow_policy = _workflow_runtime_policy(workflow)
        resume_checkpoint = await self._hydrate_resume_checkpoint(execution, state)
        await self.emitter.emit(
            state,
            ExecutionEventType.EXECUTION_STARTED,
            payload={
                "workflow_id": execution.workflow_id,
                "task_retry": _task_retry_metadata(execution.metadata) or None,
                "runtime_policy": workflow_policy or None,
                "resume_checkpoint": resume_checkpoint,
            },
        )

        try:
            ordered_nodes = self.planner.order_nodes(workflow)
            state.planned_node_ids = [node.id for node in ordered_nodes]
            state.terminal_node_ids = _terminal_executable_node_ids(workflow, ordered_nodes)
            retry_metadata = _task_retry_metadata(execution.metadata)
            retry_task_id = retry_metadata.get("task_id")
            retry_started = not isinstance(retry_task_id, str) or not retry_task_id
            prior_node_outputs = retry_metadata.get("prior_node_outputs")
            if isinstance(prior_node_outputs, dict):
                state.node_outputs = dict(prior_node_outputs)
            previous_task_context: tuple[WorkflowNodeDefinition, TaskDefinition, AgentDefinition] | None = None

            if workflow_policy.get("approval_mode") == "before_run":
                await self._maybe_request_workflow_approval(
                    workflow=workflow,
                    execution=execution,
                    state=state,
                    reason="Workflow approval mode requires approval before execution.",
                )

            for node in ordered_nodes:
                self._raise_if_cancelled(state)
                if execution.worker_id:
                    await self.execution_store.heartbeat(execution.id, execution.worker_id)
                if node.node_type != "task":
                    continue
                task = self._resolve_task(workflow, node.task_id)
                if not retry_started:
                    if task.id != retry_task_id:
                        continue
                    retry_started = True
                agent = self._resolve_agent(workflow, node.agent_id or task.agent_id)
                if retry_started and node.id in state.node_outputs:
                    previous_task_context = (node, task, agent)
                    continue
                state.current_node_id = node.id
                state.current_agent_id = agent.id
                state.current_task_id = task.id
                task_overrides = _task_runtime_overrides(task)
                if "max_retries" not in task_overrides and "max_retries" in workflow_policy:
                    task_overrides["max_retries"] = workflow_policy["max_retries"]
                if workflow_policy.get("approval_mode") == "all_tasks":
                    task_overrides["approval_policy"] = "required"
                profile = await self._resolve_profile(agent, task=task, task_overrides=task_overrides)
                workflow_timeout_seconds = self._workflow_remaining_seconds(execution, workflow_policy)
                handoff_edge: WorkflowEdgeDefinition | None = None
                if previous_task_context is not None:
                    previous_node, previous_task, previous_agent = previous_task_context
                    handoff_edge = self._handoff_edge_between(workflow, previous_node.id, node.id)
                    if handoff_edge is not None:
                        await self._emit_handoff_event(
                            workflow=workflow,
                            edge=handoff_edge,
                            source_node=previous_node,
                            target_node=node,
                            source_task=previous_task,
                            target_task=task,
                            source_agent=previous_agent,
                            target_agent=agent,
                            state=state,
                            status="requested",
                        )

                await self._maybe_retrieve_graph_context_before_task(
                    workflow=workflow,
                    task=task,
                    agent=agent,
                    execution=execution,
                    state=state,
                )

                await self.emitter.emit(
                    state,
                    ExecutionEventType.TASK_STARTED,
                    actor=agent.name,
                    payload={
                        "task_id": task.id,
                        "task_name": task.name,
                        "node_id": node.id,
                        "runtime_overrides": task_overrides,
                    },
                    agent_id=agent.id,
                    task_id=task.id,
                )

                await self._maybe_request_task_approval(
                    workflow=workflow,
                    task=task,
                    agent=agent,
                    execution=execution,
                    state=state,
                    approval_policy=task_overrides.get("approval_policy"),
                )

                model_client = self.model_provider_registry.resolve(profile)
                output, _messages = await self._execute_task_with_runtime_overrides(
                    workflow=workflow,
                    task=task,
                    agent=agent,
                    profile=profile,
                    model_client=model_client,
                    state=state,
                    execution=execution,
                    task_overrides=task_overrides,
                    workflow_timeout_seconds=workflow_timeout_seconds,
                )
                self._raise_if_cancelled(state)
                state.node_outputs[node.id] = output
                execution.output_payload = _execution_checkpoint_payload(state)
                await self.execution_store.update_execution(execution)
                if handoff_edge is not None and previous_task_context is not None:
                    previous_node, previous_task, previous_agent = previous_task_context
                    await self._emit_handoff_event(
                        workflow=workflow,
                        edge=handoff_edge,
                        source_node=previous_node,
                        target_node=node,
                        source_task=previous_task,
                        target_task=task,
                        source_agent=previous_agent,
                        target_agent=agent,
                        state=state,
                        status="completed",
                    )
                previous_task_context = (node, task, agent)

            if not retry_started:
                raise WorkflowNotFoundError(
                    f"Retry task '{retry_task_id}' was not found in workflow '{workflow.id}'"
                )

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
            execution.output_payload = _execution_checkpoint_payload(state)
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(state, ExecutionEventType.EXECUTION_PAUSED, payload={"execution_id": execution.id})
            return execution
        except ExecutionCancelledError:
            execution.status = ExecutionStatus.CANCELLED
            execution.output_payload = _execution_checkpoint_payload(state)
            execution.completed_at = utc_now()
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(state, ExecutionEventType.EXECUTION_CANCELLED,
                                    payload={"execution_id": execution.id})
            return execution
        except Exception as exc:
            execution.status = ExecutionStatus.FAILED
            execution.error = str(exc)
            execution.output_payload = _execution_checkpoint_payload(state)
            execution.completed_at = utc_now()
            await self.execution_store.update_execution(execution)
            failure_event = await self.emitter.emit(
                state,
                ExecutionEventType.EXECUTION_FAILED,
                payload={"error": str(exc)},
                metrics=collect_system_metrics(),
            )
            await self._maybe_retrieve_graph_context_after_execution_failure(
                workflow=workflow,
                execution=execution,
                state=state,
                error=str(exc),
                failure_event_id=failure_event.id,
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
        metadata = dict(execution.metadata or {})
        metadata["cancellation"] = {
            "requested_at": utc_now().isoformat(),
            "mode": "cooperative" if execution.status == ExecutionStatus.RUNNING else "immediate",
        }
        execution.metadata = metadata
        if execution.status == ExecutionStatus.RUNNING:
            execution.status = ExecutionStatus.CANCELLING
            await self.execution_store.update_execution(execution)
            return execution
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

    async def _maybe_retrieve_graph_context_before_task(
            self,
            *,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            state: NativeExecutionState,
    ) -> None:
        if self.graph_context_retriever is None:
            return
        try:
            entry = await self.graph_context_retriever(
                workflow,
                task,
                agent,
                execution,
                execution.input_payload,
                state,
            )
        except Exception as exc:
            await self.emitter.emit(
                state,
                ExecutionEventType.AGENT_MESSAGE_CREATED,
                actor=agent.name,
                payload={
                    "source": "runtime_graph_context",
                    "trigger": "subagent_start",
                    "status": "failed",
                    "reason": "prepare_assigned_agent_context",
                    "error": str(exc),
                },
                metadata={"runtime_graph_context": True, "auto_retrieval": True},
                agent_id=agent.id,
                task_id=task.id,
            )
            return
        if not entry:
            return
        if entry.get("skipped"):
            context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
            query_meta = context.get("query_meta") if isinstance(context.get("query_meta"), dict) else {}
            await self.emitter.emit(
                state,
                ExecutionEventType.AGENT_MESSAGE_CREATED,
                actor=agent.name,
                payload={
                    "source": "runtime_graph_context",
                    "trigger": entry.get("trigger") or "subagent_start",
                    "status": context.get("status") or "skipped",
                    "reason": entry.get("reason") or "auto_retrieval_loop_guard_no_progress",
                    "skip_reason": entry.get("skip_reason"),
                    "intent": query_meta.get("intent") or entry.get("intent"),
                    "budget": query_meta.get("budget") or entry.get("budget"),
                    "anchor_type": query_meta.get("anchor_type") or entry.get("anchor_type"),
                    "anchor_id": query_meta.get("anchor_id") or entry.get("anchor_id"),
                },
                metadata={"runtime_graph_context": True, "auto_retrieval": True, "skipped": True},
                agent_id=agent.id,
                task_id=task.id,
            )
            return
        working_set = record_graph_context_working_set_entry(
            state,
            entry,
            owner_agent_id=agent.id,
            workflow_id=workflow.id,
            run_id=execution.id,
            execution_id=execution.id,
            conversation_id=_conversation_id_from_execution(execution),
        )
        state.graph_context_entries.append(entry)
        state.graph_context_entries = state.graph_context_entries[-25:]
        context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
        query_meta = context.get("query_meta") if isinstance(context.get("query_meta"), dict) else {}
        await self.emitter.emit(
            state,
            ExecutionEventType.AGENT_MESSAGE_CREATED,
            actor=agent.name,
            payload={
                "source": "runtime_graph_context",
                "trigger": entry.get("trigger") or "subagent_start",
                "status": context.get("status") or "unknown",
                "reason": entry.get("reason") or "runtime_auto_retrieval",
                "intent": query_meta.get("intent") or entry.get("intent"),
                "budget": query_meta.get("budget") or entry.get("budget"),
                "anchor_type": query_meta.get("anchor_type") or entry.get("anchor_type"),
                "anchor_id": query_meta.get("anchor_id") or entry.get("anchor_id"),
                "working_set_id": working_set.working_set_id,
                "node_count": query_meta.get("node_count"),
                "edge_count": query_meta.get("edge_count"),
            },
            metadata={"runtime_graph_context": True, "auto_retrieval": True},
            agent_id=agent.id,
            task_id=task.id,
        )

    async def _maybe_retrieve_graph_context_after_execution_failure(
            self,
            *,
            workflow: WorkflowDefinition,
            execution: Execution,
            state: NativeExecutionState,
            error: str,
            failure_event_id: str | None,
    ) -> None:
        if self.execution_failure_graph_context_retriever is None:
            return
        try:
            entry = await self.execution_failure_graph_context_retriever(
                workflow,
                execution,
                state,
                error,
                failure_event_id,
            )
        except Exception as exc:
            await self.emitter.emit(
                state,
                ExecutionEventType.AGENT_MESSAGE_CREATED,
                payload={
                    "source": "runtime_graph_context",
                    "trigger": "execution_failed",
                    "status": "failed",
                    "reason": "root_cause_context_after_execution_failure",
                    "error": str(exc),
                    "failure_event_id": failure_event_id,
                },
                metadata={"runtime_graph_context": True, "auto_retrieval": True},
            )
            return
        if not entry:
            return
        if entry.get("skipped"):
            context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
            query_meta = context.get("query_meta") if isinstance(context.get("query_meta"), dict) else {}
            await self.emitter.emit(
                state,
                ExecutionEventType.AGENT_MESSAGE_CREATED,
                payload={
                    "source": "runtime_graph_context",
                    "trigger": entry.get("trigger") or "execution_failed",
                    "status": context.get("status") or "skipped",
                    "reason": entry.get("reason") or "auto_retrieval_loop_guard_no_progress",
                    "skip_reason": entry.get("skip_reason"),
                    "intent": query_meta.get("intent") or entry.get("intent"),
                    "budget": query_meta.get("budget") or entry.get("budget"),
                    "anchor_type": query_meta.get("anchor_type") or entry.get("anchor_type"),
                    "anchor_id": query_meta.get("anchor_id") or entry.get("anchor_id"),
                    "failure_event_id": entry.get("failure_event_id") or failure_event_id,
                },
                metadata={"runtime_graph_context": True, "auto_retrieval": True, "skipped": True},
            )
            return
        working_set = record_graph_context_working_set_entry(
            state,
            entry,
            owner_agent_id=entry.get("agent_id") or state.current_agent_id,
            workflow_id=workflow.id,
            run_id=execution.id,
            execution_id=execution.id,
            conversation_id=_conversation_id_from_execution(execution),
        )
        state.graph_context_entries.append(entry)
        state.graph_context_entries = state.graph_context_entries[-25:]
        context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
        query_meta = context.get("query_meta") if isinstance(context.get("query_meta"), dict) else {}
        await self.emitter.emit(
            state,
            ExecutionEventType.AGENT_MESSAGE_CREATED,
            payload={
                "source": "runtime_graph_context",
                "trigger": entry.get("trigger") or "execution_failed",
                "status": context.get("status") or "unknown",
                "reason": entry.get("reason") or "runtime_auto_retrieval",
                "intent": query_meta.get("intent") or entry.get("intent"),
                "budget": query_meta.get("budget") or entry.get("budget"),
                "anchor_type": query_meta.get("anchor_type") or entry.get("anchor_type"),
                "anchor_id": query_meta.get("anchor_id") or entry.get("anchor_id"),
                "failure_event_id": entry.get("failure_event_id") or failure_event_id,
                "working_set_id": working_set.working_set_id,
                "node_count": query_meta.get("node_count"),
                "edge_count": query_meta.get("edge_count"),
                "failed_event_count": len(entry.get("failed_events") or []),
                "tool_call_count": len(entry.get("tool_calls") or []),
                "artifact_count": len(entry.get("artifacts") or []),
                "model_request_count": len(entry.get("model_requests") or []),
                "prior_attempt_count": len(entry.get("prior_attempts") or []),
            },
            metadata={"runtime_graph_context": True, "auto_retrieval": True},
        )

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

    def _workflow_remaining_seconds(
            self,
            execution: Execution,
            workflow_policy: dict[str, Any],
    ) -> float | None:
        max_runtime_seconds = workflow_policy.get("max_runtime_seconds")
        if max_runtime_seconds is None:
            return None
        started_at = execution.started_at or execution.created_at
        remaining = float(max_runtime_seconds) - (utc_now() - started_at).total_seconds()
        if remaining <= 0:
            raise TimeoutError(f"Workflow max runtime of {max_runtime_seconds} seconds exceeded.")
        return remaining

    def _handoff_edge_between(
            self,
            workflow: WorkflowDefinition,
            source_node_id: str,
            target_node_id: str,
    ) -> WorkflowEdgeDefinition | None:
        for edge in workflow.edges:
            if (
                    edge.source_node_id == source_node_id
                    and edge.target_node_id == target_node_id
                    and edge.edge_type == EdgeType.HANDOFF
            ):
                return edge
        return None

    async def _emit_handoff_event(
            self,
            *,
            workflow: WorkflowDefinition,
            edge: WorkflowEdgeDefinition,
            source_node: WorkflowNodeDefinition,
            target_node: WorkflowNodeDefinition,
            source_task: TaskDefinition,
            target_task: TaskDefinition,
            source_agent: AgentDefinition,
            target_agent: AgentDefinition,
            state: NativeExecutionState,
            status: str,
    ) -> None:
        allowed = target_agent.id == source_agent.id or target_agent.id in source_agent.handoff_agent_ids
        # Handoff events make graph edges observable at runtime without blocking older workflows that saved
        # a handoff edge before agent-level handoff allow-lists were populated.
        payload = {
            "handoff_id": edge.id,
            "edge_id": edge.id,
            "handoff_type": "workflow_edge",
            "status": status,
            "workflow_id": workflow.id,
            "source_node_id": source_node.id,
            "target_node_id": target_node.id,
            "source_task_id": source_task.id,
            "target_task_id": target_task.id,
            "source_agent_id": source_agent.id,
            "target_agent_id": target_agent.id,
            "allowed": allowed,
            "handoff_relationship": "declared" if allowed else "edge_only",
            "source_output_available": source_node.id in state.node_outputs,
            "target_output_available": target_node.id in state.node_outputs,
        }
        event_type = (
            ExecutionEventType.HANDOFF_COMPLETED
            if status == "completed"
            else ExecutionEventType.HANDOFF_REQUESTED
        )
        await self.emitter.emit(
            state,
            event_type,
            actor=source_agent.name if status == "requested" else target_agent.name,
            payload=payload,
            metadata={"workflow_handoff": True, "edge_id": edge.id},
            agent_id=target_agent.id,
            task_id=target_task.id,
        )

    async def _resolve_profile(
            self,
            agent: AgentDefinition,
            *,
            task: TaskDefinition | None = None,
            task_overrides: dict[str, Any] | None = None,
    ) -> ModelProfileDefinition:
        task_overrides = task_overrides or {}
        model_profile_id = task_overrides.get("model_profile_id") or agent.model_profile_id
        if not model_profile_id:
            raise WorkflowNotFoundError(f"Agent '{agent.id}' is missing model_profile_id")
        profile = await self.model_profile_repository.get_profile(model_profile_id)
        if profile is None:
            raise WorkflowNotFoundError(f"Model profile '{model_profile_id}' was not found")
        max_tokens = task_overrides.get("max_tokens")
        if max_tokens is not None:
            profile = profile.model_copy(update={"max_tokens": max_tokens})
        return profile

    async def _maybe_request_task_approval(
            self,
            *,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            state: NativeExecutionState,
            approval_policy: Any,
            reason: str | None = None,
    ) -> None:
        if approval_policy != "required":
            return

        approval_event = await self.emitter.emit(
            state,
            ExecutionEventType.APPROVAL_REQUESTED,
            actor=agent.name,
            payload={
                "approval_type": "task",
                "workflow_id": workflow.id,
                "task_id": task.id,
                "task_name": task.name,
                "reason": reason or "Task approval policy requires human approval before execution.",
            },
            agent_id=agent.id,
            task_id=task.id,
        )
        decision = await self.approval_manager.request_approval(
            execution_id=execution.id,
            tool_id=_task_approval_tool_id(task),
            payload={
                "approval_type": "task",
                "workflow_id": workflow.id,
                "task_id": task.id,
                "task_name": task.name,
                "reason": reason,
            },
            event_id=approval_event.id,
            approval_metadata={
                "approval_type": "task",
                "approval_event_id": approval_event.id,
                "workflow_id": workflow.id,
                "agent_id": agent.id,
                "task_id": task.id,
            },
        )
        if not decision.granted:
            await self.emitter.emit(
                state,
                ExecutionEventType.APPROVAL_REJECTED,
                actor=agent.name,
                payload={
                    "approval_type": "task",
                    "task_id": task.id,
                    "task_name": task.name,
                    "reason": decision.reason,
                    "decision_metadata": decision.metadata or {},
                },
                agent_id=agent.id,
                task_id=task.id,
            )
            raise ExecutionPausedError(decision.reason or f"Task '{task.id}' approval was rejected.")
        await self.emitter.emit(
            state,
            ExecutionEventType.APPROVAL_GRANTED,
            actor=agent.name,
            payload={
                "approval_type": "task",
                "task_id": task.id,
                "task_name": task.name,
                "decision_metadata": decision.metadata or {},
            },
            agent_id=agent.id,
            task_id=task.id,
        )

    async def _maybe_request_workflow_approval(
            self,
            *,
            workflow: WorkflowDefinition,
            execution: Execution,
            state: NativeExecutionState,
            reason: str,
    ) -> None:
        approval_event = await self.emitter.emit(
            state,
            ExecutionEventType.APPROVAL_REQUESTED,
            payload={
                "approval_type": "workflow",
                "workflow_id": workflow.id,
                "workflow_name": workflow.name,
                "reason": reason,
            },
        )
        decision = await self.approval_manager.request_approval(
            execution_id=execution.id,
            tool_id=f"{WORKFLOW_APPROVAL_TOOL_ID_PREFIX}{workflow.id}",
            payload={
                "approval_type": "workflow",
                "workflow_id": workflow.id,
                "workflow_name": workflow.name,
                "reason": reason,
            },
            event_id=approval_event.id,
            approval_metadata={
                "approval_type": "workflow",
                "approval_event_id": approval_event.id,
                "workflow_id": workflow.id,
            },
        )
        if not decision.granted:
            await self.emitter.emit(
                state,
                ExecutionEventType.APPROVAL_REJECTED,
                payload={
                    "approval_type": "workflow",
                    "workflow_id": workflow.id,
                    "workflow_name": workflow.name,
                    "reason": decision.reason,
                    "decision_metadata": decision.metadata or {},
                },
            )
            raise ExecutionPausedError(decision.reason or f"Workflow '{workflow.id}' approval was rejected.")
        await self.emitter.emit(
            state,
            ExecutionEventType.APPROVAL_GRANTED,
            payload={
                "approval_type": "workflow",
                "workflow_id": workflow.id,
                "workflow_name": workflow.name,
                "decision_metadata": decision.metadata or {},
            },
        )

    async def _execute_task_with_runtime_overrides(
            self,
            *,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            profile: ModelProfileDefinition,
            model_client,
            state: NativeExecutionState,
            execution: Execution,
            task_overrides: dict[str, Any],
            workflow_timeout_seconds: float | None = None,
    ):
        max_retries = int(task_overrides.get("max_retries") or 0)
        timeout_seconds = task_overrides.get("timeout_seconds")
        approval_policy = task_overrides.get("approval_policy")
        attempts = max_retries + 1
        failure_approval_used = False

        attempt = 1
        while True:
            try:
                await self.emitter.emit(
                    state,
                    ExecutionEventType.AGENT_STEP_STARTED,
                    actor=agent.name,
                    payload={
                        "task_id": task.id,
                        "task_name": task.name,
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "step_kind": "task_execution",
                        "summary": f"{agent.name} is executing task '{task.name or task.id}'.",
                        "model_profile_id": profile.id,
                        "runtime_overrides": task_overrides,
                    },
                    agent_id=agent.id,
                    task_id=task.id,
                )
                execution_call = self.agent_executor.execute_task(
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
                task_timeout_seconds = (
                    float(timeout_seconds) if timeout_seconds is not None else None
                )
                workflow_timeout_selected = False
                if task_timeout_seconds is not None and workflow_timeout_seconds is not None:
                    effective_timeout = min(task_timeout_seconds, workflow_timeout_seconds)
                    workflow_timeout_selected = workflow_timeout_seconds <= task_timeout_seconds
                elif task_timeout_seconds is not None:
                    effective_timeout = task_timeout_seconds
                else:
                    effective_timeout = workflow_timeout_seconds
                    workflow_timeout_selected = workflow_timeout_seconds is not None
                if effective_timeout is not None:
                    output, messages = await asyncio.wait_for(execution_call, timeout=effective_timeout)
                else:
                    output, messages = await execution_call
                await self.emitter.emit(
                    state,
                    ExecutionEventType.AGENT_STEP_COMPLETED,
                    actor=agent.name,
                    payload={
                        "task_id": task.id,
                        "task_name": task.name,
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "step_kind": "task_execution",
                        "summary": f"{agent.name} completed task '{task.name or task.id}'.",
                        "output_preview": str(output)[:500],
                        "model_profile_id": profile.id,
                        "runtime_overrides": task_overrides,
                    },
                    agent_id=agent.id,
                    task_id=task.id,
                )
                return output, messages
            except (ExecutionPausedError, ExecutionCancelledError):
                raise
            except Exception as exc:
                timeout_error = isinstance(exc, asyncio.TimeoutError)
                workflow_timed_out = timeout_error and workflow_timeout_selected
                task_timed_out = timeout_error and not workflow_timeout_selected
                error_message = (
                    "Workflow max runtime exceeded."
                    if workflow_timed_out and not str(exc)
                    else "Task execution timed out."
                    if task_timed_out and not str(exc)
                    else str(exc)
                )
                should_retry = attempt < attempts and not workflow_timed_out
                if (
                        not should_retry
                        and not workflow_timed_out
                        and approval_policy == "on_failure"
                        and not failure_approval_used
                ):
                    await self._maybe_request_task_approval(
                        workflow=workflow,
                        task=task,
                        agent=agent,
                        execution=execution,
                        state=state,
                        approval_policy="required",
                        reason=f"Task failed with {type(exc).__name__}: {exc}",
                    )
                    failure_approval_used = True
                    should_retry = True

                await self.emitter.emit(
                    state,
                    ExecutionEventType.AGENT_STEP_FAILED,
                    actor=agent.name,
                    payload={
                        "task_id": task.id,
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "will_retry": should_retry,
                        "error": error_message,
                        "error_type": type(exc).__name__,
                        "runtime_overrides": task_overrides,
                    },
                    agent_id=agent.id,
                    task_id=task.id,
                )
                if not should_retry:
                    if workflow_timed_out and not str(exc):
                        raise TimeoutError("Workflow max runtime exceeded.") from exc
                    if task_timed_out and not str(exc):
                        raise TimeoutError("Task execution timed out.") from exc
                    raise
                attempt += 1

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

    @staticmethod
    def _raise_if_cancelled(state: NativeExecutionState) -> None:
        if state.cancelled:
            raise ExecutionCancelledError("Execution was cancelled")

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

    async def _hydrate_resume_checkpoint(
            self,
            execution: Execution,
            state: NativeExecutionState,
    ) -> dict[str, Any] | None:
        if state.node_outputs or _task_retry_metadata(execution.metadata):
            return None

        summary = _checkpoint_payload_summary(execution.output_payload)
        if summary is not None:
            state.node_outputs = _node_outputs_from_checkpoint(execution.output_payload)
            checkpoint = execution.output_payload.get("checkpoint") if isinstance(execution.output_payload,
                                                                                  dict) else {}
            if isinstance(checkpoint, dict):
                state.current_node_id = checkpoint.get("current_node_id")
                state.current_task_id = checkpoint.get("current_task_id")
                state.planned_node_ids = list(summary.get("planned_node_ids") or [])
                state.terminal_node_ids = list(summary.get("terminal_node_ids") or [])
            return {
                "source": "execution.output_payload",
                "source_execution_id": execution.id,
                **summary,
            }

        metadata = execution.metadata if isinstance(execution.metadata, dict) else {}
        explicit_source_id = metadata.get("resume_from_execution_id")
        source_execution_id = (
            explicit_source_id
            if isinstance(explicit_source_id, str) and explicit_source_id
            else execution.replacement_of_execution_id
        )
        if not source_execution_id:
            return None
        source_execution = await self.execution_store.get_execution(source_execution_id)
        if source_execution is None:
            return None
        summary = _checkpoint_payload_summary(source_execution.output_payload)
        if summary is None:
            return None
        state.node_outputs = _node_outputs_from_checkpoint(source_execution.output_payload)
        checkpoint = (
            source_execution.output_payload.get("checkpoint")
            if isinstance(source_execution.output_payload, dict)
            else {}
        )
        if isinstance(checkpoint, dict):
            state.current_node_id = checkpoint.get("current_node_id")
            state.current_task_id = checkpoint.get("current_task_id")
            state.planned_node_ids = list(summary.get("planned_node_ids") or [])
            state.terminal_node_ids = list(summary.get("terminal_node_ids") or [])
        return {
            "source": "replacement_of_execution_id" if source_execution_id == execution.replacement_of_execution_id
            else "execution.metadata.resume_from_execution_id",
            "source_execution_id": source_execution.id,
            **summary,
        }
