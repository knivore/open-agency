"""Compatibility exports for ambient confirmation models."""

from __future__ import annotations

from pydantic import BaseModel

from app.domain import AmbientActionAuditRecord, PendingAmbientAction


class PermissionDecision(BaseModel):
    allowed: bool
    requires_confirmation: bool
    risk_level: str
    audit_category: str
    summary: str
    pending_action: PendingAmbientAction | None = None


PendingActionRecord = PendingAmbientAction
AuditLogRecord = AmbientActionAuditRecord

__all__ = [
    "AuditLogRecord",
    "PendingActionRecord",
    "PermissionDecision",
]
