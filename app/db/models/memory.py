"""ORM mapping for durable memory records and pgvector-backed embeddings."""

from __future__ import annotations

from sqlalchemy import Boolean, Date, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin, VectorVariant


class MemoryRecordORM(TimestampMixin, Base):
    __tablename__ = "memory_records"
    __table_args__ = (
        Index("ix_memory_records_scope", "scope"),
        Index("ix_memory_records_user_scope", "created_by_user_id", "scope"),
        Index("ix_memory_records_workspace_scope", "workspace_id", "scope"),
        Index("ix_memory_records_conversation_scope", "conversation_id", "scope"),
        Index("ix_memory_records_workflow_scope", "workflow_id", "scope"),
        Index("ix_memory_records_agent_id", "agent_id"),
        Index("ix_memory_records_type_status", "memory_type", "status"),
        Index("ix_memory_records_source_conversation_summary_date", "source_conversation_id", "summary_date"),
        Index("ix_memory_records_agent_type", "agent_id", "memory_type"),
        Index("ix_memory_records_workflow_type", "workflow_id", "memory_type"),
        Index("ix_memory_records_workspace_type", "workspace_id", "memory_type"),
        Index("ix_memory_records_user_type", "created_by_user_id", "memory_type"),
        Index("ix_memory_records_summary_date_type", "summary_date", "memory_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    tags_json: Mapped[list] = mapped_column(JSON_VARIANT, default=list)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    memory_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    importance: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    summary_date: Mapped[object | None] = mapped_column(Date(), nullable=True)
    archived_window_start: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_window_end: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supersedes_memory_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    embedding_json: Mapped[list | None] = mapped_column(JSON_VARIANT, nullable=True)
    embedding_vector: Mapped[list | None] = mapped_column(VectorVariant(), nullable=True)
    embedding_model_profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedded_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
