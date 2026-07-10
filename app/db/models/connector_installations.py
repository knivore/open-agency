from __future__ import annotations

from datetime import datetime
from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class ConnectorInstallationORM(TimestampMixin, Base):
    __tablename__ = "connector_installations"
    __table_args__ = (
        Index("ix_connector_installations_owner_user_id", "owner_user_id"),
        Index("ix_connector_installations_provider", "provider"),
        UniqueConstraint(
            "owner_user_id",
            "provider",
            "onecli_credential_ref",
            name="uq_connector_installations_owner_provider_onecli_ref",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    onecli_credential_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    runtime_secret_encrypted: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="setup_pending")
    setup_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
