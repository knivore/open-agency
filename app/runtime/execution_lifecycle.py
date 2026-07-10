from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

ONE_TIME_RUN_MODE = "one_time"
SCHEDULED_RUN_MODE = "scheduled"
ALWAYS_ON_RUN_MODE = "always_on"

RUN_MODES = {ONE_TIME_RUN_MODE, SCHEDULED_RUN_MODE, ALWAYS_ON_RUN_MODE}
SCHEDULE_TRIGGER_TYPES = {"schedule", "scheduled", "cron", "interval"}


@dataclass(frozen=True, slots=True)
class ResolvedExecutionRuntimePolicy:
    worker_hard_timeout_seconds: int | None
    idle_timeout_seconds: int
    run_timeout_seconds: int
    codex_cli_timeout_seconds: int
    llm_request_timeout_seconds: float
    heartbeat_interval_seconds: float
    source_map: dict[str, str] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _metadata_policy(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    runtime_policy = metadata.get("runtime_policy")
    if isinstance(runtime_policy, dict):
        return runtime_policy
    timeout_policy = metadata.get("timeout_policy")
    if isinstance(timeout_policy, dict):
        return timeout_policy
    return metadata


def _apply_policy_values(
        policy: dict[str, Any],
        values: dict[str, Any],
        source_map: dict[str, str],
        source: str,
) -> None:
    worker_timeout = _positive_int(
        values.get("worker_hard_timeout_seconds")
        or values.get("execution_timeout_seconds")
        or values.get("agent_run_timeout_seconds")
        or values.get("run_timeout_seconds")
    )
    run_timeout = _positive_int(values.get("run_timeout_seconds") or values.get("agent_run_timeout_seconds"))
    idle_timeout = _positive_int(
        values.get("idle_timeout_seconds") or values.get("agent_activity_idle_timeout_seconds")
    )
    codex_timeout = _positive_int(
        values.get("codex_cli_timeout_seconds") or values.get("CODEX_CLI_TIMEOUT_SECONDS")
    )
    llm_timeout = _positive_float(
        values.get("llm_request_timeout_seconds") or values.get("LLM_REQUEST_TIMEOUT_SECONDS")
    )
    heartbeat_interval = _positive_float(
        values.get("heartbeat_interval_seconds") or values.get("worker_heartbeat_interval_seconds")
    )
    if worker_timeout is not None:
        policy["worker_hard_timeout_seconds"] = worker_timeout
        source_map["worker_hard_timeout_seconds"] = source
    if run_timeout is not None:
        policy["run_timeout_seconds"] = run_timeout
        source_map["run_timeout_seconds"] = source
    if idle_timeout is not None:
        policy["idle_timeout_seconds"] = idle_timeout
        source_map["idle_timeout_seconds"] = source
    if codex_timeout is not None:
        policy["codex_cli_timeout_seconds"] = codex_timeout
        source_map["codex_cli_timeout_seconds"] = source
    if llm_timeout is not None:
        policy["llm_request_timeout_seconds"] = llm_timeout
        source_map["llm_request_timeout_seconds"] = source
    if heartbeat_interval is not None:
        policy["heartbeat_interval_seconds"] = heartbeat_interval
        source_map["heartbeat_interval_seconds"] = source


def _apply_max_policy_values(
        policy: dict[str, Any],
        values: dict[str, Any],
        source_map: dict[str, str],
        source: str,
) -> None:
    candidate: dict[str, Any] = {}
    candidate_sources: dict[str, str] = {}
    _apply_policy_values(candidate, values, candidate_sources, source)
    for key, value in candidate.items():
        current = policy.get(key)
        if current is None or value > current:
            policy[key] = value
            source_map[key] = candidate_sources.get(key, source)


def resolve_execution_runtime_policy(
        *,
        settings: Any,
        workflow: Any | None = None,
        execution: Any | None = None,
        task: Any | None = None,
        agent: Any | None = None,
        include_workflow_member_maxima: bool = False,
) -> ResolvedExecutionRuntimePolicy:
    """Resolve timeout policy once so workers, monitors, and diagnostics agree."""
    policy: dict[str, Any] = {
        "worker_hard_timeout_seconds": int(getattr(settings, "agent_run_timeout_seconds", 7200)),
        "idle_timeout_seconds": int(getattr(settings, "agent_activity_idle_timeout_seconds", 600)),
        "run_timeout_seconds": int(getattr(settings, "agent_run_timeout_seconds", 7200)),
        "codex_cli_timeout_seconds": int(getattr(settings, "codex_cli_timeout_seconds", 1800)),
        "llm_request_timeout_seconds": float(getattr(settings, "llm_request_timeout_seconds", 15.0)),
        "heartbeat_interval_seconds": 1.0,
    }
    source_map = {key: "settings" for key in policy}

    if workflow is not None:
        _apply_policy_values(
            policy,
            _metadata_policy(getattr(workflow, "metadata", None)),
            source_map,
            "workflow.metadata.runtime_policy",
        )

    # Worker containers are created before a current task/agent is known. Look across workflow members so a
    # long-running task or agent policy can extend the hard worker cap instead of only relaxing monitor findings.
    if include_workflow_member_maxima and workflow is not None:
        for item in getattr(workflow, "task_definitions", []) or []:
            _apply_max_policy_values(
                policy,
                _metadata_policy(getattr(item, "metadata", None)),
                source_map,
                f"task:{getattr(item, 'id', 'unknown')}.metadata.runtime_policy",
            )
        for item in getattr(workflow, "agent_definitions", []) or []:
            _apply_max_policy_values(
                policy,
                _metadata_policy(getattr(item, "metadata", None)),
                source_map,
                f"agent:{getattr(item, 'id', 'unknown')}.metadata.runtime_policy",
            )

    if task is not None:
        _apply_policy_values(
            policy,
            _metadata_policy(getattr(task, "metadata", None)),
            source_map,
            f"task:{getattr(task, 'id', 'unknown')}.metadata.runtime_policy",
        )
    if agent is not None:
        _apply_policy_values(
            policy,
            _metadata_policy(getattr(agent, "metadata", None)),
            source_map,
            f"agent:{getattr(agent, 'id', 'unknown')}.metadata.runtime_policy",
        )

    if execution is not None:
        trigger_payload = getattr(execution, "trigger_payload", None)
        if isinstance(trigger_payload, dict):
            _apply_policy_values(policy, _metadata_policy(trigger_payload), source_map, "execution.trigger_payload")
        input_payload = getattr(execution, "input_payload", None)
        if isinstance(input_payload, dict):
            _apply_policy_values(policy, _metadata_policy(input_payload), source_map, "execution.input_payload")

    _apply_policy_values(
        policy,
        {
            "worker_hard_timeout_seconds": os.getenv("AGENCY_EXECUTION_TIMEOUT_SECONDS"),
            "codex_cli_timeout_seconds": os.getenv("CODEX_CLI_TIMEOUT_SECONDS"),
            "llm_request_timeout_seconds": os.getenv("LLM_REQUEST_TIMEOUT_SECONDS"),
        },
        source_map,
        "environment",
    )

    lifecycle = {}
    if execution is not None and isinstance(getattr(execution, "metadata", None), dict):
        candidate = execution.metadata.get("execution_lifecycle")
        if isinstance(candidate, dict):
            lifecycle = candidate
    if (
            lifecycle.get("run_mode") == ALWAYS_ON_RUN_MODE
            and source_map.get("worker_hard_timeout_seconds") == "settings"
    ):
        # Always-on workers are expected to be governed by heartbeats and stale
        # detection, not by the default finite run cap meant for bounded jobs.
        policy["worker_hard_timeout_seconds"] = None
        source_map["worker_hard_timeout_seconds"] = "execution_lifecycle.always_on"

    return ResolvedExecutionRuntimePolicy(
        worker_hard_timeout_seconds=(
            int(policy["worker_hard_timeout_seconds"])
            if policy["worker_hard_timeout_seconds"] is not None
            else None
        ),
        idle_timeout_seconds=int(policy["idle_timeout_seconds"]),
        run_timeout_seconds=int(policy["run_timeout_seconds"]),
        codex_cli_timeout_seconds=int(policy["codex_cli_timeout_seconds"]),
        llm_request_timeout_seconds=float(policy["llm_request_timeout_seconds"]),
        heartbeat_interval_seconds=float(policy["heartbeat_interval_seconds"]),
        source_map=source_map,
    )


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
