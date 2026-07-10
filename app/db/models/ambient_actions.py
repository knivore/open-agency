from __future__ import annotations

from datetime import datetime
from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class AmbientPendingActionORM(TimestampMixin, Base):
    __tablename__ = "ambient_pending_actions"
    __table_args__ = (
        Index("ix_ambient_pending_actions_status", "status"),
        Index("ix_ambient_pending_actions_expires_at", "expires_at"),
        Index("ix_ambient_pending_actions_audit_category", "audit_category"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    action_type: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(Text(), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    audit_category: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_required: Mapped[bool] = mapped_column(nullable=False, default=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    action_payload_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    result_payload_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)


class AmbientActionAuditORM(Base):
    __tablename__ = "ambient_action_audit_log"
    __table_args__ = (
        Index("ix_ambient_action_audit_log_action_id", "action_id"),
        Index("ix_ambient_action_audit_log_event_type", "event_type"),
        Index("ix_ambient_action_audit_log_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text(), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    audit_category: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
