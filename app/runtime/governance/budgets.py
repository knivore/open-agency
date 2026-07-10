from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.domain import (
    AgentDefinition,
    Execution,
    TaskDefinition,
    TokenBudgetPolicy,
    TokenBudgetStatus,
    WorkflowDefinition,
)


def _budget_config(container: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(container, dict):
        return {}
    governance = container.get("runtime_governance")
    if isinstance(governance, dict) and isinstance(governance.get("token_budget"), dict):
        return dict(governance["token_budget"])
    if isinstance(container.get("token_budget"), dict):
        return dict(container["token_budget"])
    return {}


def _merge_budget_config(*containers: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for container in containers:
        merged.update(_budget_config(container))
    return merged


def _settings_budget_config() -> dict[str, Any]:
    settings = get_settings()
    config: dict[str, Any] = {
        "warn_ratio": settings.agent_token_budget_warn_ratio,
        "hard_ratio": settings.agent_token_budget_hard_ratio,
        "action": settings.agent_token_budget_action,
    }
    if settings.agent_run_total_token_budget:
        config["run_total_tokens"] = settings.agent_run_total_token_budget
    return {"runtime_governance": {"token_budget": config}}


def resolve_token_budget_policy(
        *,
        workflow: WorkflowDefinition,
        agent: AgentDefinition,
        task: TaskDefinition,
        execution: Execution,
) -> TokenBudgetPolicy | None:
    config = _merge_budget_config(
        _settings_budget_config(),
        workflow.metadata,
        agent.metadata,
        task.metadata,
        execution.input_payload,
        execution.trigger_payload,
    )
    if not config:
        return None
    allowed = {
        "run_total_tokens",
        "workflow_total_tokens",
        "agent_total_tokens",
        "warn_ratio",
        "hard_ratio",
        "action",
    }
    normalized = {key: value for key, value in config.items() if key in allowed}
    try:
        policy = TokenBudgetPolicy.model_validate(normalized)
    except ValueError:
        return None
    if not any((policy.run_total_tokens, policy.workflow_total_tokens, policy.agent_total_tokens)):
        return None
    return policy


def _status_for(*, scope: str, used_tokens: int, budget_tokens: int, policy: TokenBudgetPolicy) -> TokenBudgetStatus:
    ratio = used_tokens / budget_tokens if budget_tokens > 0 else 0.0
    if ratio >= policy.hard_ratio:
        status = "exceeded"
    elif ratio >= policy.warn_ratio:
        status = "warning"
    else:
        status = "normal"
    return TokenBudgetStatus(
        scope=scope,
        used_tokens=used_tokens,
        budget_tokens=budget_tokens,
        usage_ratio=round(ratio, 6),
        status=status,
        action=policy.action,
    )


def budget_warning_statuses(
        *,
        policy: TokenBudgetPolicy | None,
        run_total_tokens: int,
        workflow_total_tokens: int,
        agent_total_tokens: int,
) -> list[TokenBudgetStatus]:
    if policy is None:
        return []
    statuses: list[TokenBudgetStatus] = []
    if policy.run_total_tokens:
        statuses.append(
            _status_for(
                scope="run",
                used_tokens=run_total_tokens,
                budget_tokens=policy.run_total_tokens,
                policy=policy,
            )
        )
    if policy.workflow_total_tokens:
        statuses.append(
            _status_for(
                scope="workflow",
                used_tokens=workflow_total_tokens,
                budget_tokens=policy.workflow_total_tokens,
                policy=policy,
            )
        )
    if policy.agent_total_tokens:
        statuses.append(
            _status_for(
                scope="agent",
                used_tokens=agent_total_tokens,
                budget_tokens=policy.agent_total_tokens,
                policy=policy,
            )
        )
    return [item for item in statuses if item.status in {"warning", "exceeded"}]
