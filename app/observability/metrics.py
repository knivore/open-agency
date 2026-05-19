from __future__ import annotations

import os
import psutil
import subprocess
from datetime import datetime
from typing import Any

from app.core.time import utc_now
from app.domain import Execution, ExecutionEvent


def _extract_token_metrics(event: ExecutionEvent) -> dict[str, Any]:
    usage = event.payload.get("usage", {}) if isinstance(event.payload, dict) else {}
    input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    output_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    total_tokens = usage.get("total_tokens") or (input_tokens + output_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
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
    llm_events = [event for event in relevant_events if event.event_type.value == "llm.response.created"]
    tool_success = sum(1 for event in relevant_events if event.event_type.value == "tool.call.completed")
    tool_failure = sum(1 for event in relevant_events if event.event_type.value == "tool.call.failed")
    total_tokens = sum(_extract_token_metrics(event)["total_tokens"] for event in llm_events)
    return {
        "agent_id": agent_id,
        "execution_count": len(
            {execution.id for execution in executions if agent_id in (execution.metadata.get("agent_ids") or [])}),
        "llm_request_count": len(llm_events),
        "tool_success_count": tool_success,
        "tool_failure_count": tool_failure,
        "total_tokens": total_tokens,
    }


def aggregate_workflow_metrics(executions: list[Execution], events: list[ExecutionEvent], *, workflow_id: str) -> dict[
    str, Any]:
    relevant_executions = [execution for execution in executions if execution.workflow_id == workflow_id]
    relevant_events = [event for event in events if event.workflow_id == workflow_id]
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
    }


def aggregate_model_usage(events: list[ExecutionEvent]) -> dict[str, Any]:
    llm_events = [event for event in events if event.event_type.value == "llm.response.created"]
    usage_by_model: dict[str, dict[str, Any]] = {}
    for event in llm_events:
        provider = event.metrics.get("model_provider") or event.payload.get("model_provider") or "unknown"
        model = event.metrics.get("model_name") or event.payload.get("model_name") or "unknown"
        key = f"{provider}:{model}"
        bucket = usage_by_model.setdefault(
            key,
            {
                "provider": provider,
                "model": model,
                "request_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost": 0.0,
            },
        )
        tokens = _extract_token_metrics(event)
        bucket["request_count"] += 1
        bucket["input_tokens"] += tokens["input_tokens"]
        bucket["output_tokens"] += tokens["output_tokens"]
        bucket["total_tokens"] += tokens["total_tokens"]
        bucket["estimated_cost"] += float(event.metrics.get("estimated_cost", 0.0) or 0.0)
    return {"items": list(usage_by_model.values()), "system": collect_system_metrics()}
