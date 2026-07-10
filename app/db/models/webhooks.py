"""SQLAlchemy models for outbound webhook delivery attempts."""

from __future__ import annotations

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.common import utcnow


class OutboundWebhookAttemptORM(Base):
    __tablename__ = "outbound_webhook_attempts"
    __table_args__ = (
        Index("ix_outbound_webhook_attempts_event_id", "event_id"),
        Index("ix_outbound_webhook_attempts_target", "target"),
        Index("ix_outbound_webhook_attempts_status", "status"),
        Index("ix_outbound_webhook_attempts_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    target: Mapped[str] = mapped_column(String(128), nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    event = relationship("ExecutionEventORM")
