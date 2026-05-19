from __future__ import annotations

from typing import Any

from app.core.time import ensure_utc, utc_now
from app.domain import Execution, ExecutionStatus

STALE_EXECUTION_STATUSES = {
    ExecutionStatus.QUEUED,
    ExecutionStatus.RUNNING,
    ExecutionStatus.PAUSED,
    ExecutionStatus.CANCELLING,
}


def classify_execution_staleness(execution: Execution, *, stale_after_seconds: int) -> dict[str, Any]:
    reference = execution.last_heartbeat_at or execution.updated_at or execution.started_at or execution.created_at
    reference_at = ensure_utc(reference)
    age_seconds = max(0, int((utc_now() - reference_at).total_seconds()))
    eligible_status = execution.status in STALE_EXECUTION_STATUSES
    is_stale = eligible_status and age_seconds > stale_after_seconds
    reason = None
    if is_stale:
        reason = (
            f"Execution has status '{execution.status.value}' with no recent heartbeat or update for "
            f"{age_seconds} seconds."
        )
    return {
        "is_stale": is_stale,
        "eligible_status": eligible_status,
        "status": execution.status.value,
        "reference_at": reference_at.isoformat(),
        "age_seconds": age_seconds,
        "stale_after_seconds": stale_after_seconds,
        "reason": reason,
    }
