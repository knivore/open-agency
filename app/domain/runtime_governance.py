"""Runtime governance contracts for token, context, and budget telemetry."""

from __future__ import annotations

from pydantic import Field, model_validator
from typing import Any, Literal

from .credentials import DomainModel

ContextHealthStatus = Literal["unknown", "normal", "warning", "critical", "overflow"]
BudgetScope = Literal["run", "workflow", "agent"]


class TokenUsage(DomainModel):
    provider: str | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost: float = 0.0
    currency: str | None = None
    estimated: bool = False
    estimate_method: str | None = None
    provider_usage: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_totals(self) -> "TokenUsage":
        self.prompt_tokens = max(int(self.prompt_tokens or 0), 0)
        self.completion_tokens = max(int(self.completion_tokens or 0), 0)
        self.cached_tokens = max(int(self.cached_tokens or 0), 0)
        self.reasoning_tokens = max(int(self.reasoning_tokens or 0), 0)
        computed_total = self.prompt_tokens + self.completion_tokens
        self.total_tokens = max(int(self.total_tokens or computed_total), computed_total, 0)
        self.estimated_cost = max(float(self.estimated_cost or 0.0), 0.0)
        return self


class TokenBudgetPolicy(DomainModel):
    run_total_tokens: int | None = None
    workflow_total_tokens: int | None = None
    agent_total_tokens: int | None = None
    warn_ratio: float = 0.8
    hard_ratio: float = 1.0
    action: Literal["warn_only", "compact_context", "pause_execution", "fail_execution"] = "warn_only"

    @model_validator(mode="after")
    def normalize_policy(self) -> "TokenBudgetPolicy":
        for field_name in ("run_total_tokens", "workflow_total_tokens", "agent_total_tokens"):
            value = getattr(self, field_name)
            if value is not None:
                setattr(self, field_name, max(int(value), 0) or None)
        self.warn_ratio = min(max(float(self.warn_ratio or 0.8), 0.0), 1.0)
        self.hard_ratio = max(float(self.hard_ratio or 1.0), self.warn_ratio)
        return self


class TokenBudgetStatus(DomainModel):
    scope: BudgetScope
    used_tokens: int
    budget_tokens: int
    usage_ratio: float
    status: Literal["normal", "warning", "exceeded"]
    action: str = "warn_only"


class ContextHealth(DomainModel):
    estimated_prompt_tokens: int
    reserved_completion_tokens: int = 0
    estimated_total_context_tokens: int
    context_window: int | None = None
    remaining_context_tokens: int | None = None
    usage_ratio: float | None = None
    status: ContextHealthStatus = "unknown"
    estimate_method: str = "plain_text_chars_div_4_with_message_overhead"
    estimator_version: str = "v1"


class ContextCompactionRecord(DomainModel):
    compacted: bool = False
    reason: str | None = None
    memory_id: str | None = None
    source_model_request_id: str | None = None
    estimated_tokens_saved: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubAgentStatusUpdate(DomainModel):
    status: str
    current_task: str | None = None
    completed_step: str | None = None
    blocker: str | None = None
    clarification_needed: str | None = None
    confidence: float | None = None
    token_usage: TokenUsage | None = None
    context_health: ContextHealth | None = None
    tool_result_summary: str | None = None
    next_action: str | None = None
    progress_percent: float | None = None

    @model_validator(mode="after")
    def normalize_confidence_and_progress(self) -> "SubAgentStatusUpdate":
        if self.confidence is not None:
            self.confidence = min(max(float(self.confidence), 0.0), 1.0)
        if self.progress_percent is not None:
            self.progress_percent = min(max(float(self.progress_percent), 0.0), 100.0)
        return self


class SupervisorSteeringDecision(DomainModel):
    action: str
    reason: str
    execution_id: str
    workflow_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    requires_human_approval: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
