from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.time import ensure_utc
from app.domain import Execution, ExecutionEvent, ExecutionEventType

ACTIVITY_EVENT_TYPES = {
    ExecutionEventType.EXECUTION_STARTED,
    ExecutionEventType.EXECUTION_CYCLE_STARTED,
    ExecutionEventType.EXECUTION_CYCLE_COMPLETED,
    ExecutionEventType.EXECUTION_CYCLE_FAILED,
    ExecutionEventType.EXECUTION_CYCLE_GUARD_TRIGGERED,
    ExecutionEventType.EXECUTION_PAUSED,
    ExecutionEventType.EXECUTION_RESUMED,
    ExecutionEventType.TASK_STARTED,
    ExecutionEventType.AGENT_STEP_STARTED,
    ExecutionEventType.AGENT_STEP_COMPLETED,
    ExecutionEventType.AGENT_STEP_FAILED,
    ExecutionEventType.SUBAGENT_TASK_ASSIGNED,
    ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
    ExecutionEventType.SUBAGENT_STEP_COMPLETED,
    ExecutionEventType.SUBAGENT_STEP_FAILED,
    ExecutionEventType.SUBAGENT_NEEDS_INPUT,
    ExecutionEventType.SUBAGENT_NEEDS_APPROVAL,
    ExecutionEventType.AGENT_MESSAGE_CREATED,
    ExecutionEventType.LLM_REQUEST_CREATED,
    ExecutionEventType.LLM_RESPONSE_CREATED,
    ExecutionEventType.CONTEXT_HEALTH_RECORDED,
    ExecutionEventType.CONTEXT_COMPACTION_STARTED,
    ExecutionEventType.CONTEXT_COMPACTION_COMPLETED,
    ExecutionEventType.CONTEXT_COMPACTION_FAILED,
    ExecutionEventType.TOOL_CALL_STARTED,
    ExecutionEventType.TOOL_CALL_COMPLETED,
    ExecutionEventType.TOOL_CALL_FAILED,
    ExecutionEventType.HANDOFF_REQUESTED,
    ExecutionEventType.ARTIFACT_CREATED,
    ExecutionEventType.APPROVAL_REQUESTED,
    ExecutionEventType.APPROVAL_GRANTED,
    ExecutionEventType.APPROVAL_REJECTED,
    ExecutionEventType.RUNTIME_BUILD_STARTED,
    ExecutionEventType.RUNTIME_BUILD_COMPLETED,
    ExecutionEventType.RUNTIME_BUILD_FAILED,
}


def activity_payload_for_event(event: ExecutionEvent) -> dict[str, Any] | None:
    if event.event_type not in ACTIVITY_EVENT_TYPES:
        return None
    return {
        "last_activity_at": ensure_utc(event.timestamp).isoformat(),
        "last_activity_event_id": event.id,
        "last_activity_event_type": event.event_type.value,
        "last_activity_sequence": event.sequence,
        "last_activity_agent_id": event.agent_id,
        "last_activity_task_id": event.task_id,
        "last_activity_tool_call_id": event.tool_call_id,
        "last_activity_model_request_id": event.model_request_id,
    }


def apply_activity_to_execution(execution: Execution, event: ExecutionEvent) -> bool:
    activity = activity_payload_for_event(event)
    if activity is None:
        return False
    metadata = dict(execution.metadata or {})
    metadata["runtime_activity"] = activity
    execution.metadata = metadata
    execution.updated_at = ensure_utc(event.timestamp)
    return True


def execution_last_activity_at(execution: Execution) -> datetime | None:
    metadata = execution.metadata if isinstance(execution.metadata, dict) else {}
    activity = metadata.get("runtime_activity")
    if not isinstance(activity, dict):
        return None
    raw_value = activity.get("last_activity_at")
    if not isinstance(raw_value, str) or not raw_value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(raw_value.replace("Z", "+00:00")))
    except ValueError:
        return None
