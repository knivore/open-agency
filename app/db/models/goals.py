"""ORM mappings for durable goal supervision state."""

from __future__ import annotations

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class GoalORM(TimestampMixin, Base):
    __tablename__ = "goals"
    __table_args__ = (
        Index("ix_goals_status", "status"),
        Index("ix_goals_parent_goal_id", "parent_goal_id"),
        Index("ix_goals_created_at", "created_at"),
        Index("ix_goals_deadline_at", "deadline_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    objective: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[str] = mapped_column(String(64), nullable=False, default="normal")
    owner_actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parent_goal_id: Mapped[str | None] = mapped_column(ForeignKey("goals.id"), nullable=True)
    success_criteria_json: Mapped[list] = mapped_column(JSON_VARIANT, default=list)
    constraints_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    execution_ids_json: Mapped[list] = mapped_column(JSON_VARIANT, default=list)
    evidence_json: Mapped[list] = mapped_column(JSON_VARIANT, default=list)
    evaluation_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    deadline_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)

    parent_goal = relationship("GoalORM", remote_side="GoalORM.id")
    executions = relationship("ExecutionORM", back_populates="goal")
