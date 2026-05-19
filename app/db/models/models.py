from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class ModelProviderORM(TimestampMixin, Base):
    __tablename__ = "model_providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)

    profiles = relationship("ModelProfileORM", back_populates="provider")


class ModelProfileORM(TimestampMixin, Base):
    __tablename__ = "model_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("model_providers.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    temperature: Mapped[float | None] = mapped_column(nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(nullable=True)
    context_window: Mapped[int | None] = mapped_column(nullable=True)
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_structured_output: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_streaming: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)

    provider = relationship("ModelProviderORM", back_populates="profiles")
    agents = relationship("AgentORM", back_populates="model_profile")


class MemorySourceORM(TimestampMixin, Base):
    __tablename__ = "memory_sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PromptTemplateORM(TimestampMixin, Base):
    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    template_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    variables_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
