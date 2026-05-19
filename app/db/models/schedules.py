from __future__ import annotations

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class ScheduleORM(TimestampMixin, Base):
    __tablename__ = "schedules"
    __table_args__ = (
        Index("ix_schedules_enabled", "enabled"),
        Index("ix_schedules_next_fire_at", "next_fire_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_config_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    input_template_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    runtime_adapter: Mapped[str | None] = mapped_column(String(64), nullable=True)
    max_concurrent_executions: Mapped[int] = mapped_column(Integer, default=1)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    next_fire_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fire_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow = relationship("WorkflowORM")


class ScheduleFireClaimORM(TimestampMixin, Base):
    __tablename__ = "schedule_fire_claims"
    __table_args__ = (
        UniqueConstraint("schedule_id", "scheduled_fire_at", name="uq_schedule_fire_claims_schedule_fire_at"),
        Index("ix_schedule_fire_claims_schedule_id", "schedule_id"),
        Index("ix_schedule_fire_claims_status", "status"),
        Index("ix_schedule_fire_claims_lease_expires_at", "lease_expires_at"),
        Index("ix_schedule_fire_claims_execution_id", "execution_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False)
    scheduled_fire_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    lease_expires_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="claimed")
    execution_id: Mapped[str | None] = mapped_column(ForeignKey("executions.id"), nullable=True)

    schedule = relationship("ScheduleORM")
    execution = relationship("ExecutionORM")
