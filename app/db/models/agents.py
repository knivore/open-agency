from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class AgentORM(TimestampMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (Index("ix_agents_enabled", "enabled"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text(), nullable=True)
    role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    backstory: Mapped[str | None] = mapped_column(Text(), nullable=True)
    model_profile_id: Mapped[str | None] = mapped_column(ForeignKey("model_profiles.id"), nullable=True)
    tool_ids_json: Mapped[list] = mapped_column(JSON_VARIANT, default=list)
    handoff_agent_ids_json: Mapped[list] = mapped_column(JSON_VARIANT, default=list)
    guardrails_json: Mapped[list] = mapped_column(JSON_VARIANT, default=list)
    memory_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    framework_hints_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    model_profile = relationship("ModelProfileORM", back_populates="agents")
