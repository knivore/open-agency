"""Domain models for pending module actions and audit records."""

from __future__ import annotations

from datetime import datetime
from pydantic import Field
from typing import Any, Literal

from .credentials import DomainModel


class PendingAmbientAction(DomainModel):
    id: str
    status: Literal["pending", "approved", "rejected", "expired", "executed"] = "pending"
    action_type: str = "module.action"
    summary: str
    risk_level: Literal["low", "medium", "high"] = "high"
    audit_category: str
    confirmation_required: bool = True
    created_at: datetime
    expires_at: datetime
    executed_at: datetime | None = None
    resolved_at: datetime | None = None
    action_payload: dict[str, Any] = Field(default_factory=dict)
    result_payload: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AmbientActionAuditRecord(DomainModel):
    id: str
    action_id: str | None = None
    event_type: Literal["requested", "approved", "rejected", "expired", "executed"]
    summary: str
    risk_level: Literal["low", "medium", "high"]
    audit_category: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
