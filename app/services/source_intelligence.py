"""Shared source classification for memory ingestion, Persona Factory, and graph hints."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Any

from app.api.context import ApiContext
from app.core.config import get_settings
from app.core.time import utc_now
from app.domain import (
    GraphProjectionEvent,
    MemoryRecord,
    MemoryStatus,
    PersonaDistillationItemType,
    PersonaMemoryLayer,
    ModelProfileDefinition,
    UserDefinition,
)
from app.llm.base import ModelMessage
from app.services.persona_distillation_pipeline import (
    PersonaDistillationPipeline,
    PersonaSourceClassification,
    SOURCE_CLASSIFICATIONS,
)

SOURCE_INTELLIGENCE_REVIEW_STATUSES = {"draft", "needs_review", "approved", "rejected"}
SOURCE_INTELLIGENCE_DOCUMENT_KINDS = {
    "policy_sop",
    "email_thread",
    "chat_export",
    "ticket",
    "code",
    "meeting_note",
    "workpaper",
    "report",
    "manual_note",
    "unknown",
}
SOURCE_INTELLIGENCE_GRAPH_ENTITY_LABELS = {
    "Person",
    "Knowledge",
    "Tool",
    "Workflow",
    "Artifact",
    "Decision",
    "Event",
    "Organization",
    "Persona",
}
SOURCE_INTELLIGENCE_GRAPH_RELATIONSHIP_TYPES = {
    "KNOWS",
    "USES",
    "FOLLOWS",
    "PRODUCES",
    "REVIEWS",
    "APPROVES",
    "ESCALATES_TO",
    "PARTICIPATES_IN",
    "DERIVED_FROM",
    "RELATES_TO",
}


class SourceIntelligenceError(ValueError):
    pass


def _normalized_graph_entity_label(label: str) -> str:
    normalized = " ".join(label.strip().replace("_", " ").replace("-", " ").split()).lower()
    if not normalized:
        return "Knowledge"
    if "tool" in normalized:
        return "Tool"
    if any(token in normalized for token in ("workflow", "process", "procedure")):
        return "Workflow"
    if "artifact" in normalized or "document" in normalized or "ticket" in normalized or "workpaper" in normalized:
        return "Artifact"
    if "decision" in normalized or "rule" in normalized:
        return "Decision"
    if "event" in normalized or "meeting" in normalized:
        return "Event"
    if "organization" in normalized or "team" in normalized or "company" in normalized:
        return "Organization"
    if "person" in normalized or "user" in normalized or "owner" in normalized:
        return "Person"
    if "persona" in normalized or "style" in normalized or "preference" in normalized:
        return "Persona"
    return "Knowledge"


class SourceIntelligenceGraphEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    name: str
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    evidence: str | None = None

    @model_validator(mode="after")
    def validate_entity(self) -> "SourceIntelligenceGraphEntity":
        if self.label not in SOURCE_INTELLIGENCE_GRAPH_ENTITY_LABELS:
            self.label = _normalized_graph_entity_label(self.label)
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Graph entity name is required.")
        if self.evidence:
            self.evidence = self.evidence.strip()[:500]
        return self


class SourceIntelligenceGraphRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_name: str
    relationship_type: str
    target_name: str
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    evidence: str | None = None

    @model_validator(mode="after")
    def validate_relationship(self) -> "SourceIntelligenceGraphRelationship":
        if self.relationship_type not in SOURCE_INTELLIGENCE_GRAPH_RELATIONSHIP_TYPES:
            self.relationship_type = "RELATES_TO"
        self.source_name = self.source_name.strip()
        self.target_name = self.target_name.strip()
        if not self.source_name or not self.target_name:
            raise ValueError("Graph relationship source_name and target_name are required.")
        if self.evidence:
            self.evidence = self.evidence.strip()[:500]
        return self


class SourceIntelligencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    confidence: float = Field(ge=0.0, le=1.0)
    signals: list[str] = Field(default_factory=list)
    document_kind: str = "unknown"
    content_roles: list[str] = Field(default_factory=list)
    extraction_targets: list[str] = Field(default_factory=list)
    memory_layers: list[str] = Field(default_factory=list)
    vector_tags: list[str] = Field(default_factory=list)
    graph_entities: list[SourceIntelligenceGraphEntity] = Field(default_factory=list)
    graph_relationships: list[SourceIntelligenceGraphRelationship] = Field(default_factory=list)
    should_include: bool = True
    rationale: str | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "SourceIntelligencePayload":
        if self.label not in SOURCE_CLASSIFICATIONS:
            raise ValueError(f"Unsupported source classification label '{self.label}'.")
        if self.document_kind not in SOURCE_INTELLIGENCE_DOCUMENT_KINDS:
            raise ValueError(f"Unsupported document kind '{self.document_kind}'.")
        self.signals = self._string_values(self.signals)[:20]
        self.content_roles = self._validated_values(
            self.content_roles,
            SOURCE_CLASSIFICATIONS,
            "content role",
            strict=False,
        )[:12]
        self.extraction_targets = self._validated_values(
            self.extraction_targets,
            {item.value for item in PersonaDistillationItemType},
            "extraction target",
        )[:12]
        self.memory_layers = self._validated_values(
            self.memory_layers,
            {item.value for item in PersonaMemoryLayer},
            "memory layer",
        )[:8]
        self.vector_tags = [item.lower() for item in self._string_values(self.vector_tags)][:20]
        if self.rationale:
            self.rationale = self.rationale.strip()[:1000]
        return self

    @staticmethod
    def _string_values(values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            item = value.strip()
            if item not in normalized:
                normalized.append(item)
        return normalized

    @staticmethod
    def _validated_values(values: list[str], allowed: set[str], label: str, *, strict: bool = True) -> list[str]:
        normalized: list[str] = []
        for item in SourceIntelligencePayload._string_values(values):
            if item not in allowed:
                if strict:
                    raise ValueError(f"Unsupported {label} '{item}'.")
                continue
            normalized.append(item)
        return normalized


@dataclass(slots=True)
class SourceIntelligenceService:
    context: ApiContext

    @staticmethod
    def catalog() -> dict[str, Any]:
        return {
            "source_classifications": sorted(SOURCE_CLASSIFICATIONS),
            "document_kinds": sorted(SOURCE_INTELLIGENCE_DOCUMENT_KINDS),
            "item_types": [item.value for item in PersonaDistillationItemType],
            "memory_layers": [item.value for item in PersonaMemoryLayer],
            "graph_entity_labels": sorted(SOURCE_INTELLIGENCE_GRAPH_ENTITY_LABELS),
            "graph_relationship_types": sorted(SOURCE_INTELLIGENCE_GRAPH_RELATIONSHIP_TYPES),
            "review_statuses": sorted(SOURCE_INTELLIGENCE_REVIEW_STATUSES),
        }

    async def classify_memory(
            self,
            memory: MemoryRecord,
            *,
            model_profile_id: str | None = None,
            model_profile: ModelProfileDefinition | None = None,
            purpose: str = "memory",
    ) -> PersonaSourceClassification:
        pipeline = PersonaDistillationPipeline()
        deterministic = pipeline.classify(memory)
        if model_profile is None and not model_profile_id:
            return deterministic
        # Explicit provider/model selections may be represented by a transient profile that
        # is valid for one run but is not persisted in the model-profile repository.
        profile = model_profile or await self._resolve_required_model_profile(model_profile_id)
        payload = await self._generate_structured_with_profile(
            profile=profile,
            schema_name="source_intelligence_classification",
            schema=SourceIntelligencePayload.model_json_schema(),
            system=(
                "You classify one Agency source chunk before memory or persona extraction. "
                "Return only schema-valid JSON. Choose labels, document kind, extraction targets, memory layers, "
                "vector tags, and graph hints from the provided enums. Do not infer unsupported private facts."
            ),
            prompt=json.dumps(
                {
                    "purpose": purpose,
                    "allowed_labels": sorted(SOURCE_CLASSIFICATIONS),
                    "allowed_document_kinds": sorted(SOURCE_INTELLIGENCE_DOCUMENT_KINDS),
                    "allowed_extraction_targets": [item.value for item in PersonaDistillationItemType],
                    "allowed_memory_layers": [item.value for item in PersonaMemoryLayer],
                    "allowed_graph_entity_labels": sorted(SOURCE_INTELLIGENCE_GRAPH_ENTITY_LABELS),
                    "allowed_graph_relationship_types": sorted(SOURCE_INTELLIGENCE_GRAPH_RELATIONSHIP_TYPES),
                    "deterministic_fallback": deterministic.as_payload(),
                    "source_ref": self.source_ref(memory),
                    "memory": {
                        "id": memory.id,
                        "summary": memory.summary,
                        "content": memory.content[:6000],
                        "tags": memory.tags,
                        "memory_type": memory.memory_type.value if memory.memory_type else None,
                        "sensitive": memory.sensitive,
                        "metadata": memory.metadata,
                    },
                },
                ensure_ascii=True,
            ),
        )
        try:
            classification = SourceIntelligencePayload.model_validate(payload)
        except ValueError as exc:
            raise SourceIntelligenceError(f"Source intelligence output failed schema validation: {exc}") from exc
        return PersonaSourceClassification(
            label=classification.label,
            confidence=classification.confidence,
            signals=["llm_classifier", *classification.signals],
            document_kind=classification.document_kind,
            content_roles=classification.content_roles,
            extraction_targets=classification.extraction_targets,
            memory_layers=classification.memory_layers,
            vector_tags=classification.vector_tags,
            graph_entities=[item.model_dump(mode="json") for item in classification.graph_entities],
            graph_relationships=[item.model_dump(mode="json") for item in classification.graph_relationships],
            should_include=classification.should_include,
            rationale=classification.rationale,
        )

    async def analyze_memories(
            self,
            *,
            memory_ids: list[str],
            model_profile_id: str | None,
            persist: bool,
            current_user: UserDefinition | None,
    ) -> dict[str, Any]:
        from app.services.memory import MemoryService

        memory_service = MemoryService(self.context)
        items: list[dict[str, Any]] = []
        for memory_id in list(dict.fromkeys(memory_ids)):
            memory = await memory_service.get_memory(memory_id, current_user=current_user)
            if memory is None:
                raise SourceIntelligenceError(f"Memory '{memory_id}' not found.")
            classification = await self.classify_memory(
                memory,
                model_profile_id=model_profile_id,
                purpose="memory",
            )
            metadata_patch = self.memory_metadata_patch(
                memory=memory,
                classification=classification,
                model_profile_id=model_profile_id,
            )
            updated = None
            if persist:
                updated = await memory_service.update_memory(
                    memory.id,
                    {"metadata": {**memory.metadata, **metadata_patch}},
                    confirmed=True,
                    current_user=current_user,
                )
            items.append(
                {
                    "memory_id": memory.id,
                    "source_intelligence": metadata_patch["source_intelligence"],
                    "graph_hints": metadata_patch["graph_hints"],
                    "memory": updated.model_dump(mode="json") if updated is not None else None,
                }
            )
        return {"items": items}

    async def review_memory_source_intelligence(
            self,
            *,
            memory_id: str,
            source_intelligence: dict[str, Any] | None,
            graph_hints: dict[str, Any] | None,
            source_intelligence_review_status: str | None,
            graph_hints_review_status: str | None,
            review_note: str | None,
            current_user: UserDefinition | None,
    ) -> MemoryRecord:
        from app.services.memory import MemoryService

        memory_service = MemoryService(self.context)
        memory = await memory_service.get_memory(memory_id, current_user=current_user)
        if memory is None:
            raise SourceIntelligenceError(f"Memory '{memory_id}' not found.")
        metadata = dict(memory.metadata or {})
        current_source_intelligence = dict(metadata.get("source_intelligence") or {})
        current_graph_hints = dict(metadata.get("graph_hints") or {})
        if source_intelligence is not None:
            current_source_intelligence = source_intelligence
        if graph_hints is not None:
            current_graph_hints = graph_hints
        if source_intelligence_review_status:
            current_source_intelligence["review_status"] = self._review_status(source_intelligence_review_status)
        if graph_hints_review_status:
            current_graph_hints["review_status"] = self._review_status(graph_hints_review_status)
        review = {
            "reviewed_at": utc_now().isoformat(),
            "reviewed_by_user_id": current_user.id if current_user else None,
        }
        if review_note:
            review["note"] = review_note
        current_source_intelligence["review"] = review
        current_graph_hints["review"] = review
        metadata["source_intelligence"] = current_source_intelligence
        metadata["graph_hints"] = current_graph_hints
        updated = await memory_service.update_memory(
            memory.id,
            {"metadata": metadata},
            confirmed=True,
            current_user=current_user,
        )
        if updated is None:
            raise SourceIntelligenceError(f"Memory '{memory_id}' not found.")
        await self._append_approved_graph_hints_event(updated)
        return updated

    @staticmethod
    def memory_metadata_patch(
            *,
            memory: MemoryRecord,
            classification: PersonaSourceClassification,
            model_profile_id: str | None,
    ) -> dict[str, Any]:
        classified_at = utc_now().isoformat()
        source_intelligence = classification.as_payload()
        review_status = "needs_review" if classification.confidence < 0.8 or classification.graph_entities or classification.graph_relationships else "draft"
        return {
            "source_intelligence": {
                "schema_version": 1,
                "classifier": "llm" if model_profile_id else "deterministic",
                "model_profile_id": model_profile_id,
                "classified_at": classified_at,
                "source_ref": SourceIntelligenceService.source_ref(memory),
                "review_status": review_status,
                "classification": source_intelligence,
            },
            "graph_hints": {
                "schema_version": 1,
                "source": "source_intelligence",
                "classified_at": classified_at,
                "review_status": (
                    "needs_review"
                    if classification.graph_entities or classification.graph_relationships
                    else "draft"
                ),
                "entities": classification.graph_entities,
                "relationships": classification.graph_relationships,
            },
            "vector_tags": classification.vector_tags,
        }

    @staticmethod
    def source_ref(memory: MemoryRecord) -> dict[str, Any]:
        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
        return {
            "memory_id": memory.id,
            "document_id": metadata.get("document_id"),
            "filename": metadata.get("filename"),
            "chunk_index": metadata.get("chunk_index"),
            "confidence": 0.75,
        }

    async def _append_approved_graph_hints_event(self, memory: MemoryRecord) -> None:
        settings = get_settings()
        if not settings.graph_projection_enabled or memory.status != MemoryStatus.ACTIVE:
            return
        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
        graph_hints = metadata.get("graph_hints") if isinstance(metadata.get("graph_hints"), dict) else {}
        if graph_hints.get("review_status") != "approved":
            return
        entities = graph_hints.get("entities")
        relationships = graph_hints.get("relationships")
        if not isinstance(entities, list):
            entities = []
        if not isinstance(relationships, list):
            relationships = []
        if not entities and not relationships:
            return
        repo = getattr(self.context, "graph_projection_event_repo", None)
        if repo is None:
            return
        source_intelligence = (
            metadata.get("source_intelligence")
            if isinstance(metadata.get("source_intelligence"), dict)
            else {}
        )
        await repo.append(
            GraphProjectionEvent(
                event_type="memory.source_intelligence.graph_hints.approved",
                aggregate_type="memory",
                aggregate_id=memory.id,
                user_id=memory.created_by_user_id,
                payload={
                    "memory_id": memory.id,
                    "document_id": metadata.get("document_id"),
                    "filename": metadata.get("filename"),
                    "chunk_index": metadata.get("chunk_index"),
                    "status": memory.status.value,
                    "source_ref": self.source_ref(memory),
                    "source_intelligence": source_intelligence,
                    "graph_hints": graph_hints,
                    "entities": entities,
                    "relationships": relationships,
                    "review": graph_hints.get("review"),
                },
                source="source_intelligence",
                source_event_id=self._graph_hints_source_event_id(memory.id, entities, relationships),
            )
        )

    @staticmethod
    def _graph_hints_source_event_id(memory_id: str, entities: list[Any], relationships: list[Any]) -> str:
        payload = json.dumps(
            {"entities": entities, "relationships": relationships},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
        memory_digest = hashlib.sha1(memory_id.encode("utf-8")).hexdigest()[:16]
        return f"memory-graph-hints:{memory_digest}:{digest}"

    async def _resolve_required_model_profile(self, model_profile_id: str) -> ModelProfileDefinition:
        profile = await self.context.model_profile_repo.get(model_profile_id)
        if profile is None:
            raise SourceIntelligenceError(f"Model profile '{model_profile_id}' not found.")
        return profile

    async def _generate_structured_with_profile(
            self,
            *,
            profile: ModelProfileDefinition,
            schema_name: str,
            schema: dict[str, Any],
            system: str,
            prompt: str,
    ) -> dict[str, Any]:
        try:
            client = self.context.llm_provider_registry.resolve(profile)
        except Exception as exc:
            raise SourceIntelligenceError(f"Model profile '{profile.id}' could not be resolved: {exc}") from exc
        messages = [
            ModelMessage(role="system", content=system),
            ModelMessage(role="user", content=prompt),
        ]
        try:
            if hasattr(client, "agenerate_structured"):
                response = await client.agenerate_structured(
                    messages,
                    schema=schema,
                    schema_name=schema_name,
                    temperature=profile.temperature,
                    max_tokens=profile.max_tokens,
                )
            else:
                response = await asyncio.to_thread(
                    client.generate_structured,
                    messages,
                    schema=schema,
                    schema_name=schema_name,
                    temperature=profile.temperature,
                    max_tokens=profile.max_tokens,
                )
        except Exception as exc:
            raise SourceIntelligenceError(f"Source intelligence model call failed: {exc}") from exc
        if not isinstance(response.content, dict):
            raise SourceIntelligenceError(f"Structured model response '{schema_name}' was not an object.")
        return response.content

    @staticmethod
    def _review_status(value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SOURCE_INTELLIGENCE_REVIEW_STATUSES:
            raise SourceIntelligenceError(
                f"Unsupported review status '{value}'. "
                f"Allowed values: {', '.join(sorted(SOURCE_INTELLIGENCE_REVIEW_STATUSES))}."
            )
        return normalized


__all__ = [
    "SOURCE_INTELLIGENCE_DOCUMENT_KINDS",
    "SOURCE_INTELLIGENCE_GRAPH_ENTITY_LABELS",
    "SOURCE_INTELLIGENCE_GRAPH_RELATIONSHIP_TYPES",
    "SOURCE_INTELLIGENCE_REVIEW_STATUSES",
    "SourceIntelligenceError",
    "SourceIntelligencePayload",
    "SourceIntelligenceService",
]
