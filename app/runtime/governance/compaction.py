from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.core.time import utc_now
from app.domain import (
    AgentDefinition,
    ContextCompactionRecord,
    ContextHealth,
    Execution,
    MemoryScope,
    MemoryType,
    ModelProfileDefinition,
    TaskDefinition,
    WorkflowDefinition,
)
from app.llm.base import ModelMessage
from app.runtime.native.state import NativeExecutionState
from .context_health import estimate_model_messages_tokens, estimate_text_tokens

COMPACTION_SUMMARY_HEADER = "Runtime Context Compaction Summary"
COMPACTION_STATE_HEADER = "Runtime Context Compaction State"
COMPACTION_STRATEGY_VERSION = "deterministic-runtime-summary-v1"


@dataclass(slots=True)
class ContextCompactionResult:
    messages: list[ModelMessage]
    record: ContextCompactionRecord
    summary: str = ""


def _json_preview(value: Any, *, max_chars: int = 1000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()} ... [truncated {len(text) - max_chars} chars]"


def _governance_config(workflow: WorkflowDefinition) -> dict[str, Any]:
    config: dict[str, Any] = {}
    governance = workflow.metadata.get("runtime_governance")
    if isinstance(governance, dict) and isinstance(governance.get("context_compaction"), dict):
        config.update(governance["context_compaction"])
    direct = workflow.metadata.get("context_compaction")
    if isinstance(direct, dict):
        config.update(direct)
    return config


def _persist_context_pack_enabled(workflow: WorkflowDefinition) -> bool:
    config = _governance_config(workflow)
    if "persist_context_pack" in config:
        return bool(config.get("persist_context_pack"))
    return bool(get_settings().agent_context_compaction_persist_context_pack_default)


def _int_config(config: dict[str, Any], key: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _message_token_count(message: ModelMessage) -> int:
    return estimate_text_tokens(message.content) + 6


def _message_payload(message: ModelMessage) -> dict[str, Any]:
    if isinstance(message.content, dict):
        return message.content
    if isinstance(message.content, str):
        try:
            parsed = json.loads(message.content)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _text_contains_any(value: Any, needles: set[str]) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(needle in lowered for needle in needles)


def _protected_message_reason(message: ModelMessage) -> str | None:
    if message.role in {"system", "user"}:
        return f"{message.role}_message"

    metadata = message.metadata or {}
    governance = metadata.get("runtime_governance")
    if isinstance(governance, dict):
        if governance.get("protected_from_compaction") is True:
            return str(governance.get("protected_reason") or "runtime_governance")
        if governance.get("pending_approval") or governance.get("pending_human_input"):
            return "pending_human_decision"
        if governance.get("unresolved_tool_error") or governance.get("tool_error"):
            return "unresolved_tool_error"

    if metadata.get("protected_from_compaction") is True:
        return str(metadata.get("protected_reason") or "metadata")
    if metadata.get("pending_approval") or metadata.get("pending_human_input"):
        return "pending_human_decision"
    if metadata.get("unresolved_tool_error") or metadata.get("tool_error"):
        return "unresolved_tool_error"
    if str(metadata.get("approval_status", "")).lower() == "pending":
        return "pending_human_decision"

    payload = _message_payload(message)
    payload_status = str(payload.get("status", "")).lower()
    if (
            payload.get("pending_approval")
            or payload.get("approval_required")
            or payload.get("approval_request_id")
            or payload_status in {"pending_approval", "waiting_for_approval", "approval_required"}
    ):
        return "pending_human_decision"
    if (
            payload.get("error")
            or payload.get("exception")
            or payload.get("tool_error")
            or payload_status in {"error", "failed", "failure"}
            or payload.get("success") is False
    ):
        return "unresolved_tool_error"

    if _text_contains_any(message.content, {"pending approval", "approval required", "waiting for approval"}):
        return "pending_human_decision"
    if _text_contains_any(message.content, {"tool failed", "tool error", "traceback", "exception"}):
        return "unresolved_tool_error"
    return None


def _protected_message_indices(messages: list[ModelMessage]) -> dict[int, str]:
    protected: dict[int, str] = {}
    for index, message in enumerate(messages):
        reason = _protected_message_reason(message)
        if reason:
            protected[index] = reason
    return protected


def _compactable_message_indices(messages: list[ModelMessage], config: dict[str, Any]) -> list[int]:
    assistant_or_tool = [index for index, message in enumerate(messages) if message.role in {"assistant", "tool"}]
    if not assistant_or_tool:
        return []

    protected_indices = set(_protected_message_indices(messages))
    recent_count = _int_config(config, "preserve_recent_messages", 1, minimum=0, maximum=10)
    oversized_threshold = _int_config(config, "oversized_message_tokens", 600, minimum=50)
    recent_indices = set(assistant_or_tool[-recent_count:]) if recent_count else set()
    compactable: list[int] = []
    for index in assistant_or_tool:
        if index in protected_indices:
            continue
        if index not in recent_indices:
            compactable.append(index)
            continue
        if _message_token_count(messages[index]) >= oversized_threshold:
            compactable.append(index)
    return compactable


def _leading_system_count(messages: list[ModelMessage]) -> int:
    count = 0
    for message in messages:
        if message.role != "system":
            break
        count += 1
    return count


def _workflow_step_summary(workflow: WorkflowDefinition, state: NativeExecutionState) -> tuple[list[str], list[str]]:
    completed_node_ids = set(state.node_outputs)
    completed: list[str] = []
    pending: list[str] = []
    tasks_by_id = {task.id: task for task in workflow.task_definitions}
    for node in workflow.nodes:
        if node.node_type != "task":
            continue
        task = tasks_by_id.get(node.task_id or "")
        label = task.name if task is not None else node.name
        if node.id in completed_node_ids:
            completed.append(f"{label} ({node.id})")
        else:
            pending.append(f"{label} ({node.id})")
    return completed, pending


def _summarize_messages(messages: list[ModelMessage], compact_indices: list[int], *, max_item_chars: int) -> list[str]:
    summaries: list[str] = []
    for index in compact_indices:
        message = messages[index]
        label = message.role
        if message.name:
            label = f"{label}:{message.name}"
        if message.tool_call_id:
            label = f"{label} tool_call_id={message.tool_call_id}"
        summaries.append(f"- {label}: {_json_preview(message.content, max_chars=max_item_chars)}")
    return summaries


def _summarize_protected_messages(
        messages: list[ModelMessage],
        protected_indices: dict[int, str],
        *,
        max_item_chars: int,
) -> list[str]:
    summaries: list[str] = []
    for index, reason in protected_indices.items():
        message = messages[index]
        label = message.role
        if message.name:
            label = f"{label}:{message.name}"
        if message.tool_call_id:
            label = f"{label} tool_call_id={message.tool_call_id}"
        if message.role in {"system", "user"}:
            summaries.append(f"- {label} retained ({reason}): raw message preserved separately.")
            continue
        summaries.append(f"- {label} retained ({reason}): {_json_preview(message.content, max_chars=max_item_chars)}")
    return summaries


def _build_summary(
        *,
        workflow: WorkflowDefinition,
        task: TaskDefinition,
        agent: AgentDefinition,
        execution: Execution,
        execution_input: dict[str, Any],
        state: NativeExecutionState,
        messages: list[ModelMessage],
        compact_indices: list[int],
        protected_indices: dict[int, str],
        context_health: ContextHealth,
        max_chars: int,
) -> str:
    completed, pending = _workflow_step_summary(workflow, state)
    current_status = [
        f"Workflow: {workflow.name} ({workflow.id})",
        f"Agent: {agent.name} ({agent.id})",
        f"Current task: {task.name} ({task.id})",
        f"Context status before compaction: {context_health.status}",
    ]
    if context_health.usage_ratio is not None:
        current_status.append(f"Context usage ratio before compaction: {context_health.usage_ratio}")

    sections = [
        f"# {COMPACTION_SUMMARY_HEADER}",
        "The runtime compacted older or oversized assistant/tool context before this model call. Critical system instructions and the user request are preserved as separate messages.",
        "",
        "## User Objective",
        _json_preview({"task": task.description, "input": execution_input}, max_chars=1400),
        "",
        "## Current Workflow State",
        "\n".join(f"- {item}" for item in current_status),
        "",
        "## Completed Steps",
        "\n".join(f"- {item}" for item in completed) if completed else "- None recorded yet.",
        "",
        "## Pending Steps",
        "\n".join(f"- {item}" for item in pending) if pending else "- None recorded.",
        "",
        "## Important Tool Outputs And Agent Messages",
        "\n".join(_summarize_messages(messages, compact_indices, max_item_chars=700)) or "- None compacted.",
        "",
        "## Protected Context Retained",
        "\n".join(_summarize_protected_messages(messages, protected_indices, max_item_chars=500))
        or "- No protected prompt messages detected.",
        "",
        "## Prior Node Outputs",
        _json_preview(state.node_outputs, max_chars=1400) if state.node_outputs else "- None recorded yet.",
        "",
        "## Runtime Memory Entries",
        _json_preview(state.memory_entries, max_chars=1400) if state.memory_entries else "- None recorded yet.",
        "",
        "## Key Constraints",
        _json_preview(
            {
                "agent_role": agent.role,
                "agent_instructions": agent.instructions,
                "task_instructions": task.instructions,
                "expected_output": task.expected_output,
            },
            max_chars=1400,
        ),
        "",
        "## Errors Or Blockers",
        _json_preview(execution.error or execution.error_json,
                      max_chars=700) if execution.error else "- None recorded.",
    ]
    summary = "\n".join(sections).strip()
    if len(summary) <= max_chars:
        return summary
    return f"{summary[:max_chars].rstrip()}\n\n[Summary truncated to {max_chars} chars by runtime compaction.]"


def _annotate_compaction_state(result: ContextCompactionResult) -> None:
    if not result.record.compacted:
        return
    state_lines = [
        f"# {COMPACTION_STATE_HEADER}",
        "context_compacted=true",
        f"compaction_reason={result.record.reason or 'unknown'}",
        f"context_pack_memory_id={result.record.memory_id or 'none'}",
        f"source_model_request_id={result.record.source_model_request_id or 'unknown'}",
        f"estimated_tokens_saved={result.record.estimated_tokens_saved}",
        f"protected_context_retained={str(bool(result.record.metadata.get('protected_context_retained'))).lower()}",
    ]
    state_block = "\n".join(state_lines)
    for message in result.messages:
        if message.name != "runtime_context_compaction":
            continue
        governance = dict(message.metadata.get("runtime_governance") or {})
        governance.update(
            {
                "context_compacted": True,
                "compaction_reason": result.record.reason,
                "context_pack_memory_id": result.record.memory_id,
                "estimated_tokens_saved": result.record.estimated_tokens_saved,
            }
        )
        message.metadata["runtime_governance"] = governance
        content = str(message.content)
        if content.startswith(f"# {COMPACTION_STATE_HEADER}"):
            message.content = content
        else:
            message.content = f"{state_block}\n\n{content}"
        result.summary = str(message.content)
        break


def deterministic_compact_messages(
        *,
        workflow: WorkflowDefinition,
        task: TaskDefinition,
        agent: AgentDefinition,
        profile: ModelProfileDefinition,
        execution: Execution,
        execution_input: dict[str, Any],
        state: NativeExecutionState,
        messages: list[ModelMessage],
        context_health: ContextHealth,
        source_model_request_id: str,
) -> ContextCompactionResult:
    config = _governance_config(workflow)
    if config.get("enabled", True) is False:
        return ContextCompactionResult(
            messages=messages,
            record=ContextCompactionRecord(
                compacted=False,
                reason="disabled",
                source_model_request_id=source_model_request_id,
            ),
        )

    compact_indices = _compactable_message_indices(messages, config)
    protected_indices = _protected_message_indices(messages)
    if not compact_indices:
        return ContextCompactionResult(
            messages=messages,
            record=ContextCompactionRecord(
                compacted=False,
                reason="no_compactable_messages",
                source_model_request_id=source_model_request_id,
            ),
        )

    before_tokens = estimate_model_messages_tokens(messages)
    summary = _build_summary(
        workflow=workflow,
        task=task,
        agent=agent,
        execution=execution,
        execution_input=execution_input,
        state=state,
        messages=messages,
        compact_indices=compact_indices,
        protected_indices=protected_indices,
        context_health=context_health,
        max_chars=_int_config(config, "max_summary_chars", 5000, minimum=1200, maximum=20000),
    )
    summary_message = ModelMessage(
        role="system",
        content=summary,
        name="runtime_context_compaction",
        metadata={
            "runtime_governance": {
                "compacted": True,
                "strategy": COMPACTION_STRATEGY_VERSION,
                "source_model_request_id": source_model_request_id,
                "protected_context_retained": True,
                "protected_message_count": len(protected_indices),
            }
        },
    )
    compact_index_set = set(compact_indices)
    leading_systems = _leading_system_count(messages)
    compacted_messages = (
            messages[:leading_systems]
            + [summary_message]
            + [
                message
                for index, message in enumerate(messages[leading_systems:])
                if index + leading_systems not in compact_index_set
            ]
    )
    after_tokens = estimate_model_messages_tokens(compacted_messages)
    tokens_saved = max(before_tokens - after_tokens, 0)
    min_saved = _int_config(config, "min_estimated_tokens_saved", 50, minimum=0)
    if tokens_saved < min_saved:
        return ContextCompactionResult(
            messages=messages,
            record=ContextCompactionRecord(
                compacted=False,
                reason="insufficient_savings",
                source_model_request_id=source_model_request_id,
                estimated_tokens_saved=tokens_saved,
                metadata={
                    "before_estimated_prompt_tokens": before_tokens,
                    "after_estimated_prompt_tokens": after_tokens,
                    "min_estimated_tokens_saved": min_saved,
                    "strategy": COMPACTION_STRATEGY_VERSION,
                    "protected_context_retained": True,
                    "protected_message_count": len(protected_indices),
                    "protected_message_roles": [messages[index].role for index in protected_indices],
                    "protected_message_reasons": protected_indices,
                },
            ),
            summary=summary,
        )

    return ContextCompactionResult(
        messages=compacted_messages,
        record=ContextCompactionRecord(
            compacted=True,
            reason="context_health_threshold",
            source_model_request_id=source_model_request_id,
            estimated_tokens_saved=tokens_saved,
            metadata={
                "before_estimated_prompt_tokens": before_tokens,
                "after_estimated_prompt_tokens": after_tokens,
                "compacted_message_count": len(compact_indices),
                "preserved_message_count": len(compacted_messages),
                "context_status_before": context_health.status,
                "context_window": context_health.context_window,
                "context_usage_ratio_before": context_health.usage_ratio,
                "model_profile_id": profile.id,
                "strategy": COMPACTION_STRATEGY_VERSION,
                "protected_context_retained": True,
                "protected_message_count": len(protected_indices),
                "protected_message_roles": [messages[index].role for index in protected_indices],
                "protected_message_reasons": protected_indices,
            },
        ),
        summary=summary,
    )


@dataclass(slots=True)
class RuntimeContextCompactor:
    context: Any | None = None

    @staticmethod
    def _source_event_sequence_range(state: NativeExecutionState) -> dict[str, int]:
        end_sequence = max(int(state.sequence or 0) - 1, 0)
        start_sequence = 1 if end_sequence > 0 else 0
        return {
            "source_event_start_sequence": start_sequence,
            "source_event_end_sequence": end_sequence,
        }

    async def __call__(
            self,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            profile: ModelProfileDefinition,
            execution: Execution,
            execution_input: dict[str, Any],
            state: NativeExecutionState,
            messages: list[ModelMessage],
            context_health: ContextHealth,
            source_model_request_id: str,
    ) -> ContextCompactionResult:
        result = deterministic_compact_messages(
            workflow=workflow,
            task=task,
            agent=agent,
            profile=profile,
            execution=execution,
            execution_input=execution_input,
            state=state,
            messages=messages,
            context_health=context_health,
            source_model_request_id=source_model_request_id,
        )
        if result.record.compacted:
            result.record.metadata.update(self._source_event_sequence_range(state))
        if (
                result.record.compacted
                and self.context is not None
                and _persist_context_pack_enabled(workflow)
        ):
            memory_id = await self._persist_context_pack(
                workflow=workflow,
                task=task,
                agent=agent,
                execution=execution,
                result=result,
            )
            if memory_id:
                result.record.memory_id = memory_id
        _annotate_compaction_state(result)
        return result

    async def _persist_context_pack(
            self,
            *,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            result: ContextCompactionResult,
    ) -> str | None:
        if not result.summary.strip():
            return None
        try:
            from app.services.memory import MemoryService

            payload = {
                "scope": MemoryScope.WORKFLOW.value,
                "workflow_id": workflow.id,
                "agent_id": agent.id,
                "source": "runtime_context_compaction",
                "source_execution_id": execution.id,
                "memory_type": MemoryType.CONTEXT_PACK.value,
                "status": "active",
                "importance": 45,
                "summary": f"Runtime context compaction for {task.name}",
                "content": result.summary,
                "tags": ["context_pack", "runtime_compaction", "execution"],
                "metadata": {
                    "mode": "runtime_compaction",
                    "task_id": task.id,
                    "agent_id": agent.id,
                    "execution_id": execution.id,
                    "source_model_request_id": result.record.source_model_request_id,
                    "compacted": result.record.compacted,
                    "compaction_reason": result.record.reason,
                    "estimated_tokens_saved": result.record.estimated_tokens_saved,
                    "created_at": utc_now().isoformat(),
                    **result.record.metadata,
                },
            }
            created = await MemoryService(self.context).create_memory(payload, confirmed=True, trusted_actor=True)
            return created.id
        except Exception as exc:
            result.record.metadata["memory_persist_error"] = str(exc)
            return None
