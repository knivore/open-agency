"""Domain contracts for Persona Factory packages and governance state.

Other agent ecosystems often call a similar reusable package a "skill"; Agency
uses "persona" because the package can model identity, style, memory, workflow,
and expertise rather than only professional capability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pydantic import Field, model_validator
from typing import Any
from uuid import uuid4

from .credentials import DomainModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PersonaStatus(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class PersonaVersionStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class PersonaSourceType(str, Enum):
    DOCUMENT = "document"
    MEMORY = "memory"
    CONVERSATION = "conversation"
    UPLOAD = "upload"
    URL = "url"
    MANUAL = "manual"


class PersonaDistillationStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    FAILED = "failed"


class PersonaDistillationMode(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"
    HYBRID = "hybrid"


class PersonaLLMModelSource(str, Enum):
    MAIN_AGENT = "main_agent"
    MODEL_PROFILE = "model_profile"
    MODEL = "model"


class PersonaDistillationItemType(str, Enum):
    DOMAIN_KNOWLEDGE = "domain_knowledge"
    PROCEDURE = "procedure"
    DECISION_PATTERN = "decision_pattern"
    WRITING_STYLE = "writing_style"
    TOOL_USAGE = "tool_usage"
    WORKFLOW = "workflow"
    EXAMPLE = "example"
    GUARDRAIL = "guardrail"
    SOCIAL_CONTEXT = "social_context"
    SOURCE_REFERENCE = "source_reference"


class PersonaMemoryLayer(str, Enum):
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    EPISODIC = "episodic"
    PERSONA = "persona"
    TOOL = "tool"
    SOCIAL = "social"


class PersonaDistillationItemReviewStatus(str, Enum):
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class PersonaDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    slug: str
    name: str
    description: str | None = None
    status: PersonaStatus = PersonaStatus.DRAFT
    created_by_user_id: str | None = None
    workspace_id: str | None = None
    current_version_id: str | None = None
    published_agent_id: str | None = None
    published_workflow_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_persona(self) -> "PersonaDefinition":
        self.slug = self.slug.strip().lower()
        self.name = self.name.strip()
        if not self.slug:
            raise ValueError("Persona slug is required.")
        if not self.name:
            raise ValueError("Persona name is required.")
        return self


class PersonaVersion(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    persona_id: str
    version: str = "1.0.0"
    status: PersonaVersionStatus = PersonaVersionStatus.DRAFT
    package: dict[str, Any] = Field(default_factory=dict)
    generated_from_run_id: str | None = None
    approved_by_user_id: str | None = None
    published_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_version(self) -> "PersonaVersion":
        if not self.persona_id.strip():
            raise ValueError("Persona version requires persona_id.")
        if not self.version.strip():
            raise ValueError("Persona version label is required.")
        return self


class PersonaSource(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    persona_id: str
    source_type: PersonaSourceType
    source_id: str | None = None
    filename: str | None = None
    content_sha256: str | None = None
    storage_uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_source(self) -> "PersonaSource":
        if not self.persona_id.strip():
            raise ValueError("Persona source requires persona_id.")
        return self


class PersonaDistillationRun(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    persona_id: str
    status: PersonaDistillationStatus = PersonaDistillationStatus.QUEUED
    distillation_mode: PersonaDistillationMode = PersonaDistillationMode.DETERMINISTIC
    llm_model_source: PersonaLLMModelSource | None = None
    model_profile_id: str | None = None
    llm_model_provider: str | None = None
    llm_model: str | None = None
    resolved_model_provider: str | None = None
    resolved_model: str | None = None
    resolved_model_profile_id: str | None = None
    input_source_ids: list[str] = Field(default_factory=list)
    output_package: dict[str, Any] = Field(default_factory=dict)
    distillation_metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_run(self) -> "PersonaDistillationRun":
        if not self.persona_id.strip():
            raise ValueError("Persona distillation run requires persona_id.")
        if self.distillation_mode == PersonaDistillationMode.DETERMINISTIC:
            self.llm_model_source = None
        return self


class PersonaDistillationItem(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    persona_id: str
    source_memory_id: str | None = None
    item_type: PersonaDistillationItemType
    memory_layer: PersonaMemoryLayer
    title: str
    content: str
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    needs_review: bool = True
    review_status: PersonaDistillationItemReviewStatus = PersonaDistillationItemReviewStatus.NEEDS_REVIEW
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_item(self) -> "PersonaDistillationItem":
        if not self.run_id.strip():
            raise ValueError("Persona distillation item requires run_id.")
        if not self.persona_id.strip():
            raise ValueError("Persona distillation item requires persona_id.")
        self.title = self.title.strip()
        self.content = self.content.strip()
        if not self.title:
            raise ValueError("Persona distillation item title is required.")
        if not self.content:
            raise ValueError("Persona distillation item content is required.")
        if self.review_status == PersonaDistillationItemReviewStatus.NEEDS_REVIEW:
            self.needs_review = True
        return self


__all__ = [
    "PersonaDefinition",
    "PersonaDistillationItem",
    "PersonaDistillationItemReviewStatus",
    "PersonaDistillationMode",
    "PersonaDistillationItemType",
    "PersonaDistillationRun",
    "PersonaDistillationStatus",
    "PersonaLLMModelSource",
    "PersonaMemoryLayer",
    "PersonaSource",
    "PersonaSourceType",
    "PersonaStatus",
    "PersonaVersion",
    "PersonaVersionStatus",
]
