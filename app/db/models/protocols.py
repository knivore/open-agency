from __future__ import annotations

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class RuntimeAdapterORM(TimestampMixin, Base):
    __tablename__ = "runtime_adapters"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    unavailable_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    config_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)


class MCPServerORM(TimestampMixin, Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    transport: Mapped[str] = mapped_column(String(64), nullable=False)
    command: Mapped[str | None] = mapped_column(Text(), nullable=True)
    url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    env_refs_json: Mapped[list] = mapped_column(JSON_VARIANT, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    security_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)


class A2AAgentORM(TimestampMixin, Base):
    __tablename__ = "a2a_agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_card_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    agent_card_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    security_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
