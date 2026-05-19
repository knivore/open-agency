from __future__ import annotations

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin, utcnow


class WorkflowORM(TimestampMixin, Base):
    __tablename__ = "workflows"
    __table_args__ = (Index("ix_workflows_enabled", "enabled"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    current_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    versions = relationship("WorkflowVersionORM", back_populates="workflow", cascade="all, delete-orphan")
    executions = relationship("ExecutionORM", back_populates="workflow")


class WorkflowVersionORM(Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (UniqueConstraint("workflow_id", "version", name="uq_workflow_versions_workflow_id_version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow = relationship("WorkflowORM", back_populates="versions")
    executions = relationship("ExecutionORM", back_populates="workflow_version")
