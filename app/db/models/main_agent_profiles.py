from __future__ import annotations

from sqlalchemy import Boolean, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class MainAgentProfileORM(TimestampMixin, Base):
    __tablename__ = "main_agent_profiles"
    __table_args__ = (
        Index("ix_main_agent_profiles_enabled", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    default_workflow_id: Mapped[str] = mapped_column(String(64), nullable=False)
    default_model_profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    policy_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
