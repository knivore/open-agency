from __future__ import annotations

from typing import Any

ONE_TIME_RUN_MODE = "one_time"
SCHEDULED_RUN_MODE = "scheduled"
ALWAYS_ON_RUN_MODE = "always_on"

RUN_MODES = {ONE_TIME_RUN_MODE, SCHEDULED_RUN_MODE, ALWAYS_ON_RUN_MODE}
SCHEDULE_TRIGGER_TYPES = {"schedule", "scheduled", "cron", "interval"}


def infer_execution_run_mode(trigger: dict[str, Any] | None) -> str:
    trigger = trigger or {}
    requested_mode = trigger.get("run_mode")
    if requested_mode in RUN_MODES:
        return requested_mode
    trigger_type = str(trigger.get("type") or "").lower()
    source = str(trigger.get("source") or "").lower()
    if trigger.get("schedule_id") or trigger_type in SCHEDULE_TRIGGER_TYPES or source == "scheduler":
        return SCHEDULED_RUN_MODE
    return ONE_TIME_RUN_MODE


def build_execution_lifecycle_metadata(
        *,
        trigger: dict[str, Any] | None,
        workflow_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workflow_lifecycle = {}
    if isinstance(workflow_metadata, dict):
        candidate = workflow_metadata.get("execution_lifecycle")
        if isinstance(candidate, dict):
            workflow_lifecycle = candidate

    run_mode = workflow_lifecycle.get("run_mode") or infer_execution_run_mode(trigger)
    if run_mode not in RUN_MODES:
        run_mode = infer_execution_run_mode(trigger)

    terminate_on_completion = workflow_lifecycle.get("terminate_container_on_completion")
    if terminate_on_completion is None:
        terminate_on_completion = run_mode != ALWAYS_ON_RUN_MODE

    return {
        "run_mode": run_mode,
        "triggered_by_schedule": run_mode == SCHEDULED_RUN_MODE,
        "terminate_container_on_completion": bool(terminate_on_completion),
    }


def should_terminate_container_on_completion(execution: Any) -> bool:
    metadata = getattr(execution, "metadata", {}) or {}
    lifecycle = metadata.get("execution_lifecycle")
    if not isinstance(lifecycle, dict):
        return True
    return bool(lifecycle.get("terminate_container_on_completion", True))
