from __future__ import annotations

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class UploadedDocumentORM(TimestampMixin, Base):
    __tablename__ = "uploaded_documents"
    __table_args__ = (
        Index("ix_uploaded_documents_conversation_id", "conversation_id"),
        Index("ix_uploaded_documents_workflow_id", "workflow_id"),
        Index("ix_uploaded_documents_agent_id", "agent_id"),
        Index("ix_uploaded_documents_created_by_user_scope", "created_by_user_id", "scope"),
        Index("ix_uploaded_documents_workspace_scope", "workspace_id", "scope"),
        Index("ix_uploaded_documents_upload_mode", "upload_mode"),
        Index("ix_uploaded_documents_content_sha256", "content_sha256"),
        Index("ix_uploaded_documents_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage_uri: Mapped[str | None] = mapped_column(Text(), nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    text_characters: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    upload_mode: Mapped[str] = mapped_column(String(32), default="vector", nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
