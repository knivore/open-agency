"""Single-agent execution loop for the native runtime.

`AgentExecutor` builds model messages from workflow/task/agent definitions,
handles tool-call iterations, records events, and enforces pause/cancel state
between model calls.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any, Awaitable, Callable, Dict, List, Tuple
from uuid import uuid4

from jsonschema import ValidationError as JSONSchemaValidationError, validate

from app.domain import (
    AgentDefinition,
    ContextCompactionRecord,
    ContextHealth,
    Execution,
    ExecutionEventType,
    ModelProfileDefinition,
    TaskDefinition,
    ToolDefinition,
    TokenBudgetPolicy,
    TokenBudgetStatus,
    WorkflowDefinition,
)
from app.llm.base import BaseModelClient, ModelMessage, ModelToolCall
from app.llm.fallback import ModelFallbackExhaustedError
from app.runtime.governance.budgets import resolve_token_budget_policy
from app.runtime.governance.compaction import ContextCompactionResult
from app.runtime.governance.context_health import estimate_context_health
from app.runtime.governance.recorder import (
    record_context_compaction_snapshot,
    record_context_health_snapshot,
    record_token_usage_snapshot,
)
from app.runtime.governance.token_usage import normalize_token_usage
from app.runtime.native.errors import (
    ExecutionCancelledError,
    ExecutionPausedError,
    MaxIterationsReachedError,
    ToolExecutionError,
)
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState, record_graph_context_working_set_entry
from app.runtime.native.tool_executor import ToolExecutor
from app.tools.names import tool_call_name

MemoryPromptBuilder = Callable[
    [WorkflowDefinition, TaskDefinition, AgentDefinition, Execution, Dict[str, Any], NativeExecutionState],
    Awaitable[str],
]
ContextCompactor = Callable[
    [
        WorkflowDefinition,
        TaskDefinition,
        AgentDefinition,
        ModelProfileDefinition,
        Execution,
        Dict[str, Any],
        NativeExecutionState,
        List[ModelMessage],
        ContextHealth,
        str,
    ],
    Awaitable[ContextCompactionResult],
]
ContextCompactionGraphContextRetriever = Callable[
    [
        WorkflowDefinition,
        TaskDefinition,
        AgentDefinition,
        Execution,
        NativeExecutionState,
        ContextCompactionRecord,
    ],
    Awaitable[dict[str, Any] | None],
]
ProposalToolGraphContextRetriever = Callable[
    [
        WorkflowDefinition,
        TaskDefinition,
        AgentDefinition,
        Execution,
        NativeExecutionState,
        ToolDefinition,
        dict[str, Any],
        str | None,
    ],
    Awaitable[dict[str, Any] | None],
]
ToolDefinitionLoader = Callable[[set[str]], Awaitable[list[ToolDefinition]]]


_RUNTIME_WORKFLOW_METADATA_KEYS = {
    "connector_bindings",
    "discord_delivery",
    "media_delivery",
    "voice_delivery",
    "voice_generation",
}


def _structured_model_output(content: Any, output_schema: dict[str, Any]) -> Any:
    """Parse and validate declared structured outputs before routing uses them."""
    if not output_schema:
        return content
    parsed = content
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith(("{", "[")):
            parsed = json.loads(stripped)
    validate(instance=parsed, schema=output_schema)
    return parsed


def _structured_output_contract(output_schema: dict[str, Any]) -> str:
    """Tell text-generation models the same contract enforced after their response."""
    return (
        "Final response contract: return only JSON that validates against this JSON Schema. "
        "Include every required property and use only allowed enum values.\n"
        f"{json.dumps(output_schema, sort_keys=True)}"
    )


def _structured_output_repair_message(
        output_schema: dict[str, Any],
        error: Exception,
) -> str:
    error_message = getattr(error, "message", str(error))
    return (
        "Your previous final response did not satisfy the task output schema. "
        "Reformat the existing result only; do not call tools again or repeat side effects. "
        f"Validation error: {error_message}.\n"
        f"{_structured_output_contract(output_schema)}"
    )


def _dependency_outputs(
        workflow: WorkflowDefinition,
        task: TaskDefinition,
        state: NativeExecutionState,
) -> dict[str, Any]:
    if not task.depends_on_task_ids:
        return dict(state.node_outputs)
    dependency_ids = set(task.depends_on_task_ids)
    return {
        node.task_id: state.node_outputs[node.id]
        for node in workflow.nodes
        if node.task_id in dependency_ids and node.id in state.node_outputs
    }


def _task_runtime_context(
        workflow: WorkflowDefinition,
        task: TaskDefinition,
        execution: Execution,
        state: NativeExecutionState,
) -> dict[str, Any]:
    workflow_metadata = {
        key: value
        for key, value in workflow.metadata.items()
        if key in _RUNTIME_WORKFLOW_METADATA_KEYS
    }
    dependency_outputs = _dependency_outputs(workflow, task, state) if task.depends_on_task_ids else {}
    if not task.metadata and not workflow_metadata and not dependency_outputs:
        return {}
    return {
        "workflow_id": workflow.id,
        "execution_id": execution.id,
        "task_id": task.id,
        "task_metadata": task.metadata,
        "workflow_metadata": workflow_metadata,
        "dependency_outputs": dependency_outputs,
    }

APPROVAL_CONTINUATION_KEY = "approval_continuation"


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _model_message_from_payload(payload: dict[str, Any]) -> ModelMessage:
    return ModelMessage(
        role=payload.get("role", "user"),
        content=payload.get("content"),
        name=payload.get("name"),
        tool_call_id=payload.get("tool_call_id"),
        tool_calls=[
            ModelToolCall(
                id=item.get("id"),
                name=str(item.get("name") or ""),
                arguments=dict(item.get("arguments") or {}),
            )
            for item in payload.get("tool_calls", [])
            if isinstance(item, dict)
        ],
        metadata=dict(payload.get("metadata") or {}),
    )


def _graph_context_prompt_for_task(
        state: NativeExecutionState,
        *,
        agent: AgentDefinition,
        task: TaskDefinition,
) -> str:
    entries = [
        entry
        for entry in state.graph_context_entries[-10:]
        if entry.get("agent_id") == agent.id and entry.get("task_id") == task.id
    ]
    if not entries:
        return ""
    return _format_graph_context_entry(entries[-1])


def _format_graph_context_entry(entry: dict[str, Any]) -> str:
    context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
    query_meta = context.get("query_meta") if isinstance(context.get("query_meta"), dict) else {}
    lines = [
        "# Runtime Agency Graph Context",
        f"trigger={entry.get('trigger') or 'unknown'}",
        f"reason={entry.get('reason') or 'runtime_auto_retrieval'}",
        f"intent={query_meta.get('intent') or entry.get('intent') or 'unknown'}",
        f"budget={query_meta.get('budget') or entry.get('budget') or 'unknown'}",
    ]
    anchor_type = query_meta.get("anchor_type") or entry.get("anchor_type")
    anchor_id = query_meta.get("anchor_id") or entry.get("anchor_id")
    if anchor_type and anchor_id:
        lines.append(f"anchor={anchor_type}:{anchor_id}")
    summary = context.get("summary")
    if summary:
        lines.append(f"summary={summary}")
    for section_name, label, limit in (
            ("facts", "facts", 8),
            ("related_memories", "related_memories", 4),
            ("run_summaries", "run_summaries", 4),
            ("recent_events", "recent_events", 4),
            ("prior_attempts", "prior_attempts", 4),
            ("prior_changes", "prior_changes", 4),
            ("failures", "failures", 4),
            ("decisions", "decisions", 4),
            ("constraints", "constraints", 4),
            ("next_actions", "next_actions", 4),
    ):
        section = context.get(section_name)
        if not isinstance(section, list) or not section:
            continue
        lines.append(f"{label}: {json.dumps(section[:limit], default=str)}")
    return "\n".join(lines)


def _context_health_blocks_graph_context(context_health: ContextHealth | None) -> bool:
    return bool(context_health is not None and context_health.status in {"critical", "overflow"})


def _token_budget_blocks_graph_context(budget_statuses: list[TokenBudgetStatus] | None) -> TokenBudgetStatus | None:
    return next((item for item in budget_statuses or [] if item.status == "exceeded"), None)


def _graph_context_message(
        entry: dict[str, Any],
        *,
        trigger: str,
) -> ModelMessage:
    return ModelMessage(
        role="system",
        content=_format_graph_context_entry(entry),
        name="runtime_graph_context",
        metadata={
            "runtime_graph_context": True,
            "auto_retrieval": True,
            "trigger": trigger,
        },
    )


def _conversation_id_from_execution(execution: Execution) -> str | None:
    for container in (execution.trigger_payload, execution.input_payload, execution.metadata):
        if not isinstance(container, dict):
            continue
        for key in ("conversation_id", "conversationId", "thread_id", "threadId"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


class AgentExecutor:
    """Execute one task for one agent against a model client and tool executor."""

    def __init__(
            self,
            tool_executor: ToolExecutor,
            *,
            tool_definition_loader: ToolDefinitionLoader | None = None,
            memory_prompt_builder: MemoryPromptBuilder | None = None,
            context_compactor: ContextCompactor | None = None,
            context_compaction_graph_context_retriever: ContextCompactionGraphContextRetriever | None = None,
            proposal_tool_graph_context_retriever: ProposalToolGraphContextRetriever | None = None,
    ):
        self.tool_executor = tool_executor
        self.tool_definition_loader = tool_definition_loader
        self.memory_prompt_builder = memory_prompt_builder
        self.context_compactor = context_compactor
        self.context_compaction_graph_context_retriever = context_compaction_graph_context_retriever
        self.proposal_tool_graph_context_retriever = proposal_tool_graph_context_retriever

    async def _build_messages(
            self,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            execution_input: Dict[str, Any],
            state: NativeExecutionState,
    ) -> List[ModelMessage]:
        system_parts = [
            agent.system_prompt or "",
            f"Role: {agent.role}" if agent.role else "",
            f"Instructions: {agent.instructions}" if agent.instructions else "",
            f"Backstory: {agent.backstory}" if agent.backstory else "",
            task.instructions or "",
            f"Expected output: {task.expected_output}" if task.expected_output else "",
        ]
        if task.output_schema:
            system_parts.append(_structured_output_contract(task.output_schema))
        if self.memory_prompt_builder is not None:
            try:
                memory_prompt = await self.memory_prompt_builder(
                    workflow,
                    task,
                    agent,
                    execution,
                    execution_input,
                    state,
                )
            except Exception:
                memory_prompt = ""
            if memory_prompt:
                system_parts.append(memory_prompt)
        graph_context_prompt = _graph_context_prompt_for_task(state, agent=agent, task=task)
        if graph_context_prompt:
            system_parts.append(graph_context_prompt)
        if state.memory_entries:
            system_parts.append(f"Memory: {json.dumps(state.memory_entries)}")
        dependency_outputs = _dependency_outputs(workflow, task, state)
        if dependency_outputs:
            system_parts.append(f"Dependency outputs: {json.dumps(dependency_outputs, default=str)}")

        runtime_context = _task_runtime_context(workflow, task, execution, state)
        user_content = {
            "workflow": workflow.name,
            "task": task.description,
            "input": execution_input,
        }
        if runtime_context:
            user_content["runtime_context"] = runtime_context
        return [
            ModelMessage(role="system", content="\n".join(part for part in system_parts if part)),
            ModelMessage(role="user", content=json.dumps(user_content, default=str)),
        ]

    async def _tool_payload(
            self,
            workflow: WorkflowDefinition,
            agent: AgentDefinition,
            task: TaskDefinition,
    ) -> List[Dict[str, Any]]:
        allowed_tool_ids = task.tool_ids or agent.tool_ids
        if self.tool_definition_loader is not None:
            requested_tool_ids = set(allowed_tool_ids)
            catalog_tools = await self.tool_definition_loader(requested_tool_ids)
            self.tool_executor.register_catalog_tools(workflow.id, requested_tool_ids, catalog_tools)
        tools = []
        for tool_id in allowed_tool_ids:
            # Workflows often persist built-in Agency tools as IDs instead of embedding the
            # full definition. Resolve those IDs here so the model receives the same callable
            # surface the task/agent contract advertises.
            try:
                tool = self.tool_executor.resolve_tool(workflow, tool_id)
            except ToolExecutionError:
                continue
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_call_name(tool),
                        "description": tool.description,
                        "parameters": tool.input_schema or {"type": "object"},
                    },
                }
            )
        return tools

    def _assert_not_interrupted(self, state: NativeExecutionState) -> None:
        if state.cancelled:
            raise ExecutionCancelledError("Execution was cancelled")
        if state.paused:
            raise ExecutionPausedError("Execution is paused")

    @staticmethod
    def _is_context_length_error(exc: Exception) -> bool:
        text = str(exc).lower()
        markers = (
            "context length",
            "context_length",
            "maximum context",
            "max context",
            "context window",
            "too many tokens",
            "token limit",
            "prompt is too long",
            "input is too long",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _annotate_compaction_state_messages(
            messages: List[ModelMessage],
            record: ContextCompactionRecord,
    ) -> None:
        if not record.compacted:
            return
        header = "Runtime Context Compaction State"
        state_block = "\n".join(
            [
                f"# {header}",
                "context_compacted=true",
                f"compaction_reason={record.reason or 'unknown'}",
                f"context_pack_memory_id={record.memory_id or 'none'}",
                f"source_model_request_id={record.source_model_request_id or 'unknown'}",
                f"estimated_tokens_saved={record.estimated_tokens_saved}",
                f"protected_context_retained={str(bool(record.metadata.get('protected_context_retained'))).lower()}",
            ]
        )
        for message in messages:
            if message.name != "runtime_context_compaction":
                continue
            governance = dict(message.metadata.get("runtime_governance") or {})
            governance.update(
                {
                    "context_compacted": True,
                    "compaction_reason": record.reason,
                    "context_pack_memory_id": record.memory_id,
                    "estimated_tokens_saved": record.estimated_tokens_saved,
                }
            )
            message.metadata["runtime_governance"] = governance
            content = str(message.content)
            if content.startswith(f"# {header}"):
                _, _, content = content.partition("\n\n")
            message.content = f"{state_block}\n\n{content}"
            break

    @staticmethod
    def _record_compaction_state(
            *,
            state: NativeExecutionState,
            record: ContextCompactionRecord,
            reason: str,
            iteration: int,
            model_request_id: str,
            agent_id: str,
            task_id: str,
            context_health_before: ContextHealth,
            context_health_after: ContextHealth,
    ) -> None:
        entry = {
            "compacted": record.compacted,
            "reason": record.reason or reason,
            "memory_id": record.memory_id,
            "source_model_request_id": record.source_model_request_id or model_request_id,
            "model_request_id": model_request_id,
            "iteration": iteration,
            "agent_id": agent_id,
            "task_id": task_id,
            "estimated_tokens_saved": record.estimated_tokens_saved,
            "context_status_before": context_health_before.status,
            "context_status_after": context_health_after.status,
            "context_usage_ratio_before": context_health_before.usage_ratio,
            "context_usage_ratio_after": context_health_after.usage_ratio,
            "source_event_start_sequence": record.metadata.get("source_event_start_sequence"),
            "source_event_end_sequence": record.metadata.get("source_event_end_sequence"),
        }
        state.context_compaction = {
            **state.context_compaction,
            "last": entry,
            "count": int(state.context_compaction.get("count") or 0) + 1,
            "compacted_count": (
                    int(state.context_compaction.get("compacted_count") or 0) + (1 if record.compacted else 0)
            ),
        }
        if record.compacted:
            state.context_compaction["estimated_tokens_saved"] = (
                    int(state.context_compaction.get("estimated_tokens_saved") or 0) + record.estimated_tokens_saved
            )
        records = list(state.context_compaction.get("records") or [])
        records.append(entry)
        state.context_compaction["records"] = records[-25:]
        if record.memory_id:
            state.compacted_context_packs.append(
                {
                    "memory_id": record.memory_id,
                    "reason": entry["reason"],
                    "source_model_request_id": entry["source_model_request_id"],
                    "agent_id": agent_id,
                    "task_id": task_id,
                    "iteration": iteration,
                }
            )

    async def _record_context_health(
            self,
            *,
            agent: AgentDefinition,
            task: TaskDefinition,
            profile: ModelProfileDefinition,
            state: NativeExecutionState,
            emitter: ExecutionEventEmitter,
            execution: Execution,
            context_health: ContextHealth,
            iteration: int,
            model_request_id: str,
            after_compaction: bool = False,
            compaction_reason: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "iteration": iteration,
            "model_profile_id": profile.id,
            "call_kind": "agent_task",
            **context_health.model_dump(mode="json"),
        }
        if after_compaction:
            payload["after_compaction"] = True
        if compaction_reason:
            payload["compaction_reason"] = compaction_reason
        context_event = await emitter.emit(
            state,
            ExecutionEventType.CONTEXT_HEALTH_RECORDED,
            actor=agent.name,
            payload=payload,
            metrics={
                "estimated_prompt_tokens": context_health.estimated_prompt_tokens,
                "reserved_completion_tokens": context_health.reserved_completion_tokens,
                "estimated_total_context_tokens": context_health.estimated_total_context_tokens,
                "context_window": context_health.context_window or 0,
                "context_usage_ratio": context_health.usage_ratio or 0,
                "context_status": context_health.status,
            },
            agent_id=agent.id,
            task_id=task.id,
            model_request_id=model_request_id,
        )
        await record_context_health_snapshot(
            emitter.store,
            execution_id=execution.id,
            context_health=context_health,
            agent_id=agent.id,
            task_id=task.id,
            event_id=context_event.id,
        )

    async def _maybe_retrieve_graph_context_after_context_compaction(
            self,
            *,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            state: NativeExecutionState,
            emitter: ExecutionEventEmitter,
            record: ContextCompactionRecord,
            context_health: ContextHealth | None = None,
    ) -> None:
        if self.context_compaction_graph_context_retriever is None or not record.compacted:
            return
        try:
            entry = await self.context_compaction_graph_context_retriever(
                workflow,
                task,
                agent,
                execution,
                state,
                record,
            )
        except Exception as exc:
            await emitter.emit(
                state,
                ExecutionEventType.AGENT_MESSAGE_CREATED,
                actor=agent.name,
                payload={
                    "source": "runtime_graph_context",
                    "trigger": "context_compaction",
                    "status": "failed",
                    "reason": "handoff_context_after_compaction",
                    "error": str(exc),
                    "context_pack_id": record.memory_id,
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
            await emitter.emit(
                state,
                ExecutionEventType.AGENT_MESSAGE_CREATED,
                actor=agent.name,
                payload={
                    "source": "runtime_graph_context",
                    "trigger": entry.get("trigger") or "context_compaction",
                    "status": context.get("status") or "skipped",
                    "reason": entry.get("reason") or "auto_retrieval_loop_guard_no_progress",
                    "skip_reason": entry.get("skip_reason"),
                    "intent": query_meta.get("intent") or entry.get("intent"),
                    "budget": query_meta.get("budget") or entry.get("budget"),
                    "anchor_type": query_meta.get("anchor_type") or entry.get("anchor_type"),
                    "anchor_id": query_meta.get("anchor_id") or entry.get("anchor_id"),
                    "context_pack_id": entry.get("context_pack_id") or record.memory_id,
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
        await emitter.emit(
            state,
            ExecutionEventType.AGENT_MESSAGE_CREATED,
            actor=agent.name,
            payload={
                "source": "runtime_graph_context",
                "trigger": entry.get("trigger") or "context_compaction",
                "status": context.get("status") or "unknown",
                "reason": entry.get("reason") or "handoff_context_after_compaction",
                "intent": query_meta.get("intent") or entry.get("intent"),
                "budget": query_meta.get("budget") or entry.get("budget"),
                "anchor_type": query_meta.get("anchor_type") or entry.get("anchor_type"),
                "anchor_id": query_meta.get("anchor_id") or entry.get("anchor_id"),
                "context_pack_id": entry.get("context_pack_id") or record.memory_id,
                "working_set_id": working_set.working_set_id,
                "graph_context_metadata_attached": bool(entry.get("graph_context_metadata_attached")),
                "node_count": query_meta.get("node_count"),
                "edge_count": query_meta.get("edge_count"),
                "decision_count": len(context.get("decisions") or []),
                "constraint_count": len(context.get("constraints") or []),
                "next_action_count": len(context.get("next_actions") or []),
            },
            metadata={"runtime_graph_context": True, "auto_retrieval": True},
            agent_id=agent.id,
            task_id=task.id,
        )

    async def _maybe_retrieve_graph_context_before_proposal_tool(
            self,
            *,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            state: NativeExecutionState,
            emitter: ExecutionEventEmitter,
            tool: ToolDefinition | None,
            arguments: dict[str, Any],
            tool_call_id: str | None,
            context_health: ContextHealth | None = None,
            budget_statuses: list[TokenBudgetStatus] | None = None,
    ) -> dict[str, Any] | None:
        if self.proposal_tool_graph_context_retriever is None or tool is None:
            return None
        if _context_health_blocks_graph_context(context_health):
            await emitter.emit(
                state,
                ExecutionEventType.AGENT_MESSAGE_CREATED,
                actor=agent.name,
                payload={
                    "source": "runtime_graph_context",
                    "trigger": "proposal_tool",
                    "status": "skipped",
                    "reason": "context_health_guard",
                    "skip_reason": "context_health_critical",
                    "context_status": context_health.status if context_health else None,
                    "context_usage_ratio": context_health.usage_ratio if context_health else None,
                    "remaining_context_tokens": context_health.remaining_context_tokens if context_health else None,
                    "tool_id": tool.id,
                    "tool_name": tool.name,
                    "tool_call_id": tool_call_id,
                },
                metadata={"runtime_graph_context": True, "auto_retrieval": True, "skipped": True},
                agent_id=agent.id,
                task_id=task.id,
            )
            return None
        exceeded = _token_budget_blocks_graph_context(budget_statuses)
        if exceeded is not None:
            await emitter.emit(
                state,
                ExecutionEventType.AGENT_MESSAGE_CREATED,
                actor=agent.name,
                payload={
                    "source": "runtime_graph_context",
                    "trigger": "proposal_tool",
                    "status": "skipped",
                    "reason": "budget_limit_guard",
                    "skip_reason": "budget_limit_exceeded",
                    "budget": exceeded.model_dump(mode="json"),
                    "tool_id": tool.id,
                    "tool_name": tool.name,
                    "tool_call_id": tool_call_id,
                },
                metadata={"runtime_graph_context": True, "auto_retrieval": True, "skipped": True},
                agent_id=agent.id,
                task_id=task.id,
            )
            return None
        try:
            entry = await self.proposal_tool_graph_context_retriever(
                workflow,
                task,
                agent,
                execution,
                state,
                tool,
                arguments,
                tool_call_id,
            )
        except Exception as exc:
            await emitter.emit(
                state,
                ExecutionEventType.AGENT_MESSAGE_CREATED,
                actor=agent.name,
                payload={
                    "source": "runtime_graph_context",
                    "trigger": "proposal_tool",
                    "status": "failed",
                    "reason": "prepare_mutation_proposal_context",
                    "tool_id": tool.id,
                    "tool_name": tool.name,
                    "tool_call_id": tool_call_id,
                    "error": str(exc),
                },
                metadata={"runtime_graph_context": True, "auto_retrieval": True},
                agent_id=agent.id,
                task_id=task.id,
            )
            return None
        if not entry:
            return None
        if entry.get("skipped"):
            context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
            query_meta = context.get("query_meta") if isinstance(context.get("query_meta"), dict) else {}
            await emitter.emit(
                state,
                ExecutionEventType.AGENT_MESSAGE_CREATED,
                actor=agent.name,
                payload={
                    "source": "runtime_graph_context",
                    "trigger": entry.get("trigger") or "proposal_tool",
                    "status": context.get("status") or "skipped",
                    "reason": entry.get("reason") or "auto_retrieval_loop_guard_no_progress",
                    "skip_reason": entry.get("skip_reason"),
                    "intent": query_meta.get("intent") or entry.get("intent"),
                    "budget": query_meta.get("budget") or entry.get("budget"),
                    "anchor_type": query_meta.get("anchor_type") or entry.get("anchor_type"),
                    "anchor_id": query_meta.get("anchor_id") or entry.get("anchor_id"),
                    "tool_id": entry.get("proposal_tool_id") or tool.id,
                    "tool_name": entry.get("proposal_tool_name") or tool.name,
                    "tool_call_id": tool_call_id,
                },
                metadata={"runtime_graph_context": True, "auto_retrieval": True, "skipped": True},
                agent_id=agent.id,
                task_id=task.id,
            )
            return None
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
        await emitter.emit(
            state,
            ExecutionEventType.AGENT_MESSAGE_CREATED,
            actor=agent.name,
            payload={
                "source": "runtime_graph_context",
                "trigger": entry.get("trigger") or "proposal_tool",
                "status": context.get("status") or "unknown",
                "reason": entry.get("reason") or "prepare_mutation_proposal_context",
                "intent": query_meta.get("intent") or entry.get("intent"),
                "budget": query_meta.get("budget") or entry.get("budget"),
                "anchor_type": query_meta.get("anchor_type") or entry.get("anchor_type"),
                "anchor_id": query_meta.get("anchor_id") or entry.get("anchor_id"),
                "tool_id": entry.get("proposal_tool_id") or tool.id,
                "tool_name": entry.get("proposal_tool_name") or tool.name,
                "tool_call_id": tool_call_id,
                "working_set_id": working_set.working_set_id,
                "node_count": query_meta.get("node_count"),
                "edge_count": query_meta.get("edge_count"),
            },
            metadata={"runtime_graph_context": True, "auto_retrieval": True},
            agent_id=agent.id,
            task_id=task.id,
        )
        return entry

    async def _compact_context(
            self,
            *,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            profile: ModelProfileDefinition,
            execution: Execution,
            execution_input: Dict[str, Any],
            state: NativeExecutionState,
            emitter: ExecutionEventEmitter,
            messages: List[ModelMessage],
            context_health: ContextHealth,
            iteration: int,
            model_request_id: str,
            reason: str,
            extra_payload: dict[str, Any] | None = None,
            extra_metadata: dict[str, Any] | None = None,
            extra_metrics: dict[str, Any] | None = None,
            failure_reason: str = "compaction_failed",
    ) -> tuple[List[ModelMessage], ContextHealth, ContextCompactionRecord]:
        if self.context_compactor is None:
            return messages, context_health, ContextCompactionRecord(
                compacted=False,
                reason="compactor_unavailable",
                source_model_request_id=model_request_id,
            )

        await emitter.emit(
            state,
            ExecutionEventType.CONTEXT_COMPACTION_STARTED,
            actor=agent.name,
            payload={
                "iteration": iteration,
                "model_profile_id": profile.id,
                "model_request_id": model_request_id,
                "reason": reason,
                "context_health": context_health.model_dump(mode="json"),
                **(extra_payload or {}),
            },
            metrics={
                "estimated_prompt_tokens": context_health.estimated_prompt_tokens,
                "estimated_total_context_tokens": context_health.estimated_total_context_tokens,
                "context_window": context_health.context_window or 0,
                "context_usage_ratio": context_health.usage_ratio or 0,
                "context_status": context_health.status,
                **(extra_metrics or {}),
            },
            agent_id=agent.id,
            task_id=task.id,
            model_request_id=model_request_id,
        )
        try:
            compaction_result = await self.context_compactor(
                workflow,
                task,
                agent,
                profile,
                execution,
                execution_input,
                state,
                messages,
                context_health,
                model_request_id,
            )
            compacted_messages = compaction_result.messages
            after_compaction_health = estimate_context_health(
                compacted_messages,
                model_profile=profile,
                reserved_completion_tokens=profile.max_tokens,
            )
            if reason != "context_health_threshold" or not compaction_result.record.reason:
                compaction_result.record.reason = reason
            compaction_result.record.metadata.update(extra_metadata or {})
            compaction_result.record.metadata["after_estimated_prompt_tokens"] = (
                after_compaction_health.estimated_prompt_tokens
            )
            compaction_result.record.metadata["context_status_after"] = after_compaction_health.status
            compaction_result.record.metadata["context_usage_ratio_after"] = after_compaction_health.usage_ratio
            self._annotate_compaction_state_messages(compacted_messages, compaction_result.record)
            compaction_event = await emitter.emit(
                state,
                ExecutionEventType.CONTEXT_COMPACTION_COMPLETED,
                actor=agent.name,
                payload={
                    "iteration": iteration,
                    "model_profile_id": profile.id,
                    "model_request_id": model_request_id,
                    "reason": reason,
                    "record": compaction_result.record.model_dump(mode="json"),
                    "context_health_before": context_health.model_dump(mode="json"),
                    "context_health_after": after_compaction_health.model_dump(mode="json"),
                    **(extra_payload or {}),
                },
                metrics={
                    "compacted": compaction_result.record.compacted,
                    "estimated_tokens_saved": compaction_result.record.estimated_tokens_saved,
                    "context_window": after_compaction_health.context_window or 0,
                    "context_usage_ratio": after_compaction_health.usage_ratio or 0,
                    "context_status": after_compaction_health.status,
                    **(extra_metrics or {}),
                },
                agent_id=agent.id,
                task_id=task.id,
                model_request_id=model_request_id,
            )
            await record_context_compaction_snapshot(
                emitter.store,
                execution_id=execution.id,
                record=compaction_result.record,
                agent_id=agent.id,
                task_id=task.id,
                event_id=compaction_event.id,
            )
            self._record_compaction_state(
                state=state,
                record=compaction_result.record,
                reason=reason,
                iteration=iteration,
                model_request_id=model_request_id,
                agent_id=agent.id,
                task_id=task.id,
                context_health_before=context_health,
                context_health_after=after_compaction_health,
            )
            if compaction_result.record.compacted:
                await self._record_context_health(
                    agent=agent,
                    task=task,
                    profile=profile,
                    state=state,
                    emitter=emitter,
                    execution=execution,
                    context_health=after_compaction_health,
                    iteration=iteration,
                    model_request_id=model_request_id,
                    after_compaction=True,
                    compaction_reason=reason,
                )
                await self._maybe_retrieve_graph_context_after_context_compaction(
                    workflow=workflow,
                    task=task,
                    agent=agent,
                    execution=execution,
                    state=state,
                    emitter=emitter,
                    record=compaction_result.record,
                    context_health=after_compaction_health,
                )
                return compacted_messages, after_compaction_health, compaction_result.record
            return messages, context_health, compaction_result.record
        except Exception as exc:
            failure_record = ContextCompactionRecord(
                compacted=False,
                reason=failure_reason,
                source_model_request_id=model_request_id,
                metadata={"error": str(exc), **(extra_metadata or {})},
            )
            failed_event = await emitter.emit(
                state,
                ExecutionEventType.CONTEXT_COMPACTION_FAILED,
                actor=agent.name,
                payload={
                    "iteration": iteration,
                    "model_profile_id": profile.id,
                    "model_request_id": model_request_id,
                    "reason": reason,
                    "error": str(exc),
                    "record": failure_record.model_dump(mode="json"),
                    **(extra_payload or {}),
                },
                agent_id=agent.id,
                task_id=task.id,
                model_request_id=model_request_id,
            )
            await record_context_compaction_snapshot(
                emitter.store,
                execution_id=execution.id,
                record=failure_record,
                agent_id=agent.id,
                task_id=task.id,
                event_id=failed_event.id,
            )
            return messages, context_health, failure_record

    async def _request_model_response(
            self,
            *,
            agent: AgentDefinition,
            task: TaskDefinition,
            profile: ModelProfileDefinition,
            model_client: BaseModelClient,
            state: NativeExecutionState,
            emitter: ExecutionEventEmitter,
            messages: List[ModelMessage],
            tool_payload: list[dict[str, Any]],
            context_health: ContextHealth,
            iteration: int,
            model_request_id: str,
            log_prompts: bool,
            force_empty_tools: bool = False,
            attempt: int = 1,
            retry_after_compaction: bool = False,
    ):
        counted_input_tokens = model_client.count_tokens(messages) or context_health.estimated_prompt_tokens
        model_tools = tool_payload if tool_payload else ([] if force_empty_tools else None)
        await emitter.emit(
            state,
            ExecutionEventType.LLM_REQUEST_CREATED,
            actor=agent.name,
            payload={
                "iteration": iteration,
                "model_profile_id": profile.id,
                "message_count": len(messages),
                "attempt": attempt,
                "retry_after_compaction": retry_after_compaction,
                "messages": [asdict(message) for message in messages] if log_prompts else "[PROMPT_LOGGING_DISABLED]",
            },
            metrics={
                "model_provider": profile.provider,
                "model_name": profile.model,
                "input_tokens": counted_input_tokens,
                "prompt_tokens": counted_input_tokens,
                "estimated_prompt_tokens": context_health.estimated_prompt_tokens,
                "reserved_completion_tokens": context_health.reserved_completion_tokens,
                "context_window": context_health.context_window or 0,
                "context_usage_ratio": context_health.usage_ratio or 0,
                "context_status": context_health.status,
            },
            agent_id=agent.id,
            task_id=task.id,
            model_request_id=model_request_id,
        )

        if hasattr(model_client, "agenerate_text"):
            return await model_client.agenerate_text(
                messages,
                temperature=profile.temperature,
                max_tokens=profile.max_tokens,
                tools=model_tools,
            )
        return await asyncio.to_thread(
            model_client.generate_text,
            messages,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            tools=model_tools,
        )

    @staticmethod
    def _approval_continuation(execution: Execution, task: TaskDefinition) -> dict[str, Any] | None:
        output_payload = execution.output_payload if isinstance(execution.output_payload, dict) else {}
        checkpoint = output_payload.get("checkpoint")
        if not isinstance(checkpoint, dict):
            return None
        continuation = checkpoint.get(APPROVAL_CONTINUATION_KEY)
        if not isinstance(continuation, dict) or continuation.get("task_id") != task.id:
            return None
        return continuation

    async def _persist_approval_continuation(
            self,
            *,
            execution: Execution,
            state: NativeExecutionState,
            task: TaskDefinition,
            agent: AgentDefinition,
            iteration: int,
            messages: List[ModelMessage],
            tool_calls: list[tuple[ModelToolCall, str]],
            pending_tool_index: int,
            pending_context_persisted: bool = True,
    ) -> None:
        store = self.tool_executor.approval_manager.execution_store
        if store is None:
            raise RuntimeError("Durable approval continuation requires an execution store.")
        payload = dict(execution.output_payload or {})
        payload["node_outputs"] = dict(state.node_outputs)
        checkpoint = dict(payload.get("checkpoint") or {})
        checkpoint.update({
            "current_node_id": state.current_node_id,
            "current_task_id": state.current_task_id,
            "completed_node_ids": list(state.node_outputs.keys()),
            "node_outcomes": dict(state.node_outcomes),
            "node_errors": dict(state.node_errors),
            # Tool-call evidence must survive an approval pause so delivery
            # contracts cannot be bypassed or falsely failed after resume.
            "task_tool_results": _json_safe(state.task_tool_results),
            "planned_node_ids": list(state.planned_node_ids),
            "terminal_node_ids": list(state.terminal_node_ids),
            APPROVAL_CONTINUATION_KEY: {
                "version": 1,
                "task_id": task.id,
                "agent_id": agent.id,
                "iteration": iteration,
                "pending_tool_index": pending_tool_index,
                "pending_context_persisted": pending_context_persisted,
                "messages": _json_safe([asdict(message) for message in messages]),
                "tool_calls": _json_safe([
                    {"id": call_id, "name": tool_call.name, "arguments": tool_call.arguments}
                    for tool_call, call_id in tool_calls
                ]),
            },
        })
        payload["checkpoint"] = checkpoint
        execution.output_payload = payload
        await store.update_execution(execution)

    async def _execute_tool_calls(
            self,
            *,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            profile: ModelProfileDefinition,
            execution: Execution,
            state: NativeExecutionState,
            emitter: ExecutionEventEmitter,
            messages: List[ModelMessage],
            tool_calls: list[tuple[ModelToolCall, str]],
            context_health: ContextHealth,
            budget_statuses: list[TokenBudgetStatus],
            iteration: int,
            start_index: int = 0,
            first_context_already_persisted: bool = False,
    ) -> List[ModelMessage]:
        for tool_index in range(start_index, len(tool_calls)):
            tool_call, call_id = tool_calls[tool_index]
            resolved_tool = None
            try:
                resolved_tool = self.tool_executor.resolve_tool(
                    workflow,
                    call_id,
                    tool_name=tool_call.name,
                )
            except Exception:
                resolved_tool = None

            skip_context = first_context_already_persisted and tool_index == start_index
            if not skip_context:
                proposal_context_entry = await self._maybe_retrieve_graph_context_before_proposal_tool(
                    workflow=workflow,
                    task=task,
                    agent=agent,
                    execution=execution,
                    state=state,
                    emitter=emitter,
                    tool=resolved_tool,
                    arguments=tool_call.arguments,
                    tool_call_id=call_id,
                    context_health=context_health,
                    budget_statuses=budget_statuses,
                )
                if proposal_context_entry:
                    graph_context_message = _graph_context_message(
                        proposal_context_entry,
                        trigger="proposal_tool",
                    )
                    projected_health = estimate_context_health(
                        [*messages, graph_context_message],
                        model_profile=profile,
                        reserved_completion_tokens=profile.max_tokens,
                    )
                    if projected_health.status in {"critical", "overflow"}:
                        await emitter.emit(
                            state,
                            ExecutionEventType.AGENT_MESSAGE_CREATED,
                            actor=agent.name,
                            payload={
                                "source": "runtime_graph_context",
                                "trigger": "proposal_tool",
                                "status": "skipped",
                                "reason": "projected_context_health_guard",
                                "skip_reason": "graph_context_would_exceed_context_health",
                                "context_status": projected_health.status,
                                "context_usage_ratio": projected_health.usage_ratio,
                                "remaining_context_tokens": projected_health.remaining_context_tokens,
                                "tool_id": resolved_tool.id if resolved_tool is not None else None,
                                "tool_name": resolved_tool.name if resolved_tool is not None else tool_call.name,
                                "tool_call_id": call_id,
                            },
                            metadata={"runtime_graph_context": True, "auto_retrieval": True, "skipped": True},
                            agent_id=agent.id,
                            task_id=task.id,
                        )
                    else:
                        messages.append(graph_context_message)

            requires_approval = bool(
                resolved_tool is not None and resolved_tool.security.approval_required
            )
            if requires_approval:
                # Persist immediately before entering the approval boundary.
                # Earlier tool results are already in messages, so resume starts
                # at this exact call instead of replaying prior side effects.
                await self._persist_approval_continuation(
                    execution=execution,
                    state=state,
                    task=task,
                    agent=agent,
                    iteration=iteration,
                    messages=messages,
                    tool_calls=tool_calls,
                    pending_tool_index=tool_index,
                )

            tool_result = await self.tool_executor.execute(
                workflow,
                state,
                emitter,
                tool_id=call_id,
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
            )
            state.task_tool_results.setdefault(task.id, []).append(
                {
                    "tool_id": resolved_tool.id if resolved_tool is not None else tool_call.name,
                    "result": tool_result,
                }
            )
            state.memory_entries.append(
                {"tool_name": tool_call.name, "arguments": tool_call.arguments, "output": tool_result}
            )
            messages.append(
                ModelMessage(
                    role="tool",
                    content=json.dumps(tool_result, default=str),
                    name=tool_call.name,
                    tool_call_id=call_id,
                )
            )
            if requires_approval:
                current = await self.tool_executor.approval_manager.execution_store.get_execution(execution.id)
                if current is not None:
                    # Merge lifecycle fields changed by the approval request
                    # before advancing the durable transcript past the side effect.
                    execution.metadata = dict(current.metadata or {})
                    execution.input_payload = dict(current.input_payload or {})
                    execution.status = current.status
                    execution.worker_id = current.worker_id
                    execution.last_heartbeat_at = current.last_heartbeat_at
                await self._persist_approval_continuation(
                    execution=execution,
                    state=state,
                    task=task,
                    agent=agent,
                    iteration=iteration,
                    messages=messages,
                    tool_calls=tool_calls,
                    pending_tool_index=tool_index + 1,
                    pending_context_persisted=False,
                )
        return messages

    async def execute_task(
            self,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            profile: ModelProfileDefinition,
            model_client: BaseModelClient,
            state: NativeExecutionState,
            emitter: ExecutionEventEmitter,
            execution: Execution,
            execution_input: Dict[str, Any],
    ) -> Tuple[Any, List[ModelMessage]]:
        messages = await self._build_messages(workflow, task, agent, execution, execution_input, state)
        max_iterations = int(agent.framework_hints.adapter_config.get("max_iterations", 5))
        tool_payload = await self._tool_payload(workflow, agent, task)
        token_budget_policy = resolve_token_budget_policy(
            workflow=workflow,
            task=task,
            agent=agent,
            execution=execution,
        )

        first_iteration = 1
        structured_output_repair = False
        finalization_grace = False
        finalization_prompt_added = False
        continuation = self._approval_continuation(execution, task)
        if continuation is not None:
            persisted_messages = continuation.get("messages")
            persisted_tool_calls = continuation.get("tool_calls")
            if not isinstance(persisted_messages, list) or not isinstance(persisted_tool_calls, list):
                raise ValueError("Approval continuation is missing its persisted agent transcript.")
            messages = [
                _model_message_from_payload(item)
                for item in persisted_messages
                if isinstance(item, dict)
            ]
            resumed_tool_calls = [
                (
                    ModelToolCall(
                        id=item.get("id"),
                        name=str(item.get("name") or ""),
                        arguments=dict(item.get("arguments") or {}),
                    ),
                    str(item.get("id") or f"tool-call-{uuid4()}"),
                )
                for item in persisted_tool_calls
                if isinstance(item, dict)
            ]
            resumed_iteration = max(1, int(continuation.get("iteration") or 1))
            pending_tool_index = max(0, int(continuation.get("pending_tool_index") or 0))
            resumed_context_health = estimate_context_health(
                messages,
                model_profile=profile,
                reserved_completion_tokens=profile.max_tokens,
            )
            messages = await self._execute_tool_calls(
                workflow=workflow,
                task=task,
                agent=agent,
                profile=profile,
                execution=execution,
                state=state,
                emitter=emitter,
                messages=messages,
                tool_calls=resumed_tool_calls,
                context_health=resumed_context_health,
                budget_statuses=[],
                iteration=resumed_iteration,
                start_index=pending_tool_index,
                first_context_already_persisted=continuation.get("pending_context_persisted") is not False,
            )
            first_iteration = resumed_iteration + 1

        # Allow one tool-free finalization turn after the ordinary loop uses its
        # last iteration for a tool call, plus one schema-only repair turn.
        # Neither turn receives tools, preventing follow-up formatting or finalization
        # from replaying side effects.
        for iteration in range(first_iteration, max_iterations + 2):
            if iteration > max_iterations and not structured_output_repair and not finalization_grace:
                break
            self._assert_not_interrupted(state)
            model_request_id = str(uuid4())
            log_prompts = bool(workflow.metadata.get("log_prompts", True))
            context_health = estimate_context_health(
                messages,
                model_profile=profile,
                reserved_completion_tokens=profile.max_tokens,
            )
            await self._record_context_health(
                agent=agent,
                task=task,
                profile=profile,
                state=state,
                emitter=emitter,
                execution=execution,
                context_health=context_health,
                iteration=iteration,
                model_request_id=model_request_id,
            )
            if context_health.status in {"critical", "overflow"} and self.context_compactor is not None:
                messages, context_health, _ = await self._compact_context(
                    workflow=workflow,
                    task=task,
                    agent=agent,
                    profile=profile,
                    execution=execution,
                    execution_input=execution_input,
                    state=state,
                    emitter=emitter,
                    messages=messages,
                    context_health=context_health,
                    iteration=iteration,
                    model_request_id=model_request_id,
                    reason="context_health_threshold",
                    failure_reason="compaction_failed",
                )

            if finalization_grace and not finalization_prompt_added:
                messages.append(
                    ModelMessage(role="user", content="The tool budget is exhausted. Write the final structured output now; do not call any tools.")
                )
                finalization_prompt_added = True
            try:
                response = await self._request_model_response(
                    agent=agent,
                    task=task,
                    profile=profile,
                    model_client=model_client,
                    state=state,
                    emitter=emitter,
                    messages=messages,
                    tool_payload=[] if structured_output_repair or finalization_grace else tool_payload,
                    context_health=context_health,
                    iteration=iteration,
                    model_request_id=model_request_id,
                    log_prompts=log_prompts,
                    force_empty_tools=finalization_grace,
                )
            except Exception as exc:
                if isinstance(exc, ModelFallbackExhaustedError):
                    await emitter.emit(
                        state,
                        ExecutionEventType.MODEL_FALLBACK_FAILED,
                        actor=agent.name,
                        payload={
                            "iteration": iteration,
                            "model_profile_id": profile.id,
                            "model_request_id": model_request_id,
                            "primary_provider": profile.provider,
                            "primary_model": profile.model,
                            "attempts": exc.attempts,
                            "error": str(exc.last_error),
                        },
                        metrics={"fallback_attempt_count": len(exc.attempts)},
                        agent_id=agent.id,
                        task_id=task.id,
                        model_request_id=model_request_id,
                    )
                if not self._is_context_length_error(exc) or self.context_compactor is None:
                    raise
                messages, context_health, compaction_record = await self._compact_context(
                    workflow=workflow,
                    task=task,
                    agent=agent,
                    profile=profile,
                    execution=execution,
                    execution_input=execution_input,
                    state=state,
                    emitter=emitter,
                    messages=messages,
                    context_health=context_health,
                    iteration=iteration,
                    model_request_id=model_request_id,
                    reason="context_length_error",
                    extra_payload={"model_error": str(exc)},
                    extra_metadata={"model_error": str(exc)},
                    failure_reason="context_length_compaction_failed",
                )
                if not compaction_record.compacted:
                    raise
                response = await self._request_model_response(
                    agent=agent,
                    task=task,
                    profile=profile,
                    model_client=model_client,
                    state=state,
                    emitter=emitter,
                    messages=messages,
                    tool_payload=[] if structured_output_repair or finalization_grace else tool_payload,
                    context_health=context_health,
                    iteration=iteration,
                    model_request_id=model_request_id,
                    force_empty_tools=finalization_grace,
                    log_prompts=log_prompts,
                    attempt=2,
                    retry_after_compaction=True,
                )
            usage = normalize_token_usage(
                response.usage,
                provider=response.provider or profile.provider,
                model=response.model or profile.model,
                profile=profile,
                estimated_prompt_tokens=context_health.estimated_prompt_tokens,
                response_content=response.content,
            )
            fallback = usage.provider_usage.get("model_fallback")
            if isinstance(fallback, dict) and fallback.get("used") is True:
                await emitter.emit(
                    state,
                    ExecutionEventType.MODEL_FALLBACK_USED,
                    actor=agent.name,
                    payload={
                        "iteration": iteration,
                        "model_profile_id": profile.id,
                        "model_request_id": model_request_id,
                        **fallback,
                    },
                    metrics={
                        "fallback_index": fallback.get("fallback_index"),
                        "fallback_attempt_count": len(fallback.get("attempts") or []),
                    },
                    agent_id=agent.id,
                    task_id=task.id,
                    model_request_id=model_request_id,
                )

            await emitter.emit(
                state,
                ExecutionEventType.LLM_RESPONSE_CREATED,
                actor=agent.name,
                payload={
                    "iteration": iteration,
                    "content": response.content,
                    "tool_calls": [tool_call.name for tool_call in response.tool_calls],
                    "usage": response.usage,
                    "latency_ms": response.latency_ms,
                    "model_provider": response.provider or profile.provider,
                    "model_name": response.model or profile.model,
                },
                metrics={
                    "model_provider": response.provider or profile.provider,
                    "model_name": response.model or profile.model,
                    "latency_ms": response.latency_ms,
                    "input_tokens": usage.prompt_tokens,
                    "output_tokens": usage.completion_tokens,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "estimated_cost": usage.estimated_cost,
                    "token_usage_estimated": usage.estimated,
                },
                agent_id=agent.id,
                task_id=task.id,
                model_request_id=model_request_id,
            )
            token_event = await emitter.emit(
                state,
                ExecutionEventType.TOKEN_USAGE_RECORDED,
                actor=agent.name,
                payload={
                    "iteration": iteration,
                    "model_profile_id": profile.id,
                    "model_request_id": model_request_id,
                    "call_kind": "agent_task",
                    "usage": usage.model_dump(mode="json"),
                },
                metrics={
                    "model_provider": usage.provider or profile.provider,
                    "model_name": usage.model or profile.model,
                    "input_tokens": usage.prompt_tokens,
                    "output_tokens": usage.completion_tokens,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "estimated_cost": usage.estimated_cost,
                    "token_usage_estimated": usage.estimated,
                },
                agent_id=agent.id,
                task_id=task.id,
                model_request_id=model_request_id,
            )
            _, budget_statuses = await record_token_usage_snapshot(
                emitter.store,
                execution_id=execution.id,
                usage=usage,
                policy=token_budget_policy,
                agent_id=agent.id,
                task_id=task.id,
                workflow_id=workflow.id,
                model_request_id=model_request_id,
                event_id=token_event.id,
            )
            for budget_status in budget_statuses:
                await emitter.emit(
                    state,
                    (
                        ExecutionEventType.TOKEN_BUDGET_EXCEEDED
                        if budget_status.status == "exceeded"
                        else ExecutionEventType.TOKEN_BUDGET_WARNING
                    ),
                    actor=agent.name,
                    payload={
                        "iteration": iteration,
                        "model_profile_id": profile.id,
                        "model_request_id": model_request_id,
                        "budget": budget_status.model_dump(mode="json"),
                        "policy": (
                            token_budget_policy.model_dump(mode="json")
                            if token_budget_policy is not None
                            else None
                        ),
                    },
                    metrics={
                        "used_tokens": budget_status.used_tokens,
                        "budget_tokens": budget_status.budget_tokens,
                        "usage_ratio": budget_status.usage_ratio,
                    },
                    agent_id=agent.id,
                    task_id=task.id,
                    model_request_id=model_request_id,
                )
            messages = await self._enforce_token_budget_action(
                workflow=workflow,
                task=task,
                agent=agent,
                profile=profile,
                execution=execution,
                execution_input=execution_input,
                state=state,
                emitter=emitter,
                messages=messages,
                context_health=context_health,
                token_budget_policy=token_budget_policy,
                budget_statuses=budget_statuses,
                iteration=iteration,
                model_request_id=model_request_id,
                allow_compaction=False,
            )

            if response.tool_calls:
                if finalization_grace:
                    raise MaxIterationsReachedError(f"Max iterations reached for task '{task.id}'")
                normalized_tool_calls = [
                    (tool_call, tool_call.id or f"tool-call-{uuid4()}")
                    for tool_call in response.tool_calls
                ]
                messages.append(
                    ModelMessage(
                        role="assistant",
                        content=response.content,
                        name=agent.name,
                        tool_calls=[
                            ModelToolCall(
                                id=call_id,
                                name=tool_call.name,
                                arguments=tool_call.arguments,
                            )
                            for tool_call, call_id in normalized_tool_calls
                        ],
                    )
                )
                if iteration >= max_iterations and not structured_output_repair:
                    finalization_grace = True
                if response.content:
                    await emitter.emit(
                        state,
                        ExecutionEventType.AGENT_MESSAGE_CREATED,
                        actor=agent.name,
                        payload={"iteration": iteration, "content": response.content},
                        agent_id=agent.id,
                        task_id=task.id,
                    )
                messages = await self._execute_tool_calls(
                    workflow=workflow,
                    task=task,
                    agent=agent,
                    profile=profile,
                    execution=execution,
                    state=state,
                    emitter=emitter,
                    messages=messages,
                    tool_calls=normalized_tool_calls,
                    context_health=context_health,
                    budget_statuses=budget_statuses,
                    iteration=iteration,
                )
                if token_budget_policy is not None and token_budget_policy.action == "compact_context":
                    messages = await self._enforce_token_budget_action(
                        workflow=workflow,
                        task=task,
                        agent=agent,
                        profile=profile,
                        execution=execution,
                        execution_input=execution_input,
                        state=state,
                        emitter=emitter,
                        messages=messages,
                        context_health=context_health,
                        token_budget_policy=token_budget_policy,
                        budget_statuses=budget_statuses,
                        iteration=iteration,
                        model_request_id=model_request_id,
                        allow_compaction=True,
                    )
                continue
            if response.content:
                messages.append(ModelMessage(role="assistant", content=response.content, name=agent.name))
                await emitter.emit(
                    state,
                    ExecutionEventType.AGENT_MESSAGE_CREATED,
                    actor=agent.name,
                    payload={"iteration": iteration, "content": response.content},
                    agent_id=agent.id,
                    task_id=task.id,
                )

            try:
                return _structured_model_output(response.content, task.output_schema), messages
            except (JSONSchemaValidationError, json.JSONDecodeError) as exc:
                if structured_output_repair:
                    raise
                messages.append(
                    ModelMessage(
                        role="user",
                        content=_structured_output_repair_message(task.output_schema, exc),
                    )
                )
                structured_output_repair = True
                continue

        raise MaxIterationsReachedError(f"Max iterations reached for task '{task.id}'")

    async def _enforce_token_budget_action(
            self,
            *,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            profile: ModelProfileDefinition,
            execution: Execution,
            execution_input: Dict[str, Any],
            state: NativeExecutionState,
            emitter: ExecutionEventEmitter,
            messages: List[ModelMessage],
            context_health: ContextHealth,
            token_budget_policy: TokenBudgetPolicy | None,
            budget_statuses: list[TokenBudgetStatus],
            iteration: int,
            model_request_id: str,
            allow_compaction: bool,
    ) -> List[ModelMessage]:
        exceeded = next((item for item in budget_statuses if item.status == "exceeded"), None)
        if exceeded is None or token_budget_policy is None or token_budget_policy.action == "warn_only":
            return messages

        if token_budget_policy.action == "pause_execution":
            state.paused = True
            raise ExecutionPausedError(
                f"Token budget {exceeded.scope} exceeded: {exceeded.used_tokens}/{exceeded.budget_tokens} tokens."
            )

        if token_budget_policy.action == "fail_execution":
            raise RuntimeError(
                f"Token budget {exceeded.scope} exceeded: {exceeded.used_tokens}/{exceeded.budget_tokens} tokens."
            )

        if token_budget_policy.action != "compact_context" or not allow_compaction or self.context_compactor is None:
            return messages

        compacted_messages, _, compaction_record = await self._compact_context(
            workflow=workflow,
            task=task,
            agent=agent,
            profile=profile,
            execution=execution,
            execution_input=execution_input,
            state=state,
            emitter=emitter,
            messages=messages,
            context_health=context_health,
            iteration=iteration,
            model_request_id=model_request_id,
            reason="budget_exceeded",
            extra_payload={
                "budget": exceeded.model_dump(mode="json"),
                "policy": token_budget_policy.model_dump(mode="json"),
            },
            extra_metadata={
                "budget": exceeded.model_dump(mode="json"),
            },
            extra_metrics={
                "used_tokens": exceeded.used_tokens,
                "budget_tokens": exceeded.budget_tokens,
                "usage_ratio": exceeded.usage_ratio,
            },
            failure_reason="budget_compaction_failed",
        )
        return compacted_messages if compaction_record.compacted else messages
