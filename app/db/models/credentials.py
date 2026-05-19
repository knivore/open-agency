from __future__ import annotations

from datetime import datetime
from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class CredentialORM(TimestampMixin, Base):
    __tablename__ = "credentials"
    __table_args__ = (Index("ix_credentials_owner_user_id", "owner_user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_ref: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="active")
    last_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    secret_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rotation_policy_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
