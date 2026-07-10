"""Domain contracts for durable goal-driven autonomous work."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pydantic import Field, model_validator
from typing import Any
from uuid import uuid4

from app.core.time import utc_now
from .credentials import DomainModel


class GoalStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    ACTIVE = "active"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


TERMINAL_GOAL_STATUSES = {
    GoalStatus.COMPLETED,
    GoalStatus.FAILED,
    GoalStatus.CANCELLED,
    GoalStatus.ABANDONED,
}


class GoalDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    objective: str
    status: GoalStatus = GoalStatus.CREATED
    priority: str = "normal"
    owner_actor: str | None = None
    parent_goal_id: str | None = None
    success_criteria: list[dict[str, Any]] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    execution_ids: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    evaluation: dict[str, Any] | None = None
    deadline_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_goal(self) -> "GoalDefinition":
        if not self.objective.strip():
            raise ValueError("Goal objective is required")
        if self.completed_at is None and self.status in TERMINAL_GOAL_STATUSES:
            self.completed_at = utc_now()
        if self.completed_at is not None and self.status not in TERMINAL_GOAL_STATUSES:
            raise ValueError("completed_at can only be set on a terminal goal")
        return self
