from __future__ import annotations

from app.domain import ExecutionEvent, ExecutionEventType
from .runtime_event_models import (
    RuntimeEventActor,
    RuntimeEventLevel,
    RuntimeEventTask,
    RuntimeEventType,
    RuntimeEventWorkflow,
    RuntimeStreamEvent,
)


def _actor_for(event: ExecutionEvent) -> RuntimeEventActor | None:
    if not event.agent_id:
        return None
    return RuntimeEventActor(id=event.agent_id, name=event.actor)


def _workflow_for(event: ExecutionEvent) -> RuntimeEventWorkflow | None:
    if not event.workflow_id:
        return None
    return RuntimeEventWorkflow(id=event.workflow_id)


def _task_for(event: ExecutionEvent, *, progress: float | None = None) -> RuntimeEventTask | None:
    task_id = event.task_id or event.payload.get("task_id")
    if not task_id:
        return None
    return RuntimeEventTask(
        id=str(task_id),
        title=event.payload.get("task_name"),
        progress=progress,
    )


def _metadata_for(event: ExecutionEvent, **extra: object) -> dict[str, object]:
    metadata: dict[str, object] = {
        "executionId": event.execution_id,
        "executionEventId": event.id,
        "executionEventType": event.event_type.value,
        "sequence": event.sequence,
    }
    metadata.update({key: value for key, value in extra.items() if value is not None})
    return metadata


def _runtime_event(
        event: ExecutionEvent,
        runtime_type: RuntimeEventType,
        *,
        level: RuntimeEventLevel = RuntimeEventLevel.INFO,
        message: str | None = None,
        progress: float | None = None,
        metadata: dict[str, object] | None = None,
) -> RuntimeStreamEvent:
    return RuntimeStreamEvent(
        id=f"runtime:{event.id}:{runtime_type.value}",
        source="agency-runtime",
        sourceType="agency",
        type=runtime_type,
        timestamp=event.timestamp,
        actor=_actor_for(event),
        workflow=_workflow_for(event),
        task=_task_for(event, progress=progress),
        level=level,
        message=message,
        metadata=metadata or _metadata_for(event),
    )


def _log_message_for(event: ExecutionEvent) -> str:
    event_type = event.event_type
    if event_type == ExecutionEventType.LLM_REQUEST_CREATED:
        return f"Model request created for iteration {event.payload.get('iteration', '?')}"
    if event_type == ExecutionEventType.LLM_RESPONSE_CREATED:
        return f"Model response received for iteration {event.payload.get('iteration', '?')}"
    if event_type == ExecutionEventType.MODEL_FALLBACK_USED:
        return (
            f"Model fallback used: {event.payload.get('primary_provider')}:"
            f"{event.payload.get('primary_model')} -> {event.payload.get('fallback_provider')}:"
            f"{event.payload.get('fallback_model')}"
        )
    if event_type == ExecutionEventType.MODEL_FALLBACK_FAILED:
        return f"Model fallback failed: {event.payload.get('error') or 'all fallback attempts failed'}"
    if event_type == ExecutionEventType.TOKEN_USAGE_RECORDED:
        usage = event.payload.get("usage") if isinstance(event.payload.get("usage"), dict) else {}
        total_tokens = usage.get("total_tokens") or event.metrics.get("total_tokens") or 0
        return f"Token usage recorded: {total_tokens} total tokens"
    if event_type == ExecutionEventType.TOKEN_BUDGET_WARNING:
        return "Token budget warning"
    if event_type == ExecutionEventType.TOKEN_BUDGET_EXCEEDED:
        return "Token budget exceeded"
    if event_type == ExecutionEventType.CONTEXT_HEALTH_RECORDED:
        return f"Context health: {event.payload.get('status') or event.metrics.get('context_status') or 'unknown'}"
    if event_type == ExecutionEventType.CONTEXT_COMPACTION_STARTED:
        return "Context compaction started"
    if event_type == ExecutionEventType.CONTEXT_COMPACTION_COMPLETED:
        record = event.payload.get("record") if isinstance(event.payload.get("record"), dict) else {}
        if record.get("compacted"):
            return f"Context compaction completed: {record.get('estimated_tokens_saved') or 0} estimated tokens saved"
        return f"Context compaction skipped: {record.get('reason') or 'not needed'}"
    if event_type == ExecutionEventType.CONTEXT_COMPACTION_FAILED:
        return f"Context compaction failed: {event.payload.get('error') or 'unknown error'}"
    if event_type == ExecutionEventType.SUBAGENT_PROGRESS_UPDATED:
        if event.payload.get("blocker"):
            return f"Sub-agent blocked: {event.payload.get('blocker')}"
        if event.payload.get("clarification_needed"):
            return f"Sub-agent needs clarification: {event.payload.get('clarification_needed')}"
        status = event.payload.get("status") or event.payload.get("subagent_status") or "running"
        return f"Sub-agent progress: {status}"
    if event_type == ExecutionEventType.SUBAGENT_STEP_COMPLETED:
        return "Sub-agent step completed"
    if event_type == ExecutionEventType.SUBAGENT_STEP_FAILED:
        return f"Sub-agent step failed: {event.payload.get('error') or event.payload.get('blocker') or 'unknown error'}"
    if event_type == ExecutionEventType.SUBAGENT_NEEDS_INPUT:
        return f"Sub-agent needs input: {event.payload.get('clarification_needed') or event.payload.get('question') or 'input required'}"
    if event_type == ExecutionEventType.SUBAGENT_NEEDS_APPROVAL:
        return f"Sub-agent needs approval: {event.payload.get('approval_type') or event.payload.get('reason') or 'approval required'}"
    if event_type == ExecutionEventType.TOOL_CALL_STARTED:
        return f"Tool started: {event.payload.get('tool_name') or event.payload.get('tool_id') or 'tool'}"
    if event_type == ExecutionEventType.TOOL_CALL_COMPLETED:
        return f"Tool completed: {event.payload.get('tool_name') or event.payload.get('tool_id') or 'tool'}"
    if event_type == ExecutionEventType.TOOL_CALL_FAILED:
        return f"Tool failed: {event.payload.get('tool_name') or event.payload.get('tool_id') or 'tool'}"
    if event_type == ExecutionEventType.EXECUTION_FAILED:
        return f"Execution failed: {event.payload.get('error') or 'unknown error'}"
    if event_type == ExecutionEventType.EXECUTION_REPAIRED:
        return f"Execution repaired: {event.payload.get('repair_action') or 'stale execution repaired'}"
    if event_type == ExecutionEventType.EXECUTION_CYCLE_STARTED:
        return f"Monitor cycle {event.payload.get('cycle_number') or '?'} started"
    if event_type == ExecutionEventType.EXECUTION_CYCLE_COMPLETED:
        return f"Monitor cycle {event.payload.get('cycle_number') or '?'} completed"
    if event_type == ExecutionEventType.EXECUTION_CYCLE_FAILED:
        return f"Monitor cycle failed: {event.payload.get('error') or 'unknown error'}"
    if event_type == ExecutionEventType.EXECUTION_CYCLE_GUARD_TRIGGERED:
        return f"Monitor cycle paused: {event.payload.get('reason') or 'loop guard triggered'}"
    if event_type == ExecutionEventType.EXECUTION_WAITING:
        return f"Execution waiting for {event.payload.get('kind') or 'wake condition'}"
    if event_type == ExecutionEventType.EXECUTION_WOKEN:
        return f"Execution wait {event.payload.get('status') or 'resolved'}"
    if event_type == ExecutionEventType.MONITOR_FINDING_CREATED:
        return f"Monitor finding: {event.payload.get('category') or 'finding'}"
    if event_type == ExecutionEventType.MONITOR_EVALUATION_RECORDED:
        verdict = event.payload.get("verdict")
        summary = verdict.get("summary") if isinstance(verdict, dict) else None
        return f"Monitor recorded Evaluation-agent review: {summary or 'advisory verdict'}"
    if event_type == ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED:
        return "Monitor proposed a workflow improvement"
    if event_type == ExecutionEventType.MONITOR_IMPROVEMENT_COMPARED:
        return f"Monitor compared improvement outcome: {event.payload.get('outcome') or 'unknown'}"
    if event_type == ExecutionEventType.SUPERVISOR_STEERING_REQUESTED:
        return f"Supervisor steering requested: {event.payload.get('recommended_action') or 'review'}"
    if event_type == ExecutionEventType.SUPERVISOR_STEERING_APPLIED:
        return f"Supervisor steering applied: {event.payload.get('applied_action') or event.payload.get('recommended_action') or 'steering'}"
    return event_type.value


def map_execution_event_to_runtime_events(event: ExecutionEvent) -> list[RuntimeStreamEvent]:
    runtime_events: list[RuntimeStreamEvent] = []

    if event.event_type == ExecutionEventType.TASK_STARTED:
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.AGENT_STATUS_CHANGED,
                message=f"{event.actor or 'Agent'} started {event.payload.get('task_name') or 'a task'}",
                metadata=_metadata_for(event, status="working", visualAction="working"),
            )
        )
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.TASK_STARTED,
                message=event.payload.get("task_name") or "Task started",
                progress=0,
            )
        )
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.TASK_PROGRESS,
                message="Task started",
                progress=0,
                metadata=_metadata_for(event, phase="started"),
            )
        )

    if event.event_type == ExecutionEventType.LLM_RESPONSE_CREATED:
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.TASK_PROGRESS,
                message="Model response received",
                progress=0.55,
                metadata=_metadata_for(event, phase="model_response", iteration=event.payload.get("iteration")),
            )
        )

    if event.event_type == ExecutionEventType.CONTEXT_HEALTH_RECORDED:
        level = (
            RuntimeEventLevel.WARNING
            if event.payload.get("status") in {"warning", "critical", "overflow"}
            else RuntimeEventLevel.INFO
        )
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.TASK_PROGRESS,
                level=level,
                message=_log_message_for(event),
                progress=0.35,
                metadata=_metadata_for(
                    event,
                    phase="context_health",
                    contextStatus=event.payload.get("status"),
                    contextUsageRatio=event.payload.get("usage_ratio"),
                ),
            )
        )

    if event.event_type in {
        ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
        ExecutionEventType.SUBAGENT_STEP_COMPLETED,
        ExecutionEventType.SUBAGENT_STEP_FAILED,
        ExecutionEventType.SUBAGENT_NEEDS_INPUT,
        ExecutionEventType.SUBAGENT_NEEDS_APPROVAL,
    }:
        progress_percent = event.payload.get("progress_percent") or event.payload.get("percent")
        progress = None
        if isinstance(progress_percent, int | float):
            progress = max(min(float(progress_percent) / 100, 1), 0)
        level = RuntimeEventLevel.ERROR if event.event_type == ExecutionEventType.SUBAGENT_STEP_FAILED else (
            RuntimeEventLevel.WARNING
            if event.event_type in {ExecutionEventType.SUBAGENT_NEEDS_INPUT, ExecutionEventType.SUBAGENT_NEEDS_APPROVAL}
               or event.payload.get("blocker")
               or event.payload.get("clarification_needed")
            else RuntimeEventLevel.SUCCESS
            if event.event_type == ExecutionEventType.SUBAGENT_STEP_COMPLETED
            else RuntimeEventLevel.INFO
        )
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.TASK_PROGRESS,
                level=level,
                message=_log_message_for(event),
                progress=progress,
                metadata=_metadata_for(
                    event,
                    phase="subagent_status",
                    subagentStatus=event.payload.get("status") or event.payload.get("subagent_status"),
                    confidence=event.payload.get("confidence"),
                ),
            )
        )

    if event.event_type in {ExecutionEventType.TOKEN_BUDGET_WARNING, ExecutionEventType.TOKEN_BUDGET_EXCEEDED}:
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.TASK_PROGRESS,
                level=RuntimeEventLevel.WARNING,
                message=_log_message_for(event),
                progress=0.6,
                metadata=_metadata_for(event, phase="token_budget"),
            )
        )

    if event.event_type == ExecutionEventType.SUPERVISOR_STEERING_REQUESTED:
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.TASK_PROGRESS,
                level=RuntimeEventLevel.WARNING,
                message=_log_message_for(event),
                progress=0.65,
                metadata=_metadata_for(
                    event,
                    phase="supervisor_steering",
                    recommendedAction=event.payload.get("recommended_action"),
                    findingCategory=event.payload.get("category"),
                    steeringStatus=event.payload.get("status"),
                ),
            )
        )

    if event.event_type in {
        ExecutionEventType.CONTEXT_COMPACTION_STARTED,
        ExecutionEventType.CONTEXT_COMPACTION_COMPLETED,
        ExecutionEventType.CONTEXT_COMPACTION_FAILED,
    }:
        level = (
            RuntimeEventLevel.ERROR
            if event.event_type == ExecutionEventType.CONTEXT_COMPACTION_FAILED
            else RuntimeEventLevel.INFO
        )
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.TASK_PROGRESS,
                level=level,
                message=_log_message_for(event),
                progress=0.4,
                metadata=_metadata_for(event, phase="context_compaction"),
            )
        )

    if event.event_type == ExecutionEventType.TOOL_CALL_COMPLETED:
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.TASK_PROGRESS,
                message="Tool completed",
                progress=0.75,
                metadata=_metadata_for(event, phase="tool_completed", toolName=event.payload.get("tool_name")),
            )
        )

    if event.event_type == ExecutionEventType.AGENT_MESSAGE_CREATED:
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.AGENT_STATUS_CHANGED,
                message="Agent produced a message",
                metadata=_metadata_for(event, status="speaking", visualAction="talk"),
            )
        )
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.AGENT_SPOKE,
                message=str(event.payload.get("content") or ""),
                metadata=_metadata_for(event, iteration=event.payload.get("iteration")),
            )
        )

    if event.event_type == ExecutionEventType.EXECUTION_COMPLETED:
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.AGENT_STATUS_CHANGED,
                level=RuntimeEventLevel.SUCCESS,
                message="Execution completed",
                metadata=_metadata_for(event, status="complete", visualAction="idle"),
            )
        )
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.TASK_COMPLETED,
                level=RuntimeEventLevel.SUCCESS,
                message="Task completed",
                progress=1,
            )
        )

    if event.event_type == ExecutionEventType.EXECUTION_FAILED:
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.AGENT_STATUS_CHANGED,
                level=RuntimeEventLevel.ERROR,
                message="Execution failed",
                metadata=_metadata_for(event, status="error", visualAction="idle"),
            )
        )
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.TASK_FAILED,
                level=RuntimeEventLevel.ERROR,
                message=str(event.payload.get("error") or "Task failed"),
            )
        )

    if event.event_type in {
        ExecutionEventType.LLM_REQUEST_CREATED,
        ExecutionEventType.LLM_RESPONSE_CREATED,
        ExecutionEventType.MODEL_FALLBACK_USED,
        ExecutionEventType.MODEL_FALLBACK_FAILED,
        ExecutionEventType.TOKEN_USAGE_RECORDED,
        ExecutionEventType.TOKEN_BUDGET_WARNING,
        ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
        ExecutionEventType.CONTEXT_HEALTH_RECORDED,
        ExecutionEventType.CONTEXT_COMPACTION_STARTED,
        ExecutionEventType.CONTEXT_COMPACTION_COMPLETED,
        ExecutionEventType.CONTEXT_COMPACTION_FAILED,
        ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
        ExecutionEventType.SUBAGENT_STEP_COMPLETED,
        ExecutionEventType.SUBAGENT_STEP_FAILED,
        ExecutionEventType.SUBAGENT_NEEDS_INPUT,
        ExecutionEventType.SUBAGENT_NEEDS_APPROVAL,
        ExecutionEventType.TOOL_CALL_STARTED,
        ExecutionEventType.TOOL_CALL_COMPLETED,
        ExecutionEventType.TOOL_CALL_FAILED,
        ExecutionEventType.EXECUTION_FAILED,
        ExecutionEventType.EXECUTION_REPAIRED,
        ExecutionEventType.EXECUTION_CYCLE_STARTED,
        ExecutionEventType.EXECUTION_CYCLE_COMPLETED,
        ExecutionEventType.EXECUTION_CYCLE_FAILED,
        ExecutionEventType.EXECUTION_CYCLE_GUARD_TRIGGERED,
        ExecutionEventType.EXECUTION_WAITING,
        ExecutionEventType.EXECUTION_WOKEN,
        ExecutionEventType.MONITOR_FINDING_CREATED,
        ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
        ExecutionEventType.SUPERVISOR_STEERING_REQUESTED,
        ExecutionEventType.SUPERVISOR_STEERING_APPLIED,
    }:
        level = RuntimeEventLevel.ERROR if event.event_type in {
            ExecutionEventType.CONTEXT_COMPACTION_FAILED,
            ExecutionEventType.MODEL_FALLBACK_FAILED,
            ExecutionEventType.SUBAGENT_STEP_FAILED,
            ExecutionEventType.TOOL_CALL_FAILED,
            ExecutionEventType.EXECUTION_FAILED,
            ExecutionEventType.EXECUTION_CYCLE_FAILED,
        } else RuntimeEventLevel.WARNING if event.event_type in {
            ExecutionEventType.TOKEN_BUDGET_WARNING,
            ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
            ExecutionEventType.CONTEXT_COMPACTION_STARTED,
            ExecutionEventType.MODEL_FALLBACK_USED,
            ExecutionEventType.SUBAGENT_NEEDS_INPUT,
            ExecutionEventType.SUBAGENT_NEEDS_APPROVAL,
            ExecutionEventType.EXECUTION_CYCLE_GUARD_TRIGGERED,
            ExecutionEventType.SUPERVISOR_STEERING_REQUESTED,
        } else RuntimeEventLevel.INFO
        runtime_events.append(
            _runtime_event(
                event,
                RuntimeEventType.LOG_RECEIVED,
                level=level,
                message=_log_message_for(event),
            )
        )

    return runtime_events
