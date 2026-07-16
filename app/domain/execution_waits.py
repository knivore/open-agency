"""Durable suspension records for resumable workflow executions."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import Field, model_validator

from app.core.time import ensure_utc, utc_now
from .credentials import DomainModel


class ExecutionWaitKind(str, Enum):
    INPUT = "input"
    APPROVAL = "approval"
    EVENT = "event"
    SLEEP = "sleep"


class ExecutionWaitStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


TERMINAL_EXECUTION_WAIT_STATUSES = {
    ExecutionWaitStatus.RESOLVED,
    ExecutionWaitStatus.EXPIRED,
    ExecutionWaitStatus.CANCELLED,
}


class ExecutionWait(DomainModel):
    """Queryable wait ledger entry used to suspend and wake one continuation."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    execution_id: str
    kind: ExecutionWaitKind
    status: ExecutionWaitStatus = ExecutionWaitStatus.PENDING
    idempotency_key: str = Field(min_length=1, max_length=255)
    correlation_key: str | None = Field(default=None, max_length=255)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    request_payload: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    resolution_payload: dict[str, Any] | None = None
    resolution_key: str | None = Field(default=None, max_length=255)
    wake_at: datetime | None = None
    deadline_at: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = Field(default=None, max_length=255)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ExecutionWait":
        if not self.idempotency_key.strip():
            raise ValueError("Execution wait idempotency_key must not be empty.")
        if self.kind == ExecutionWaitKind.SLEEP and self.wake_at is None:
            raise ValueError("Sleep waits require wake_at.")
        if self.kind != ExecutionWaitKind.SLEEP and self.wake_at is not None:
            raise ValueError("Only sleep waits may define wake_at.")
        if self.kind == ExecutionWaitKind.EVENT and not (self.correlation_key or "").strip():
            raise ValueError("Event waits require correlation_key.")
        for field_name in ("wake_at", "deadline_at", "resolved_at", "created_at", "updated_at"):
            value = getattr(self, field_name)
            if value is not None:
                setattr(self, field_name, ensure_utc(value))
        if self.wake_at is not None and self.deadline_at is not None and self.deadline_at < self.wake_at:
            raise ValueError("Execution wait deadline_at cannot be earlier than wake_at.")
        if self.status == ExecutionWaitStatus.PENDING and self.resolved_at is not None:
            raise ValueError("Pending execution waits cannot have resolved_at.")
        if self.status in TERMINAL_EXECUTION_WAIT_STATUSES and self.resolved_at is None:
            self.resolved_at = self.updated_at
        return self
