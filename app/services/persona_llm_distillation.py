"""Schema contracts for LLM-backed Persona Factory distillation.

LLM distillers must emit this candidate shape before Persona Factory converts
anything into reviewable `PersonaDistillationItem` records. Keeping the schema
separate from the engine makes validation, eval fixtures, and hybrid merging
usable before live model orchestration exists.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pydantic import Field, field_validator, model_validator
from pydantic import ValidationError
from typing import Any, Literal

from app.domain import MemoryRecord, ModelProfileDefinition, PersonaDistillationItemType, PersonaMemoryLayer
from app.domain.credentials import DomainModel
from app.services.persona_distillation_pipeline import PersonaDistillationCandidate, PersonaSourceClassification

LLM_DISTILLATION_CANDIDATE_SCHEMA_VERSION = 1
LLM_DISTILLATION_EXTRACTOR = "llm-distillation-v1"
LLM_DISTILLATION_PROMPT_VERSION = "persona-llm-distill-v1"
LLM_DISTILLER_VERSION = "llm-distillers-v1"
LLM_DISTILLATION_SCHEMA_NAME = "persona_llm_distillation_candidates"
LLM_MAX_CANDIDATES_PER_SOURCE = 12
LLM_MAX_TITLE_CHARS = 160
LLM_MAX_CONTENT_CHARS = 6000
LLM_MAX_EVIDENCE_CHARS = 1200
LLM_MAX_STRUCTURED_PAYLOAD_BYTES = 20_000
LLM_MAX_REVIEW_REASONS = 20
LLM_MAX_CONFLICT_SIGNALS = 20
LLM_MAX_GRAPH_HINTS = 24
LLM_LOW_CONFIDENCE_BROAD_EXTRACTION_THRESHOLD = 0.8
LLM_EVIDENCE_FUZZY_MATCH_THRESHOLD = 0.72

_GRAPH_ENTITY_LABELS = {
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
_GRAPH_RELATIONSHIP_TYPES = {
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


class PersonaLLMDistillationError(ValueError):
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


class PersonaLLMSourceSpan(DomainModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_span(self) -> "PersonaLLMSourceSpan":
        if self.end <= self.start:
            raise ValueError("source_span.end must be greater than source_span.start.")
        return self


def _normalized_evidence_text(value: str) -> str:
    return " ".join(value.lower().split())


class PersonaLLMGraphEntityHint(DomainModel):
    label: str
    name: str
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    evidence: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_entity(self) -> "PersonaLLMGraphEntityHint":
        self.label = self.label.strip()
        self.name = self.name.strip()
        if self.label not in _GRAPH_ENTITY_LABELS:
            self.label = _normalized_graph_entity_label(self.label)
        if not self.name:
            raise ValueError("Graph entity name is required.")
        if self.evidence:
            self.evidence = self.evidence.strip()
        return self


class PersonaLLMGraphRelationshipHint(DomainModel):
    source_name: str
    relationship_type: str
    target_name: str
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    evidence: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_relationship(self) -> "PersonaLLMGraphRelationshipHint":
        self.source_name = self.source_name.strip()
        self.relationship_type = self.relationship_type.strip()
        self.target_name = self.target_name.strip()
        if self.relationship_type not in _GRAPH_RELATIONSHIP_TYPES:
            self.relationship_type = "RELATES_TO"
        if not self.source_name or not self.target_name:
            raise ValueError("Graph relationship source_name and target_name are required.")
        if self.evidence:
            self.evidence = self.evidence.strip()
        return self


class PersonaLLMDistillationCandidatePayload(DomainModel):
    item_type: PersonaDistillationItemType
    memory_layer: PersonaMemoryLayer
    title: str = Field(min_length=1, max_length=LLM_MAX_TITLE_CHARS)
    content: str = Field(min_length=1, max_length=LLM_MAX_CONTENT_CHARS)
    confidence: float = Field(ge=0.0, le=1.0)
    source_evidence: str = Field(min_length=1, max_length=LLM_MAX_EVIDENCE_CHARS)
    source_span: PersonaLLMSourceSpan | None = Field(...)
    review_reasons: list[str] = Field(default_factory=list, max_length=LLM_MAX_REVIEW_REASONS)
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    inference_type: Literal["extractive", "abstractive", "inferred", "normalized"] | None = None
    unsupported_claim_risk: float | None = Field(default=None, ge=0.0, le=1.0)
    conflict_signals: list[str] = Field(default_factory=list, max_length=LLM_MAX_CONFLICT_SIGNALS)
    suggested_graph_entities: list[PersonaLLMGraphEntityHint] = Field(
        default_factory=list,
        max_length=LLM_MAX_GRAPH_HINTS,
    )
    suggested_graph_relationships: list[PersonaLLMGraphRelationshipHint] = Field(
        default_factory=list,
        max_length=LLM_MAX_GRAPH_HINTS,
    )
    needs_review: bool = False

    @field_validator("title", "content", "source_evidence", mode="before")
    @classmethod
    def strip_required_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("review_reasons", "conflict_signals", mode="before")
    @classmethod
    def normalize_string_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            reason = item.strip()
            if reason and reason not in normalized:
                normalized.append(reason)
        return normalized

    @model_validator(mode="after")
    def validate_candidate(self) -> "PersonaLLMDistillationCandidatePayload":
        if not self.title:
            raise ValueError("title is required.")
        if not self.content:
            raise ValueError("content is required.")
        if not self.source_evidence:
            raise ValueError("source_evidence is required.")

        payload_bytes = len(json.dumps(self.structured_payload, ensure_ascii=True, sort_keys=True).encode("utf-8"))
        if payload_bytes > LLM_MAX_STRUCTURED_PAYLOAD_BYTES:
            raise ValueError(
                f"structured_payload exceeds {LLM_MAX_STRUCTURED_PAYLOAD_BYTES} bytes."
            )

        review_reasons = list(self.review_reasons)
        if len(self.source_evidence) < 12:
            review_reasons.append("weak_source_evidence")
        if self.source_span is None:
            review_reasons.append("missing_source_span")
        if self.unsupported_claim_risk is not None and self.unsupported_claim_risk >= 0.5:
            review_reasons.append("unsupported_claim_risk")
        if self.conflict_signals:
            review_reasons.append("conflict_signals")

        self.review_reasons = list(dict.fromkeys(review_reasons))[:LLM_MAX_REVIEW_REASONS]
        self.needs_review = self.needs_review or bool(
            {
                "weak_source_evidence",
                "missing_source_span",
                "unsupported_claim_risk",
                "conflict_signals",
            }.intersection(self.review_reasons)
        )
        return self

    @property
    def source_evidence_hash(self) -> str:
        return hashlib.sha256(self.source_evidence.encode("utf-8")).hexdigest()

    def ground_source_evidence(self, source_content: str) -> dict[str, Any]:
        provided_span = self.source_span.model_dump(mode="json") if self.source_span else None
        grounding: dict[str, Any] = {
            "text": self.source_evidence,
            "hash": self.source_evidence_hash,
            "provided_span": provided_span,
            "matched_span": None,
            "verified": False,
            "match_method": None,
            "match_score": 0.0,
            "verification_reason": None,
        }
        evidence = self.source_evidence.strip()
        if not source_content or not evidence:
            return self._mark_evidence_unverified(grounding, "missing_source_content")

        evidence_normalized = _normalized_evidence_text(evidence)
        if self.source_span is not None:
            start = self.source_span.start
            end = self.source_span.end
            if end <= len(source_content):
                span_text = source_content[start:end]
                span_normalized = _normalized_evidence_text(span_text)
                if evidence_normalized == span_normalized or evidence_normalized in span_normalized:
                    return {
                        **grounding,
                        "matched_span": {"start": start, "end": end},
                        "verified": True,
                        "match_method": "exact_span",
                        "match_score": 1.0,
                    }
                span_score = SequenceMatcher(None, evidence_normalized, span_normalized).ratio()
                if span_score >= LLM_EVIDENCE_FUZZY_MATCH_THRESHOLD:
                    return {
                        **grounding,
                        "matched_span": {"start": start, "end": end},
                        "verified": True,
                        "match_method": "fuzzy_span",
                        "match_score": round(span_score, 3),
                    }
                grounding["verification_reason"] = "source_span_mismatch"
            else:
                grounding["verification_reason"] = "source_span_out_of_bounds"

        exact_index = source_content.lower().find(evidence.lower())
        if exact_index >= 0:
            self._mark_evidence_verified()
            return {
                **grounding,
                "matched_span": {"start": exact_index, "end": exact_index + len(evidence)},
                "verified": True,
                "match_method": "exact_text",
                "match_score": 1.0,
                "verification_reason": grounding["verification_reason"],
            }

        fuzzy = self._fuzzy_evidence_match(source_content=source_content, evidence=evidence)
        if fuzzy is not None:
            return {
                **grounding,
                "matched_span": {"start": fuzzy["start"], "end": fuzzy["end"]},
                "verified": True,
                "match_method": "fuzzy_text",
                "match_score": fuzzy["score"],
                "verification_reason": grounding["verification_reason"],
            }
        return self._mark_evidence_unverified(grounding, grounding["verification_reason"] or "evidence_not_found")

    def _mark_evidence_unverified(self, grounding: dict[str, Any], reason: str) -> dict[str, Any]:
        self.needs_review = True
        self.review_reasons = list(dict.fromkeys([*self.review_reasons, "evidence_not_verified"]))
        grounding["verification_reason"] = reason
        return grounding

    def _mark_evidence_verified(self) -> None:
        if "missing_source_span" not in self.review_reasons:
            return
        self.review_reasons = [reason for reason in self.review_reasons if reason != "missing_source_span"]
        self.needs_review = bool(
            {
                "weak_source_evidence",
                "unsupported_claim_risk",
                "conflict_signals",
                "evidence_not_verified",
            }.intersection(self.review_reasons)
        )

    @staticmethod
    def _fuzzy_evidence_match(*, source_content: str, evidence: str) -> dict[str, Any] | None:
        evidence_normalized = _normalized_evidence_text(evidence)
        if len(evidence_normalized) < 12:
            return None
        target_len = len(evidence)
        window_sizes = sorted({target_len, max(12, int(target_len * 0.8)), int(target_len * 1.2)})
        step = max(1, target_len // 4)
        best: dict[str, Any] | None = None
        for window_size in window_sizes:
            if window_size <= 0:
                continue
            max_start = max(0, len(source_content) - window_size)
            for start in range(0, max_start + 1, step):
                end = min(len(source_content), start + window_size)
                candidate = source_content[start:end]
                score = SequenceMatcher(None, evidence_normalized, _normalized_evidence_text(candidate)).ratio()
                if best is None or score > best["score"]:
                    best = {"start": start, "end": end, "score": round(score, 3)}
        if best and best["score"] >= LLM_EVIDENCE_FUZZY_MATCH_THRESHOLD:
            return best
        return None

    def to_distillation_candidate(
            self,
            *,
            source_ref: dict[str, Any],
            evidence_grounding: dict[str, Any] | None = None,
            source_classification: dict[str, Any] | None = None,
            source_memory_id: str,
            model_provider: str | None,
            model_name: str | None,
            model_profile_id: str | None,
            prompt_version: str,
            distiller_name: str,
            distiller_version: str,
    ) -> PersonaDistillationCandidate:
        source_span = self.source_span.model_dump(mode="json") if self.source_span else None
        graph_entities = [item.model_dump(mode="json") for item in self.suggested_graph_entities]
        graph_relationships = [item.model_dump(mode="json") for item in self.suggested_graph_relationships]
        evidence = evidence_grounding or {
            "text": self.source_evidence,
            "hash": self.source_evidence_hash,
            "provided_span": source_span,
            "matched_span": source_span,
            "verified": None,
            "match_method": None,
            "match_score": None,
            "verification_reason": None,
        }
        source_ref_with_evidence = {**source_ref, "evidence": evidence}
        provenance = {
            "generated_by": "llm",
            "model_provider": model_provider,
            "model_name": model_name,
            "model_profile_id": model_profile_id,
            "prompt_version": prompt_version,
            "distiller": distiller_name,
            "distiller_version": distiller_version,
            "source_memory_id": source_memory_id,
            "source_evidence_hash": self.source_evidence_hash,
            "source_span": source_span,
        }
        structured_payload = {
            **self.structured_payload,
            "source_ref": source_ref_with_evidence,
            "source_classification": source_classification,
            "source_evidence": self.source_evidence,
            "source_span": evidence.get("matched_span") or source_span,
            "evidence_grounding": evidence,
            "inference_type": self.inference_type,
            "unsupported_claim_risk": self.unsupported_claim_risk,
            "conflict_signals": self.conflict_signals,
            "suggested_graph_entities": graph_entities,
            "suggested_graph_relationships": graph_relationships,
            "graph_hints_review_status": "pending_item_approval" if graph_entities or graph_relationships else None,
            "extractor": LLM_DISTILLATION_EXTRACTOR,
            "schema_version": LLM_DISTILLATION_CANDIDATE_SCHEMA_VERSION,
            "distiller": distiller_name,
            "distiller_version": distiller_version,
            "prompt_version": prompt_version,
            "review_flags": self.review_reasons,
            "provenance": provenance,
        }
        return PersonaDistillationCandidate(
            item_type=self.item_type,
            memory_layer=self.memory_layer,
            title=self.title,
            content=self.content,
            confidence=self.confidence,
            structured_payload=structured_payload,
            metadata={
                **provenance,
                "review_reasons": self.review_reasons,
                "unsupported_claim_risk": self.unsupported_claim_risk,
                "conflict_signals": self.conflict_signals,
                "has_graph_hints": bool(graph_entities or graph_relationships),
                "evidence_grounding": evidence,
            },
            needs_review=self.needs_review,
            distiller_name=distiller_name,
        )


class PersonaLLMDistillationBatchPayload(DomainModel):
    candidates: list[PersonaLLMDistillationCandidatePayload] = Field(
        default_factory=list,
        max_length=LLM_MAX_CANDIDATES_PER_SOURCE,
    )


StructuredGenerator = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class LLMSpecializedDistiller:
    name: str
    item_types: tuple[PersonaDistillationItemType, ...]
    memory_layers: tuple[PersonaMemoryLayer, ...]
    focus: str
    instructions: tuple[str, ...]

    def source_prompt_payload(
            self,
            *,
            engine: "LLMDistillationEngine",
            memory: MemoryRecord,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
    ) -> dict[str, Any]:
        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
        source_intelligence = (
            metadata.get("source_intelligence")
            if isinstance(metadata.get("source_intelligence"), dict)
            else {}
        )
        approved_source_intelligence = (
            source_intelligence
            if source_intelligence.get("review_status") == "approved"
            else None
        )
        return {
            "schema_version": LLM_DISTILLATION_CANDIDATE_SCHEMA_VERSION,
            "max_candidates": engine.max_candidates_per_source,
            "distiller": self.name,
            "distiller_focus": self.focus,
            "allowed_item_types": [item.value for item in self.item_types],
            "allowed_memory_layers": [item.value for item in self.memory_layers],
            "source_ref": source_ref,
            "source_classification": classification.as_payload(),
            "approved_source_intelligence": approved_source_intelligence,
            "memory": {
                "id": memory.id,
                "summary": memory.summary,
                "content": memory.content[:6000],
                "tags": memory.tags,
                "memory_type": memory.memory_type.value if memory.memory_type else None,
                "sensitive": memory.sensitive,
                "metadata": memory.metadata,
            },
            "instructions": [
                *self.instructions,
                "Use approved_source_intelligence for routing context only; do not treat it as a substitute for source evidence.",
                "Return only candidates with allowed_item_types and allowed_memory_layers.",
                "Use exact source_evidence text where possible and include source_span character offsets when known.",
                "Use suggested graph hints only for source-supported entities and relationships; hints remain review-only until the item is approved.",
                "Set unsupported_claim_risk above 0.5 when the candidate depends on inference beyond the cited evidence.",
            ],
        }

    def prepare_candidate(
            self,
            *,
            candidate: PersonaLLMDistillationCandidatePayload,
            source_content: str,
            evidence_grounding: dict[str, Any],
    ) -> None:
        if candidate.item_type not in self.item_types:
            allowed = ", ".join(item.value for item in self.item_types)
            raise PersonaLLMDistillationError(
                f"{self.name} returned unsupported item_type '{candidate.item_type.value}'. Allowed: {allowed}."
            )
        if candidate.memory_layer not in self.memory_layers:
            allowed = ", ".join(item.value for item in self.memory_layers)
            raise PersonaLLMDistillationError(
                f"{self.name} returned unsupported memory_layer '{candidate.memory_layer.value}'. Allowed: {allowed}."
            )
        if candidate.item_type in {PersonaDistillationItemType.WRITING_STYLE,
                                   PersonaDistillationItemType.SOCIAL_CONTEXT}:
            if not evidence_grounding.get("verified"):
                raise PersonaLLMDistillationError(
                    f"{self.name} returned a style/social candidate without source-supported evidence."
                )
        if candidate.item_type == PersonaDistillationItemType.TOOL_USAGE:
            candidate.needs_review = True
            candidate.review_reasons = list(
                dict.fromkeys([*candidate.review_reasons, "tool_usage_candidate_only", "tool_grant_requires_review"])
            )
            candidate.structured_payload = {
                **candidate.structured_payload,
                "review_policy": "tool_usage_candidate_only",
                "tool_grant": False,
            }


class LLMKnowledgeDistiller(LLMSpecializedDistiller):
    def __init__(self) -> None:
        super().__init__(
            name="llm_knowledge_distiller",
            item_types=(PersonaDistillationItemType.DOMAIN_KNOWLEDGE,),
            memory_layers=(PersonaMemoryLayer.SEMANTIC,),
            focus="Extract reusable factual/domain knowledge that is directly supported by the source.",
            instructions=(
                "Do not convert process steps, decisions, or examples into generic facts unless they are reusable knowledge.",),
        )


class LLMWorkflowDistiller(LLMSpecializedDistiller):
    def __init__(self) -> None:
        super().__init__(
            name="llm_workflow_distiller",
            item_types=(PersonaDistillationItemType.WORKFLOW, PersonaDistillationItemType.PROCEDURE),
            memory_layers=(PersonaMemoryLayer.PROCEDURAL,),
            focus="Extract workflows, procedures, triggers, owners, inputs, outputs, and failure paths.",
            instructions=("Keep process order and decision points source-backed.",),
        )


class LLMDecisionDistiller(LLMSpecializedDistiller):
    def __init__(self) -> None:
        super().__init__(
            name="llm_decision_distiller",
            item_types=(PersonaDistillationItemType.DECISION_PATTERN,),
            memory_layers=(PersonaMemoryLayer.PROCEDURAL,),
            focus="Extract source-backed rules, thresholds, escalation criteria, and decision patterns.",
            instructions=("Represent condition/action logic clearly without inventing thresholds.",),
        )


class LLMWritingStyleDistiller(LLMSpecializedDistiller):
    def __init__(self) -> None:
        super().__init__(
            name="llm_writing_style_distiller",
            item_types=(PersonaDistillationItemType.WRITING_STYLE,),
            memory_layers=(PersonaMemoryLayer.PERSONA,),
            focus="Extract writing style preferences only when source evidence explicitly describes style or tone.",
            instructions=("Do not infer personality or private identity from incidental wording.",),
        )


class LLMToolUsageDistiller(LLMSpecializedDistiller):
    def __init__(self) -> None:
        super().__init__(
            name="llm_tool_usage_distiller",
            item_types=(PersonaDistillationItemType.TOOL_USAGE,),
            memory_layers=(PersonaMemoryLayer.TOOL,),
            focus="Extract tool usage patterns as review candidates, not permissions or grants.",
            instructions=("Never grant tool access; output only source-backed observed or recommended tool usage.",),
        )


class LLMGuardrailDistiller(LLMSpecializedDistiller):
    def __init__(self) -> None:
        super().__init__(
            name="llm_guardrail_distiller",
            item_types=(PersonaDistillationItemType.GUARDRAIL,),
            memory_layers=(PersonaMemoryLayer.PROCEDURAL, PersonaMemoryLayer.PERSONA),
            focus="Extract safety, privacy, compliance, refusal, and escalation guardrails.",
            instructions=("Keep guardrails actionable and tied to source evidence.",),
        )


class LLMExampleDistiller(LLMSpecializedDistiller):
    def __init__(self) -> None:
        super().__init__(
            name="llm_example_distiller",
            item_types=(PersonaDistillationItemType.EXAMPLE,),
            memory_layers=(PersonaMemoryLayer.EPISODIC,),
            focus="Extract reusable examples, templates, sample responses, and representative cases.",
            instructions=("Preserve the example as an example rather than turning it into a policy.",),
        )


class LLMSocialContextDistiller(LLMSpecializedDistiller):
    def __init__(self) -> None:
        super().__init__(
            name="llm_social_context_distiller",
            item_types=(PersonaDistillationItemType.SOCIAL_CONTEXT,),
            memory_layers=(PersonaMemoryLayer.SOCIAL,),
            focus="Extract explicit social or collaboration context only when source evidence supports it.",
            instructions=("Avoid private relationship claims unless the exact evidence supports the candidate.",),
        )


class LLMBroadDistiller(LLMSpecializedDistiller):
    def __init__(self) -> None:
        super().__init__(
            name="llm_broad_distiller",
            item_types=tuple(
                item for item in PersonaDistillationItemType if item != PersonaDistillationItemType.SOURCE_REFERENCE),
            memory_layers=tuple(PersonaMemoryLayer),
            focus=(
                "Broad extraction for low-confidence or multi-target classification; choose the strongest "
                "source-backed item types."
            ),
            instructions=(
                "Prefer fewer, higher-confidence candidates, but preserve distinct source-backed item types.",),
        )


@dataclass(slots=True)
class LLMDistillationEngine:
    prompt_version: str = LLM_DISTILLATION_PROMPT_VERSION
    distiller_version: str = LLM_DISTILLER_VERSION
    max_candidates_per_source: int = LLM_MAX_CANDIDATES_PER_SOURCE
    distillers: tuple[LLMSpecializedDistiller, ...] = (
        LLMDecisionDistiller(),
        LLMWorkflowDistiller(),
        LLMKnowledgeDistiller(),
        LLMWritingStyleDistiller(),
        LLMToolUsageDistiller(),
        LLMGuardrailDistiller(),
        LLMExampleDistiller(),
        LLMSocialContextDistiller(),
    )
    broad_distiller: LLMSpecializedDistiller = LLMBroadDistiller()

    async def extract_source(
            self,
            *,
            memory: MemoryRecord,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
            model_profile: ModelProfileDefinition,
            generate_structured: StructuredGenerator,
    ) -> list[PersonaDistillationCandidate]:
        distiller = self.select_distiller(classification)
        payload = await generate_structured(
            profile=model_profile,
            schema_name=LLM_DISTILLATION_SCHEMA_NAME,
            schema=PersonaLLMDistillationBatchPayload.model_json_schema(),
            system=self.system_prompt(distiller),
            prompt=self.source_prompt(
                memory=memory,
                source_ref=source_ref,
                classification=classification,
                distiller=distiller,
            ),
        )
        try:
            batch = PersonaLLMDistillationBatchPayload.model_validate(payload)
        except ValidationError as exc:
            raise PersonaLLMDistillationError(
                f"LLM distillation output failed schema validation: {exc}"
            ) from exc

        source_classification = classification.as_payload()
        candidates: list[PersonaDistillationCandidate] = []
        for candidate in batch.candidates[:self.max_candidates_per_source]:
            evidence_grounding = candidate.ground_source_evidence(memory.content)
            distiller.prepare_candidate(
                candidate=candidate,
                source_content=memory.content,
                evidence_grounding=evidence_grounding,
            )
            candidates.append(
                candidate.to_distillation_candidate(
                    source_ref=source_ref,
                    evidence_grounding=evidence_grounding,
                    source_classification=source_classification,
                    source_memory_id=memory.id,
                    model_provider=model_profile.provider,
                    model_name=model_profile.model,
                    model_profile_id=model_profile.id,
                    prompt_version=self.prompt_version,
                    distiller_name=self._candidate_distiller_name(candidate, distiller),
                    distiller_version=self.distiller_version,
                )
            )
        return candidates

    def select_distiller(self, classification: PersonaSourceClassification) -> LLMSpecializedDistiller:
        if classification.confidence < LLM_LOW_CONFIDENCE_BROAD_EXTRACTION_THRESHOLD:
            return self.broad_distiller

        target_distillers = [
            distiller
            for target in classification.extraction_targets
            if (distiller := self._distiller_for_item_type(target)) is not None
        ]
        unique_target_distillers = list(dict.fromkeys(target_distillers))
        if len(unique_target_distillers) > 1:
            return self.broad_distiller

        for target in classification.extraction_targets:
            distiller = self._distiller_for_item_type(target)
            if distiller is not None:
                return distiller

        label_targets = {
            "decision": PersonaDistillationItemType.DECISION_PATTERN,
            "workflow": PersonaDistillationItemType.WORKFLOW,
            "policy_sop": PersonaDistillationItemType.GUARDRAIL,
            "tool_usage": PersonaDistillationItemType.TOOL_USAGE,
            "example": PersonaDistillationItemType.EXAMPLE,
            "personal_writing_style": PersonaDistillationItemType.WRITING_STYLE,
            "conversation": PersonaDistillationItemType.SOCIAL_CONTEXT,
            "domain_knowledge": PersonaDistillationItemType.DOMAIN_KNOWLEDGE,
        }
        target = label_targets.get(classification.label)
        if target is not None:
            distiller = self._distiller_for_item_type(target.value)
            if distiller is not None:
                return distiller
        return self.broad_distiller

    def _distiller_for_item_type(self, item_type: str) -> LLMSpecializedDistiller | None:
        for distiller in self.distillers:
            if any(candidate_type.value == item_type for candidate_type in distiller.item_types):
                return distiller
        return None

    @staticmethod
    def system_prompt(distiller: LLMSpecializedDistiller) -> str:
        return (
            "You distill one Agency Persona Factory source chunk into reviewable persona memory candidates. "
            "Return only schema-valid JSON. Every candidate must be grounded in source_evidence from the source. "
            "Do not create tool grants, private identity claims, or unsupported facts. If evidence is weak, set "
            "review_reasons so a human can inspect it before persona publishing. "
            f"Current specialized distiller: {distiller.name}. Focus: {distiller.focus}"
        )

    def source_prompt(
            self,
            *,
            memory: MemoryRecord,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
            distiller: LLMSpecializedDistiller | None = None,
    ) -> str:
        selected = distiller or self.select_distiller(classification)
        return json.dumps(
            selected.source_prompt_payload(
                engine=self,
                memory=memory,
                source_ref=source_ref,
                classification=classification,
            ),
            ensure_ascii=True,
        )

    def _candidate_distiller_name(
            self,
            candidate: PersonaLLMDistillationCandidatePayload,
            selected_distiller: LLMSpecializedDistiller,
    ) -> str:
        if selected_distiller is not self.broad_distiller:
            return selected_distiller.name
        distiller = self._distiller_for_item_type(candidate.item_type.value)
        return distiller.name if distiller is not None else selected_distiller.name


__all__ = [
    "LLM_DISTILLATION_PROMPT_VERSION",
    "LLM_DISTILLATION_CANDIDATE_SCHEMA_VERSION",
    "LLM_DISTILLATION_EXTRACTOR",
    "LLM_DISTILLATION_SCHEMA_NAME",
    "LLM_DISTILLER_VERSION",
    "LLM_MAX_CANDIDATES_PER_SOURCE",
    "LLM_MAX_CONTENT_CHARS",
    "LLM_MAX_EVIDENCE_CHARS",
    "LLM_MAX_GRAPH_HINTS",
    "LLM_MAX_REVIEW_REASONS",
    "LLM_MAX_STRUCTURED_PAYLOAD_BYTES",
    "LLM_MAX_TITLE_CHARS",
    "LLMBroadDistiller",
    "LLMDistillationEngine",
    "LLMDecisionDistiller",
    "LLMExampleDistiller",
    "LLMGuardrailDistiller",
    "LLMKnowledgeDistiller",
    "LLMSocialContextDistiller",
    "LLMSpecializedDistiller",
    "LLMToolUsageDistiller",
    "LLMWorkflowDistiller",
    "LLMWritingStyleDistiller",
    "PersonaLLMDistillationBatchPayload",
    "PersonaLLMDistillationCandidatePayload",
    "PersonaLLMDistillationError",
    "PersonaLLMGraphEntityHint",
    "PersonaLLMGraphRelationshipHint",
    "PersonaLLMSourceSpan",
]
