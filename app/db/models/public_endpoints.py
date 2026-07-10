from __future__ import annotations

from datetime import datetime
from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class PublicEndpointORM(TimestampMixin, Base):
    __tablename__ = "public_endpoints"
    __table_args__ = (
        Index("ix_public_endpoints_endpoint_type", "endpoint_type"),
        Index("ix_public_endpoints_provider", "provider"),
        Index("ix_public_endpoints_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="launcher")
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
