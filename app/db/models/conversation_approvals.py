from __future__ import annotations

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class ConversationApprovalRequestORM(TimestampMixin, Base):
    __tablename__ = "conversation_approval_requests"
    __table_args__ = (
        Index("ix_conversation_approval_requests_conversation_id", "conversation_id"),
        Index("ix_conversation_approval_requests_status", "status"),
        Index("ix_conversation_approval_requests_origin_message_id", "origin_message_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    approval_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_by_agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by_profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text(), nullable=False)
    diff_summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    proposed_payload_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
