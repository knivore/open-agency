from __future__ import annotations

from typing import Any

from app.core.time import utc_now
from app.domain import ContextCompactionRecord, ContextHealth, Execution, TokenBudgetPolicy, TokenBudgetStatus, \
    TokenUsage
from .budgets import budget_warning_statuses

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "estimated_cost")


def _governance(metadata: dict[str, Any] | None) -> dict[str, Any]:
    updated = dict(metadata or {})
    runtime_governance = dict(updated.get("runtime_governance") or {})
    updated["runtime_governance"] = runtime_governance
    return updated


def _usage_bucket(container: dict[str, Any], key: str) -> dict[str, Any]:
    bucket = dict(container.get(key) or {})
    for field in TOKEN_FIELDS:
        bucket.setdefault(field, 0.0 if field == "estimated_cost" else 0)
    container[key] = bucket
    return bucket


def _add_usage(bucket: dict[str, Any], usage: TokenUsage) -> None:
    bucket["prompt_tokens"] = int(bucket.get("prompt_tokens") or 0) + usage.prompt_tokens
    bucket["completion_tokens"] = int(bucket.get("completion_tokens") or 0) + usage.completion_tokens
    bucket["total_tokens"] = int(bucket.get("total_tokens") or 0) + usage.total_tokens
    bucket["estimated_cost"] = round(float(bucket.get("estimated_cost") or 0.0) + usage.estimated_cost, 8)
    if usage.currency:
        bucket["currency"] = usage.currency


def _model_fallback_metadata(usage: TokenUsage) -> dict[str, Any] | None:
    fallback = usage.provider_usage.get("model_fallback")
    if not isinstance(fallback, dict) or fallback.get("used") is not True:
        return None
    return dict(fallback)


def _increment_fallback_count(bucket: dict[str, Any]) -> None:
    bucket["fallback_count"] = int(bucket.get("fallback_count") or 0) + 1


def _budget_event_key(status: TokenBudgetStatus) -> str:
    return f"{status.scope}:{status.budget_tokens}:{status.status}"


async def record_context_health_snapshot(
        store: Any,
        *,
        execution_id: str,
        context_health: ContextHealth,
        agent_id: str | None = None,
        task_id: str | None = None,
        event_id: str | None = None,
) -> Execution | None:
    execution = await store.get_execution(execution_id)
    if execution is None:
        return None
    metadata = _governance(execution.metadata)
    runtime_governance = metadata["runtime_governance"]
    runtime_governance["context_health"] = {
        "last": {
            **context_health.model_dump(mode="json"),
            "agent_id": agent_id,
            "task_id": task_id,
            "event_id": event_id,
            "updated_at": utc_now().isoformat(),
        }
    }
    execution.metadata = metadata
    execution.updated_at = utc_now()
    return await store.update_execution(execution)


async def record_context_compaction_snapshot(
        store: Any,
        *,
        execution_id: str,
        record: ContextCompactionRecord,
        agent_id: str | None = None,
        task_id: str | None = None,
        event_id: str | None = None,
) -> Execution | None:
    execution = await store.get_execution(execution_id)
    if execution is None:
        return None
    metadata = _governance(execution.metadata)
    runtime_governance = metadata["runtime_governance"]
    container = dict(runtime_governance.get("context_compaction") or {})
    records = list(container.get("records") or [])
    entry = {
        **record.model_dump(mode="json"),
        "agent_id": agent_id,
        "task_id": task_id,
        "event_id": event_id,
        "updated_at": utc_now().isoformat(),
    }
    records.append(entry)
    container["last"] = entry
    container["records"] = records[-25:]
    container["count"] = int(container.get("count") or 0) + 1
    if record.compacted:
        container["compacted_count"] = int(container.get("compacted_count") or 0) + 1
        container["estimated_tokens_saved"] = (
                int(container.get("estimated_tokens_saved") or 0) + record.estimated_tokens_saved
        )
    runtime_governance["context_compaction"] = container
    execution.metadata = metadata
    execution.updated_at = utc_now()
    return await store.update_execution(execution)


async def record_token_usage_snapshot(
        store: Any,
        *,
        execution_id: str,
        usage: TokenUsage,
        policy: TokenBudgetPolicy | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        workflow_id: str | None = None,
        model_request_id: str | None = None,
        event_id: str | None = None,
) -> tuple[Execution | None, list[TokenBudgetStatus]]:
    execution = await store.get_execution(execution_id)
    if execution is None:
        return None, []

    metadata = _governance(execution.metadata)
    runtime_governance = metadata["runtime_governance"]
    token_usage = dict(runtime_governance.get("token_usage") or {})
    processed_event_ids = list(token_usage.get("processed_event_ids") or [])
    if event_id and event_id in processed_event_ids:
        return execution, []

    total = _usage_bucket(token_usage, "total")
    _add_usage(total, usage)

    by_agent = dict(token_usage.get("by_agent") or {})
    if agent_id:
        agent_bucket = _usage_bucket(by_agent, agent_id)
        _add_usage(agent_bucket, usage)
    token_usage["by_agent"] = by_agent

    by_task = dict(token_usage.get("by_task") or {})
    if task_id:
        task_bucket = _usage_bucket(by_task, task_id)
        _add_usage(task_bucket, usage)
    token_usage["by_task"] = by_task

    model_key = ":".join(part for part in (usage.provider, usage.model) if part) or "unknown"
    by_model = dict(token_usage.get("by_model") or {})
    model_bucket = _usage_bucket(by_model, model_key)
    _add_usage(model_bucket, usage)
    token_usage["by_model"] = by_model

    fallback = _model_fallback_metadata(usage)
    if fallback is not None:
        _increment_fallback_count(total)
        _increment_fallback_count(model_bucket)
        if agent_id:
            _increment_fallback_count(agent_bucket)
        if task_id:
            _increment_fallback_count(task_bucket)
        fallback_records = list(token_usage.get("model_fallbacks") or [])
        fallback_records.append(
            {
                **fallback,
                "agent_id": agent_id,
                "task_id": task_id,
                "workflow_id": workflow_id,
                "model_request_id": model_request_id,
                "event_id": event_id,
                "updated_at": utc_now().isoformat(),
            }
        )
        token_usage["fallback_count"] = int(token_usage.get("fallback_count") or 0) + 1
        token_usage["model_fallbacks"] = fallback_records[-25:]

    if event_id:
        processed_event_ids.append(event_id)
        token_usage["processed_event_ids"] = processed_event_ids[-100:]
    token_usage["last_model_request_id"] = model_request_id
    token_usage["last_event_id"] = event_id
    token_usage["updated_at"] = utc_now().isoformat()
    runtime_governance["token_usage"] = token_usage

    run_total_tokens = int(total.get("total_tokens") or 0)
    workflow_total_tokens = run_total_tokens
    agent_total_tokens = int(by_agent.get(agent_id or "", {}).get("total_tokens") or 0)
    statuses = budget_warning_statuses(
        policy=policy,
        run_total_tokens=run_total_tokens,
        workflow_total_tokens=workflow_total_tokens,
        agent_total_tokens=agent_total_tokens,
    )

    emitted = dict(runtime_governance.get("budget_warnings_emitted") or {})
    new_statuses: list[TokenBudgetStatus] = []
    for status in statuses:
        key = _budget_event_key(status)
        if key in emitted:
            continue
        emitted[key] = {
            **status.model_dump(mode="json"),
            "agent_id": agent_id,
            "task_id": task_id,
            "workflow_id": workflow_id,
            "event_id": event_id,
            "emitted_at": utc_now().isoformat(),
        }
        new_statuses.append(status)
    runtime_governance["budget_warnings_emitted"] = emitted

    execution.metadata = metadata
    execution.updated_at = utc_now()
    updated = await store.update_execution(execution)
    return updated, new_statuses
