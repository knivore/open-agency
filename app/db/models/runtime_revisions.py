from __future__ import annotations

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class RuntimeRevisionORM(TimestampMixin, Base):
    __tablename__ = "runtime_revisions"
    __table_args__ = (
        Index("ix_runtime_revisions_fingerprint", "fingerprint", unique=True),
        Index("ix_runtime_revisions_build_status", "build_status"),
        Index("ix_runtime_revisions_created_at", "created_at"),
        Index("ix_runtime_revisions_invalidated_at", "invalidated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    source_path: Mapped[str] = mapped_column(String, nullable=False, default="integrations/")
    build_status: Mapped[str] = mapped_column(String(64), nullable=False)
    image_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_tag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    build_log_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    ready_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)

    executions = relationship("ExecutionORM", back_populates="runtime_revision")
