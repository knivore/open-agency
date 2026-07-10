from __future__ import annotations

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class OneCLIIdentityMappingORM(TimestampMixin, Base):
    __tablename__ = "onecli_identity_mappings"
    __table_args__ = (
        Index("ix_onecli_identity_mappings_owner_user_id", "owner_user_id"),
        Index("ix_onecli_identity_mappings_workflow_id", "workflow_id"),
        UniqueConstraint("onecli_agent_id", name="uq_onecli_identity_mappings_onecli_agent_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    onecli_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_token_secret_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="active")
    workflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
