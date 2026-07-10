from __future__ import annotations

from typing import Any

from app.core.time import ensure_utc, utc_now
from app.domain import Execution, ExecutionStatus
from app.services.execution_activity import execution_last_activity_at

STALE_EXECUTION_STATUSES = {
    ExecutionStatus.QUEUED,
    ExecutionStatus.RUNNING,
    ExecutionStatus.WAITING_FOR_APPROVAL,
    ExecutionStatus.PAUSED,
    ExecutionStatus.CANCELLING,
}

INTENTIONAL_WAIT_STATUSES = {
    ExecutionStatus.WAITING_FOR_APPROVAL: "waiting_for_approval",
    ExecutionStatus.PAUSED: "paused",
}

INTENTIONAL_WAIT_CHECKPOINT_STATUSES = {
    "needs_input": "waiting_for_input",
    "needs_approval": "waiting_for_approval",
}


def classify_execution_staleness(
        execution: Execution,
        *,
        stale_after_seconds: int,
        idle_timeout_seconds: int | None = None,
        run_timeout_seconds: int | None = None,
) -> dict[str, Any]:
    now = utc_now()
    heartbeat_at = ensure_utc(execution.last_heartbeat_at) if execution.last_heartbeat_at else None
    last_activity_at = execution_last_activity_at(execution)
    activity_reference = last_activity_at or execution.updated_at or execution.started_at or execution.created_at
    activity_reference_at = ensure_utc(activity_reference)
    heartbeat_reference_at = heartbeat_at or activity_reference_at
    age_seconds = max(0, int((now - heartbeat_reference_at).total_seconds()))
    activity_age_seconds = max(0, int((now - activity_reference_at).total_seconds()))
    runtime_seconds = (
        max(0, int((now - ensure_utc(execution.started_at)).total_seconds()))
        if execution.started_at is not None
        else None
    )
    eligible_status = execution.status in STALE_EXECUTION_STATUSES
    wait_state = intentional_execution_wait_state(execution)
    intentionally_waiting = wait_state is not None
    intentionally_long_running = intentional_long_running_execution(execution)
    heartbeat_stale = eligible_status and not intentionally_waiting and age_seconds > stale_after_seconds
    alive_but_idle = (
            eligible_status
            and not intentionally_waiting
            and not heartbeat_stale
            and idle_timeout_seconds is not None
            and activity_age_seconds > idle_timeout_seconds
    )
    run_timeout_exceeded = (
            eligible_status
            and not intentionally_waiting
            and not intentionally_long_running
            and run_timeout_seconds is not None
            and runtime_seconds is not None
            and runtime_seconds > run_timeout_seconds
    )
    is_stale = heartbeat_stale or alive_but_idle or run_timeout_exceeded
    stale_kind = None
    reason = None
    if heartbeat_stale:
        stale_kind = "worker_unresponsive"
        reason = (
            f"Execution has status '{execution.status.value}' with no recent worker heartbeat for "
            f"{age_seconds} seconds."
        )
    elif alive_but_idle:
        stale_kind = "alive_but_idle"
        reason = (
            f"Execution has status '{execution.status.value}' and a live worker, but no agent activity for "
            f"{activity_age_seconds} seconds."
        )
    elif run_timeout_exceeded:
        stale_kind = "run_timeout_exceeded"
        reason = (
            f"Execution has status '{execution.status.value}' and has exceeded the run timeout after "
            f"{runtime_seconds} seconds."
        )
    return {
        "is_stale": is_stale,
        "eligible_status": eligible_status,
        "status": execution.status.value,
        "stale_kind": stale_kind,
        "wait_state": wait_state,
        "intentionally_waiting": intentionally_waiting,
        "intentionally_long_running": intentionally_long_running,
        "reference_at": heartbeat_reference_at.isoformat(),
        "age_seconds": age_seconds,
        "stale_after_seconds": stale_after_seconds,
        "last_heartbeat_at": heartbeat_at.isoformat() if heartbeat_at else None,
        "heartbeat_age_seconds": age_seconds,
        "last_activity_at": activity_reference_at.isoformat(),
        "last_recorded_activity_at": last_activity_at.isoformat() if last_activity_at else None,
        "activity_age_seconds": activity_age_seconds,
        "idle_timeout_seconds": idle_timeout_seconds,
        "runtime_seconds": runtime_seconds,
        "run_timeout_seconds": run_timeout_seconds,
        "reason": reason,
    }


def intentional_execution_wait_state(execution: Execution) -> str | None:
    """Return a supervisor-visible wait state that should not be repaired as a stale worker."""
    if execution.status in INTENTIONAL_WAIT_STATUSES:
        return INTENTIONAL_WAIT_STATUSES[execution.status]

    metadata = execution.metadata if isinstance(execution.metadata, dict) else {}
    pending_input = metadata.get("pending_subagent_input")
    if isinstance(pending_input, dict) and pending_input.get("status") == "needs_input":
        return "waiting_for_input"
    pending_approval = metadata.get("pending_subagent_approval")
    if isinstance(pending_approval, dict) and pending_approval.get("status") == "needs_approval":
        return "waiting_for_approval"

    runtime_callbacks = metadata.get("runtime_callbacks")
    checkpoints = runtime_callbacks.get("checkpoints") if isinstance(runtime_callbacks, dict) else None
    if isinstance(checkpoints, dict):
        for checkpoint in checkpoints.values():
            if not isinstance(checkpoint, dict):
                continue
            status = checkpoint.get("status")
            if isinstance(status, str) and status in INTENTIONAL_WAIT_CHECKPOINT_STATUSES:
                return INTENTIONAL_WAIT_CHECKPOINT_STATUSES[status]
    return None


def intentional_long_running_execution(execution: Execution) -> bool:
    metadata = execution.metadata if isinstance(execution.metadata, dict) else {}
    lifecycle = metadata.get("execution_lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("run_mode") == "always_on":
        return True
    runtime_policy = metadata.get("runtime_policy") or metadata.get("timeout_policy")
    if isinstance(runtime_policy, dict) and runtime_policy.get("long_running") is True:
        return True
    return metadata.get("long_running") is True
