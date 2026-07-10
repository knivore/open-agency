from __future__ import annotations

import os
import psutil
import subprocess
from typing import Any

from app.core.time import utc_now
from app.domain import Execution, ExecutionEvent


def _extract_token_metrics(event: ExecutionEvent) -> dict[str, Any]:
    usage = event.payload.get("usage", {}) if isinstance(event.payload, dict) else {}
    if isinstance(usage, dict) and isinstance(usage.get("usage"), dict):
        usage = usage["usage"]
    input_tokens = (
            event.metrics.get("prompt_tokens")
            or event.metrics.get("input_tokens")
            or usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or 0
    )
    output_tokens = (
            event.metrics.get("completion_tokens")
            or event.metrics.get("output_tokens")
            or usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0
    )
    total_tokens = event.metrics.get("total_tokens") or usage.get("total_tokens") or (input_tokens + output_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated": bool(event.metrics.get("token_usage_estimated") or usage.get("estimated", False)),
    }


def _usage_cost(event: ExecutionEvent) -> float:
    usage = event.payload.get("usage", {}) if isinstance(event.payload, dict) else {}
    return float(
        event.metrics.get("estimated_cost")
        or (usage.get("estimated_cost") if isinstance(usage, dict) else 0.0)
        or 0.0
    )


def _model_fallback_payload(event: ExecutionEvent) -> dict[str, Any] | None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    provider_usage = usage.get("provider_usage") if isinstance(usage.get("provider_usage"), dict) else {}
    fallback = provider_usage.get("model_fallback") or usage.get("model_fallback") or payload
    return fallback if isinstance(fallback, dict) and fallback.get("used") is True else None


def _fallback_failure_payload(event: ExecutionEvent) -> dict[str, Any] | None:
    if event.event_type.value != "model.fallback.failed":
        return None
    return event.payload if isinstance(event.payload, dict) else {}


def _runtime_governance(execution: Execution) -> dict[str, Any]:
    if not isinstance(execution.metadata, dict):
        return {}
    value = execution.metadata.get("runtime_governance")
    return value if isinstance(value, dict) else {}


def _event_provider_model(event: ExecutionEvent) -> tuple[str, str, str | None]:
    event_payload = event.payload if isinstance(event.payload, dict) else {}
    usage_payload = event_payload.get("usage") if isinstance(event_payload.get("usage"), dict) else {}
    provider = (
            event.metrics.get("model_provider")
            or usage_payload.get("provider")
            or event_payload.get("fallback_provider")
            or event_payload.get("primary_provider")
            or event_payload.get("model_provider")
            or "unknown"
    )
    model = (
            event.metrics.get("model_name")
            or usage_payload.get("model")
            or event_payload.get("fallback_model")
            or event_payload.get("primary_model")
            or event_payload.get("model_name")
            or "unknown"
    )
    currency = usage_payload.get("currency") if isinstance(usage_payload, dict) else None
    return str(provider), str(model), str(currency) if currency else None


def _event_matches_filters(
        event: ExecutionEvent,
        *,
        execution_workflow_ids: dict[str, str],
        workflow_id: str | None = None,
        agent_id: str | None = None,
        execution_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
) -> bool:
    if execution_id and event.execution_id != execution_id:
        return False
    if workflow_id and (event.workflow_id or execution_workflow_ids.get(event.execution_id)) != workflow_id:
        return False
    if agent_id and event.agent_id != agent_id:
        return False
    event_provider, event_model, _currency = _event_provider_model(event)
    if provider and event_provider != provider:
        return False
    if model and event_model != model:
        return False
    return True


def _usage_events(events: list[ExecutionEvent]) -> list[ExecutionEvent]:
    token_usage_events = [
        event
        for event in events
        if event.event_type.value == "token.usage.recorded"
    ]
    token_usage_request_ids = {
        event.model_request_id
        for event in token_usage_events
        if event.model_request_id
    }
    return [
        *token_usage_events,
        *[
            event
            for event in events
            if event.event_type.value == "llm.response.created"
               and (not event.model_request_id or event.model_request_id not in token_usage_request_ids)
        ],
    ]


def _latest_event(events: list[ExecutionEvent]) -> ExecutionEvent | None:
    if not events:
        return None
    return max(events, key=lambda event: (event.sequence, event.timestamp))


def _summarize_context_health(events: list[ExecutionEvent]) -> dict[str, Any]:
    health_events = [event for event in events if event.event_type.value == "context.health.recorded"]
    status_counts: dict[str, int] = {}
    for event in health_events:
        status = str(event.payload.get("status") or event.status or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    latest = _latest_event(health_events)
    latest_payload = dict(latest.payload) if latest and isinstance(latest.payload, dict) else {}
    if latest:
        latest_payload.update(
            {
                "event_id": latest.id,
                "sequence": latest.sequence,
                "timestamp": latest.timestamp.isoformat(),
                "agent_id": latest.agent_id,
                "task_id": latest.task_id,
            }
        )
    return {
        "event_count": len(health_events),
        "status_counts": status_counts,
        "critical_count": status_counts.get("critical", 0) + status_counts.get("overflow", 0),
        "latest": latest_payload if latest else None,
    }


def _summarize_budget(events: list[ExecutionEvent]) -> dict[str, Any]:
    budget_events = [
        event
        for event in events
        if event.event_type.value in {"token.budget.warning", "token.budget.exceeded"}
    ]
    status_counts = {"warning": 0, "exceeded": 0}
    for event in budget_events:
        if event.event_type.value == "token.budget.exceeded":
            status_counts["exceeded"] += 1
        else:
            status_counts["warning"] += 1
    latest = _latest_event(budget_events)
    latest_payload = dict(latest.payload) if latest and isinstance(latest.payload, dict) else {}
    if latest:
        latest_payload.update(
            {
                "event_id": latest.id,
                "event_type": latest.event_type.value,
                "sequence": latest.sequence,
                "timestamp": latest.timestamp.isoformat(),
                "agent_id": latest.agent_id,
                "task_id": latest.task_id,
            }
        )
    return {
        "event_count": len(budget_events),
        "warning_count": status_counts["warning"],
        "exceeded_count": status_counts["exceeded"],
        "latest": latest_payload if latest else None,
    }


def _summarize_compaction(events: list[ExecutionEvent]) -> dict[str, Any]:
    compaction_events = [
        event
        for event in events
        if event.event_type.value.startswith("context.compaction.")
    ]
    status_counts: dict[str, int] = {}
    for event in compaction_events:
        status = event.event_type.value.rsplit(".", 1)[-1]
        status_counts[status] = status_counts.get(status, 0) + 1
    latest = _latest_event(compaction_events)
    latest_payload = dict(latest.payload) if latest and isinstance(latest.payload, dict) else {}
    if latest:
        latest_payload.update(
            {
                "event_id": latest.id,
                "event_type": latest.event_type.value,
                "sequence": latest.sequence,
                "timestamp": latest.timestamp.isoformat(),
                "agent_id": latest.agent_id,
                "task_id": latest.task_id,
            }
        )
    return {
        "event_count": len(compaction_events),
        "status_counts": status_counts,
        "latest": latest_payload if latest else None,
    }


def collect_system_metrics() -> dict[str, Any]:
    metrics = {
        "memory_usage_bytes": psutil.Process(os.getpid()).memory_info().rss,
        "gpu": None,
    }
    try:
        completed = subprocess.run(  # noqa: S603
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            util, used, total = [part.strip() for part in completed.stdout.strip().splitlines()[0].split(",")]
            metrics["gpu"] = {
                "utilization_pct": float(util),
                "memory_used_mb": float(used),
                "memory_total_mb": float(total),
            }
    except Exception:
        metrics["gpu"] = None
    return metrics


def build_timeline(execution: Execution, events: list[ExecutionEvent]) -> dict[str, Any]:
    return {
        "execution": execution.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in events],
        "execution_duration_ms": (
            ((execution.completed_at or utc_now()) - (
                    execution.started_at or execution.created_at)).total_seconds() * 1000
            if execution else None
        ),
    }


def aggregate_agent_metrics(executions: list[Execution], events: list[ExecutionEvent], *, agent_id: str) -> dict[
    str, Any]:
    relevant_events = [event for event in events if event.agent_id == agent_id]
    llm_events = _usage_events(relevant_events)
    tool_success = sum(1 for event in relevant_events if event.event_type.value == "tool.call.completed")
    tool_failure = sum(1 for event in relevant_events if event.event_type.value == "tool.call.failed")
    total_tokens = sum(_extract_token_metrics(event)["total_tokens"] for event in llm_events)
    estimated_cost = sum(_usage_cost(event) for event in llm_events)
    return {
        "agent_id": agent_id,
        "execution_count": len(
            {execution.id for execution in executions if agent_id in (execution.metadata.get("agent_ids") or [])}),
        "llm_request_count": len(llm_events),
        "tool_success_count": tool_success,
        "tool_failure_count": tool_failure,
        "total_tokens": total_tokens,
        "estimated_cost": round(estimated_cost, 8),
        "context_health": _summarize_context_health(relevant_events),
        "budget": _summarize_budget(relevant_events),
        "compaction": _summarize_compaction(relevant_events),
    }


def aggregate_workflow_metrics(executions: list[Execution], events: list[ExecutionEvent], *, workflow_id: str) -> dict[
    str, Any]:
    relevant_executions = [execution for execution in executions if execution.workflow_id == workflow_id]
    relevant_execution_ids = {execution.id for execution in relevant_executions}
    relevant_events = [
        event
        for event in events
        if event.workflow_id == workflow_id or event.execution_id in relevant_execution_ids
    ]
    durations = [
        ((execution.completed_at or execution.created_at) - (
                execution.started_at or execution.created_at)).total_seconds() * 1000
        for execution in relevant_executions
        if execution.started_at is not None
    ]
    return {
        "workflow_id": workflow_id,
        "execution_count": len(relevant_executions),
        "completed_count": sum(1 for execution in relevant_executions if execution.status.value == "completed"),
        "failed_count": sum(1 for execution in relevant_executions if execution.status.value == "failed"),
        "average_duration_ms": sum(durations) / len(durations) if durations else 0,
        "event_count": len(relevant_events),
        "total_tokens": sum(
            int(
                _runtime_governance(execution)
                .get("token_usage", {})
                .get("total", {})
                .get("total_tokens")
                or 0
            )
            for execution in relevant_executions
            if isinstance(execution.metadata, dict)
        ),
        "estimated_cost": round(
            sum(
                float(
                    _runtime_governance(execution)
                    .get("token_usage", {})
                    .get("total", {})
                    .get("estimated_cost")
                    or 0.0
                )
                for execution in relevant_executions
                if isinstance(execution.metadata, dict)
            ),
            8,
        ),
        "context_health": _summarize_context_health(relevant_events),
        "budget": _summarize_budget(relevant_events),
        "compaction": _summarize_compaction(relevant_events),
    }


def aggregate_model_usage(
        events: list[ExecutionEvent],
        *,
        executions: list[Execution] | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        execution_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
) -> dict[str, Any]:
    execution_workflow_ids = {
        execution.id: execution.workflow_id
        for execution in executions or []
    }
    filtered_events = [
        event
        for event in events
        if _event_matches_filters(
            event,
            execution_workflow_ids=execution_workflow_ids,
            workflow_id=workflow_id,
            agent_id=agent_id,
            execution_id=execution_id,
            provider=provider,
            model=model,
        )
    ]
    llm_events = _usage_events(filtered_events)
    usage_by_model: dict[str, dict[str, Any]] = {}
    fallback_primary_models: dict[str, int] = {}
    for event in llm_events:
        event_provider, event_model, currency = _event_provider_model(event)
        key = f"{event_provider}:{event_model}"
        bucket = usage_by_model.setdefault(
            key,
            {
                "provider": event_provider,
                "model": event_model,
                "request_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated_cost": 0.0,
                "estimated_token_count": 0,
                "currency": currency,
            },
        )
        tokens = _extract_token_metrics(event)
        bucket["request_count"] += 1
        bucket["input_tokens"] += tokens["input_tokens"]
        bucket["output_tokens"] += tokens["output_tokens"]
        bucket["prompt_tokens"] += tokens["prompt_tokens"]
        bucket["completion_tokens"] += tokens["completion_tokens"]
        bucket["total_tokens"] += tokens["total_tokens"]
        bucket["estimated_cost"] += float(
            _usage_cost(event)
        )
        if tokens["estimated"]:
            bucket["estimated_token_count"] += 1
        fallback = _model_fallback_payload(event)
        if fallback is not None:
            bucket["fallback_count"] = int(bucket.get("fallback_count") or 0) + 1
            bucket.setdefault("fallback_primary_models", {})
            primary_key = ":".join(
                part
                for part in (
                    fallback.get("primary_provider"),
                    fallback.get("primary_model"),
                )
                if part
            ) or "unknown"
            bucket["fallback_primary_models"][primary_key] = (
                    int(bucket["fallback_primary_models"].get(primary_key) or 0) + 1
            )
            fallback_primary_models[primary_key] = int(fallback_primary_models.get(primary_key) or 0) + 1
        if currency:
            bucket["currency"] = currency
    fallback_failures = [
        event
        for event in filtered_events
        if _fallback_failure_payload(event) is not None
    ]
    total_request_count = sum(int(item.get("request_count") or 0) for item in usage_by_model.values())
    fallback_count = sum(int(item.get("fallback_count") or 0) for item in usage_by_model.values())
    for item in usage_by_model.values():
        request_count = int(item.get("request_count") or 0)
        item["fallback_count"] = int(item.get("fallback_count") or 0)
        item["fallback_rate"] = round(item["fallback_count"] / request_count, 6) if request_count else 0.0
    return {
        "items": list(usage_by_model.values()),
        "fallback_summary": {
            "fallback_count": fallback_count,
            "fallback_failure_count": len(fallback_failures),
            "fallback_rate": round(fallback_count / total_request_count, 6) if total_request_count else 0.0,
            "fallback_primary_models": fallback_primary_models,
            "recent_failures": [
                {
                    "event_id": event.id,
                    "execution_id": event.execution_id,
                    "workflow_id": event.workflow_id,
                    "agent_id": event.agent_id,
                    "task_id": event.task_id,
                    "timestamp": event.timestamp.isoformat(),
                    **(_fallback_failure_payload(event) or {}),
                }
                for event in
                sorted(fallback_failures, key=lambda item: (item.timestamp, item.sequence), reverse=True)[:5]
            ],
        },
        "filters": {
            "workflow_id": workflow_id,
            "agent_id": agent_id,
            "execution_id": execution_id,
            "provider": provider,
            "model": model,
        },
        "system": collect_system_metrics(),
    }
