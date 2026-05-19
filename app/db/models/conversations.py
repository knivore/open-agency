from __future__ import annotations

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin, utcnow


class ConversationORM(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_status", "status"),
        Index("ix_conversations_created_by_user_id", "created_by_user_id"),
        Index("ix_conversations_channel_type", "channel_type"),
        Index("ix_conversations_updated_at", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="open")
    created_by_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    main_agent_profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel_type: Mapped[str] = mapped_column(String(64), nullable=False, default="api")
    channel_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)

    messages = relationship("ConversationMessageORM", back_populates="conversation", cascade="all, delete-orphan")


class ChannelIdentityMappingORM(TimestampMixin, Base):
    __tablename__ = "channel_identity_mappings"
    __table_args__ = (
        Index("ix_channel_identity_mappings_channel", "channel_type", "channel_user_id", unique=True),
        Index("ix_channel_identity_mappings_internal_user_id", "internal_user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_type: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    internal_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trusted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)


class ConversationMessageORM(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conversation_messages_conversation_id", "conversation_id"),
        Index("ix_conversation_messages_external_message", "conversation_id", "external_message_id"),
        Index("ix_conversation_messages_created_at", "created_at"),
        Index("ix_conversation_messages_message_type", "message_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    plain_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    conversation = relationship("ConversationORM", back_populates="messages")
