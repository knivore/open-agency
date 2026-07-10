"""ORM mappings for Persona Factory packages, extracted items, versions, sources, and runs."""

from __future__ import annotations

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.common import JSON_VARIANT, TimestampMixin


class PersonaORM(TimestampMixin, Base):
    __tablename__ = "personas"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_personas_slug"),
        Index("ix_personas_status", "status"),
        Index("ix_personas_created_by_user_id", "created_by_user_id"),
        Index("ix_personas_workspace_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_by_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_workflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)


class PersonaVersionORM(TimestampMixin, Base):
    __tablename__ = "persona_versions"
    __table_args__ = (
        UniqueConstraint("persona_id", "version", name="uq_persona_versions_persona_id_version"),
        Index("ix_persona_versions_persona_id", "persona_id"),
        Index("ix_persona_versions_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    persona_id: Mapped[str] = mapped_column(ForeignKey("personas.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    package_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    generated_from_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PersonaSourceORM(TimestampMixin, Base):
    __tablename__ = "persona_sources"
    __table_args__ = (
        Index("ix_persona_sources_persona_id", "persona_id"),
        Index("ix_persona_sources_source", "source_type", "source_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    persona_id: Mapped[str] = mapped_column(ForeignKey("personas.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    storage_uri: Mapped[str | None] = mapped_column(Text(), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)


class PersonaDistillationRunORM(TimestampMixin, Base):
    __tablename__ = "persona_distillation_runs"
    __table_args__ = (
        Index("ix_persona_distillation_runs_persona_id", "persona_id"),
        Index("ix_persona_distillation_runs_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    persona_id: Mapped[str] = mapped_column(ForeignKey("personas.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    distillation_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic")
    llm_model_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_model_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved_model_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved_model_profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_source_ids_json: Mapped[list] = mapped_column(JSON_VARIANT, default=list)
    output_package_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    distillation_metrics_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    warnings_json: Mapped[list] = mapped_column(JSON_VARIANT, default=list)
    errors_json: Mapped[list] = mapped_column(JSON_VARIANT, default=list)
    completed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PersonaDistillationItemORM(TimestampMixin, Base):
    __tablename__ = "persona_distillation_items"
    __table_args__ = (
        Index("ix_persona_distillation_items_run_id", "run_id"),
        Index("ix_persona_distillation_items_persona_id", "persona_id"),
        Index("ix_persona_distillation_items_source_memory_id", "source_memory_id"),
        Index("ix_persona_distillation_items_type_layer", "item_type", "memory_layer"),
        Index("ix_persona_distillation_items_review_status", "review_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("persona_distillation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    persona_id: Mapped[str] = mapped_column(ForeignKey("personas.id", ondelete="CASCADE"), nullable=False)
    source_memory_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    item_type: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_layer: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    structured_payload_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)
    confidence: Mapped[float] = mapped_column(nullable=False, default=0.5)
    needs_review: Mapped[bool] = mapped_column(nullable=False, default=True)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="needs_review")
    metadata_json: Mapped[dict] = mapped_column(JSON_VARIANT, default=dict)


__all__ = [
    "PersonaDistillationItemORM",
    "PersonaDistillationRunORM",
    "PersonaORM",
    "PersonaSourceORM",
    "PersonaVersionORM",
]
