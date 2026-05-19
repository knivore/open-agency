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
        ExecutionEventType.TOOL_CALL_STARTED,
        ExecutionEventType.TOOL_CALL_COMPLETED,
        ExecutionEventType.TOOL_CALL_FAILED,
        ExecutionEventType.EXECUTION_FAILED,
        ExecutionEventType.EXECUTION_REPAIRED,
        ExecutionEventType.MONITOR_FINDING_CREATED,
        ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
    }:
        level = RuntimeEventLevel.ERROR if event.event_type in {
            ExecutionEventType.TOOL_CALL_FAILED,
            ExecutionEventType.EXECUTION_FAILED,
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
