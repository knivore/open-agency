"""Domain contracts for graph projection outbox events."""

from __future__ import annotations

from datetime import datetime
from pydantic import Field, model_validator
from typing import Any
from uuid import uuid4

from app.core.time import utc_now
from .credentials import DomainModel


class GraphProjectionEvent(DomainModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime = Field(default_factory=utc_now)
    tenant_id: str | None = None
    user_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1
    source: str = "application"
    source_event_id: str | None = None
    status: str = "pending"
    attempts: int = 0
    projected_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def normalize_projection_event(self) -> "GraphProjectionEvent":
        self.event_type = self.event_type.strip()
        self.aggregate_type = self.aggregate_type.strip()
        self.aggregate_id = self.aggregate_id.strip()
        self.source = self.source.strip() or "application"
        self.status = self.status.strip().lower() or "pending"
        if not self.event_type:
            raise ValueError("event_type is required")
        if not self.aggregate_type:
            raise ValueError("aggregate_type is required")
        if not self.aggregate_id:
            raise ValueError("aggregate_id is required")
        if self.schema_version < 1:
            raise ValueError("schema_version must be greater than zero")
        return self
