from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin, utcnow


@dataclass(frozen=True)
class CollectionDefinition:
    name: str
    description: str


EXECUTIONS_COLLECTION = CollectionDefinition(
    name="executions",
    description="Persistent execution records for workflow and adapter lifecycles.",
)
EXECUTION_EVENTS_COLLECTION = CollectionDefinition(
    name="execution_events",
    description="Ordered execution event stream entries keyed by execution_id and sequence.",
)
EXECUTION_ARTIFACTS_COLLECTION = CollectionDefinition(
    name="execution_artifacts",
    description="Artifacts produced during an execution, optionally linked to an event.",
)


class ExecutionORM(TimestampMixin, Base):
    __tablename__ = "executions"
    __table_args__ = (
        Index("ix_executions_status", "status"),
        Index("ix_executions_workflow_id", "workflow_id"),
        Index("ix_executions_created_at", "created_at"),
        Index("ix_executions_runtime_revision_id", "runtime_revision_id"),
        Index("ix_executions_container_id", "container_id"),
        Index("ix_executions_container_status", "container_status"),
        Index("ix_executions_replacement_of_execution_id", "replacement_of_execution_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), nullable=False)
    workflow_version_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_versions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_revision_id: Mapped[str | None] = mapped_column(ForeignKey("runtime_revisions.id"), nullable=True)
    runtime_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    trigger_payload_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    input_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    output_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    error_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    container_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    container_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    container_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_started_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    container_ended_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    container_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replacement_of_execution_id: Mapped[str | None] = mapped_column(ForeignKey("executions.id"), nullable=True)
    restart_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    workflow = relationship("WorkflowORM", back_populates="executions")
    workflow_version = relationship("WorkflowVersionORM", back_populates="executions")
    runtime_revision = relationship("RuntimeRevisionORM", back_populates="executions")
    replacement_of_execution = relationship("ExecutionORM", remote_side="ExecutionORM.id")
    events = relationship("ExecutionEventORM", back_populates="execution", cascade="all, delete-orphan")
    artifacts = relationship("ExecutionArtifactORM", back_populates="execution", cascade="all, delete-orphan")
    approval_requests = relationship("ApprovalRequestORM", back_populates="execution", cascade="all, delete-orphan")
    tool_invocations = relationship("ToolInvocationORM", back_populates="execution", cascade="all, delete-orphan")


class ExecutionEventORM(Base):
    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint("execution_id", "sequence", name="uq_execution_events_execution_id_sequence"),
        Index("ix_execution_events_execution_id", "execution_id"),
        Index("ix_execution_events_event_type", "event_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    actor_type: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    parent_event_id: Mapped[str | None] = mapped_column(ForeignKey("execution_events.id"), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    execution = relationship("ExecutionORM", back_populates="events")
    parent_event = relationship("ExecutionEventORM", remote_side="ExecutionEventORM.id")
    artifacts = relationship("ExecutionArtifactORM", back_populates="event")
    approval_requests = relationship("ApprovalRequestORM", back_populates="event")
    tool_invocations = relationship("ToolInvocationORM", back_populates="event")


class ExecutionArtifactORM(Base):
    __tablename__ = "execution_artifacts"
    __table_args__ = (Index("ix_execution_artifacts_execution_id", "execution_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("execution_events.id"), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    content_text: Mapped[str | None] = mapped_column(String, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    execution = relationship("ExecutionORM", back_populates="artifacts")
    event = relationship("ExecutionEventORM", back_populates="artifacts")


class ApprovalRequestORM(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("execution_events.id"), nullable=True)
    tool_id: Mapped[str | None] = mapped_column(ForeignKey("tools.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    response_payload_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    requested_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    responded_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    responded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    execution = relationship("ExecutionORM", back_populates="approval_requests")
    event = relationship("ExecutionEventORM", back_populates="approval_requests")
    tool = relationship("ToolORM")


class ToolInvocationORM(Base):
    __tablename__ = "tool_invocations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id", ondelete="CASCADE"), nullable=False)
    tool_id: Mapped[str] = mapped_column(ForeignKey("tools.id"), nullable=False)
    event_id: Mapped[str | None] = mapped_column(ForeignKey("execution_events.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    input_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    output_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    error_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    started_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    execution = relationship("ExecutionORM", back_populates="tool_invocations")
    event = relationship("ExecutionEventORM", back_populates="tool_invocations")
    tool = relationship("ToolORM")
