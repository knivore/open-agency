"""ORM mapping for built-in and user-defined tool contracts."""

from __future__ import annotations

from sqlalchemy import Boolean, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class ToolORM(TimestampMixin, Base):
    __tablename__ = "tools"
    __table_args__ = (Index("ix_tools_tool_type", "tool_type"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str] = mapped_column(Text(), nullable=False)
    tool_type: Mapped[str] = mapped_column(String(64), nullable=False)
    input_schema_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    output_schema_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    implementation_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    security_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    mcp_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
