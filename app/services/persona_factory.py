"""Review-first Persona Factory distillation and publishing service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Any

from app.api.context import ApiContext
from app.core.config import get_settings
from app.core.time import utc_now
from app.domain import (
    GraphProjectionEvent,
    MemoryRecord,
    MemoryScope,
    MemoryType,
    ModelProfileDefinition,
    PersonaDefinition,
    PersonaDistillationItem,
    PersonaDistillationItemReviewStatus,
    PersonaDistillationItemType,
    PersonaDistillationMode,
    PersonaDistillationRun,
    PersonaDistillationStatus,
    PersonaLLMModelSource,
    PersonaMemoryLayer,
    PersonaSource,
    PersonaSourceType,
    PersonaStatus,
    PersonaVersion,
    PersonaVersionStatus,
    UserDefinition,
)
from app.llm.base import ModelMessage
from app.services.memory import MemoryService
from app.services.persona_distillation_pipeline import (
    DISTILLER_VERSION,
    PersonaDistillationCandidate,
    PersonaDistillationPipeline,
    PersonaSourceClassification,
    SOURCE_CLASSIFICATIONS,
)
from app.services.persona_llm_distillation import (
    LLM_DISTILLATION_EXTRACTOR,
    LLM_DISTILLER_VERSION,
    LLMDistillationEngine,
    PersonaLLMDistillationError,
)
from app.services.personas import PersonaNotFoundError, PersonaService
from app.services.source_intelligence import (
    SOURCE_INTELLIGENCE_DOCUMENT_KINDS,
    SourceIntelligenceError,
    SourceIntelligenceService,
)


class PersonaDistillationError(ValueError):
    pass


class PersonaPublishError(ValueError):
    pass


MEMORY_LAYER_KEYWORDS = {
    "episodic": ("incident", "audit", "lesson", "previous", "retrospective", "postmortem", "experience"),
    "procedural": ("process", "procedure", "workflow", "step", "sop", "lifecycle", "checklist", "how to"),
    "social": ("stakeholder", "reviewer", "approver", "owner", "contact", "escalation", "committee"),
}
DEFAULT_GOVERNANCE_LABELS = {
    "persona_type": "professional",
    "capability_mode": "persona_plus_expertise",
    "consent_status": "unspecified",
    "source_basis": "memory_records",
    "sensitivity_level": "standard",
    "visibility": "private",
    "representation_policy": "simulated_persona",
}
LLM_BACKED_DISTILLATION_MODES = {PersonaDistillationMode.LLM, PersonaDistillationMode.HYBRID}
HYBRID_DISTILLATION_EXTRACTOR = "hybrid-distillation-v1"
HYBRID_MERGE_STRATEGY = "hybrid-exact-semantic-conflict-merge-v1"
DETERMINISTIC_EXTRACTION_PIPELINE_VERSION = "classify-extract-normalize-validate-v1"
HYBRID_SEMANTIC_DUPLICATE_THRESHOLD = 0.84
HYBRID_TOKEN_DUPLICATE_THRESHOLD = 0.62
HYBRID_TOPIC_TOKEN_OVERLAP_THRESHOLD = 0.45
PERSONA_PACKAGE_POLISH_SCHEMA_NAME = "persona_package_polish"
PERSONA_PACKAGE_POLISH_PROMPT_VERSION = "persona-package-polish-v1"
PACKAGE_POLISH_SUPPORTED_SECTIONS = (
    "knowledge",
    "decision_patterns",
    "workflows",
    "tools",
    "guardrails",
    "examples",
    "memory_layers",
)
PACKAGE_POLISH_ALLOWED_GENERIC_TERMS = {
    "persona",
    "package",
    "reusable",
    "source",
    "grounded",
    "concise",
    "practical",
    "professional",
    "expertise",
    "workflow",
    "knowledge",
    "decision",
    "decisions",
    "support",
    "supports",
    "based",
    "approved",
    "reviewed",
    "communication",
    "escalation",
    "response",
    "style",
    "preferences",
    "summary",
}
PERSONA_REVIEW_EXTRACTION_SOURCES = {"deterministic", "llm", "hybrid"}
PERSONA_REVIEW_ACTIONS = {
    "prefer_llm",
    "prefer_deterministic",
    "merge_manually",
    "mark_evidence_insufficient",
}


@dataclass(slots=True)
class _DistillationModelSelection:
    distillation_mode: PersonaDistillationMode
    llm_model_source: PersonaLLMModelSource | None
    model_profile_id: str | None
    llm_model_provider: str | None
    llm_model: str | None
    resolved_model_provider: str | None
    resolved_model: str | None
    resolved_model_profile_id: str | None
    inline_model_profile: ModelProfileDefinition | None = None


class HybridDistillationMerger:
    """Merge deterministic and LLM candidates while preserving review provenance."""

    def merge(
            self,
            *,
            deterministic_candidates: list[PersonaDistillationCandidate],
            llm_candidates: list[PersonaDistillationCandidate],
    ) -> tuple[list[PersonaDistillationCandidate], dict[str, Any]]:
        candidates = [
                         self._normalize_candidate(candidate, generated_by="deterministic")
                         for candidate in deterministic_candidates
                     ] + [
                         self._normalize_candidate(candidate, generated_by="llm")
                         for candidate in llm_candidates
                     ]
        if not candidates:
            return [], {
                "strategy": "hybrid-merge-v1",
                "input_count": 0,
                "output_count": 0,
            }

        conflict_groups = self._conflict_groups(candidates)
        conflicted_ids = {
            self._candidate_id(candidate)
            for group in conflict_groups
            for candidate in group
        }
        candidates = self._stamp_conflicts(candidates, conflict_groups)

        active_for_merge = [
            candidate for candidate in candidates
            if self._candidate_id(candidate) not in conflicted_ids
        ]
        merged: list[PersonaDistillationCandidate] = []
        exact_groups = self._exact_duplicate_groups(active_for_merge)
        exact_grouped_ids = {
            self._candidate_id(candidate)
            for group in exact_groups
            for candidate in group
        }
        for group in exact_groups:
            merged.append(self._merge_group(group, strategy="exact_duplicate"))

        remaining = [
            candidate for candidate in active_for_merge
            if self._candidate_id(candidate) not in exact_grouped_ids
        ]
        semantic_groups = self._semantic_duplicate_groups(remaining)
        semantic_grouped_ids = {
            self._candidate_id(candidate)
            for group in semantic_groups
            for candidate in group
        }
        for group in semantic_groups:
            merged.append(self._merge_group(group, strategy="semantic_duplicate"))

        subsumed_groups = self._source_subsumed_groups([
            candidate for candidate in remaining
            if self._candidate_id(candidate) not in semantic_grouped_ids
        ])
        subsumed_grouped_ids = {
            self._candidate_id(candidate)
            for group in subsumed_groups
            for candidate in group
        }
        for group in subsumed_groups:
            merged.append(self._merge_group(group, strategy="source_subsumed"))

        singles = [
                      self._stamp_single(candidate)
                      for candidate in candidates
                      if self._candidate_id(candidate) in conflicted_ids
                  ] + [
                      self._stamp_single(candidate)
                      for candidate in remaining
                      if self._candidate_id(candidate) not in semantic_grouped_ids
                         and self._candidate_id(candidate) not in subsumed_grouped_ids
                  ]
        output = [*merged, *singles]
        summary = {
            "strategy": "hybrid-merge-v1",
            "input_count": len(candidates),
            "deterministic_input_count": len(deterministic_candidates),
            "llm_input_count": len(llm_candidates),
            "output_count": len(output),
            "exact_duplicate_group_count": len(exact_groups),
            "exact_duplicate_merged_count": sum(len(group) - 1 for group in exact_groups),
            "semantic_duplicate_group_count": len(semantic_groups),
            "semantic_duplicate_merged_count": sum(len(group) - 1 for group in semantic_groups),
            "source_subsumed_group_count": len(subsumed_groups),
            "source_subsumed_merged_count": sum(len(group) - 1 for group in subsumed_groups),
            "conflict_group_count": len(conflict_groups),
            "both_agreed_count": sum(1 for candidate in output if "both_agreed" in self._review_flags(candidate)),
            "llm_only_count": sum(1 for candidate in output if "llm_only" in self._review_flags(candidate)),
            "deterministic_only_count": sum(
                1 for candidate in output if "deterministic_only" in self._review_flags(candidate)),
        }
        return output, summary

    def _normalize_candidate(
            self,
            candidate: PersonaDistillationCandidate,
            *,
            generated_by: str,
    ) -> PersonaDistillationCandidate:
        payload = dict(candidate.structured_payload or {})
        metadata = dict(candidate.metadata or {})
        source_refs = self._source_refs(payload)
        if source_refs:
            payload["source_refs"] = source_refs
            payload["source_ref"] = source_refs[0]
        flags = list(dict.fromkeys([
            *self._string_list(payload.get("review_flags")),
            *self._string_list(metadata.get("review_reasons")),
        ]))
        metadata.update(
            {
                "generated_by": metadata.get("generated_by") or generated_by,
                "merge_strategy": metadata.get("merge_strategy") or "candidate_normalized",
                "merged_from_item_ids": metadata.get("merged_from_item_ids") or [],
                "merged_from_candidate_ids": metadata.get("merged_from_candidate_ids")
                                             or [self._candidate_id(candidate)],
                "merged_from_distillers": metadata.get("merged_from_distillers")
                                          or [self._distiller_name(candidate)],
                "review_flags": flags,
            }
        )
        payload.update(
            {
                "generated_by": payload.get("generated_by") or generated_by,
                "review_flags": flags,
                "hybrid_merge": {
                    "candidate_id": self._candidate_id(candidate),
                    "generated_by": generated_by,
                    "merge_strategy": metadata["merge_strategy"],
                },
            }
        )
        candidate.structured_payload = payload
        candidate.metadata = metadata
        return candidate

    def _stamp_single(self, candidate: PersonaDistillationCandidate) -> PersonaDistillationCandidate:
        generated_by = self._generated_by(candidate)
        flag = "llm_only" if generated_by == "llm" else "deterministic_only"
        flags = list(dict.fromkeys([*self._review_flags(candidate), flag]))
        payload = dict(candidate.structured_payload or {})
        metadata = dict(candidate.metadata or {})
        payload["review_flags"] = flags
        payload["hybrid_merge"] = {
            **(payload.get("hybrid_merge") if isinstance(payload.get("hybrid_merge"), dict) else {}),
            "merge_strategy": metadata.get("merge_strategy") or "single",
            "review_flags": flags,
        }
        metadata.update(
            {
                "generated_by": generated_by,
                "merge_strategy": metadata.get("merge_strategy") or "single",
                "review_flags": flags,
            }
        )
        candidate.structured_payload = payload
        candidate.metadata = metadata
        return candidate

    def _merge_group(
            self,
            group: list[PersonaDistillationCandidate],
            *,
            strategy: str,
    ) -> PersonaDistillationCandidate:
        primary = (
            self._source_subsumed_primary(group)
            if strategy == "source_subsumed"
            else sorted(group, key=self._preference_key)[0]
        )
        source_refs = self._merged_source_refs(group)
        generated_modes = sorted({self._generated_by(candidate) for candidate in group})
        flags = list(dict.fromkeys([
            *[
                flag
                for candidate in group
                for flag in self._review_flags(candidate)
            ],
            "both_agreed" if {"deterministic", "llm"}.issubset(set(generated_modes)) else f"{generated_modes[0]}_only",
        ]))
        group_id = self._group_id(strategy, group)
        payload = dict(primary.structured_payload or {})
        if source_refs:
            payload["source_refs"] = source_refs
            payload["source_ref"] = self._preferred_source_ref(source_refs)
        payload.update(
            {
                "extractor": HYBRID_DISTILLATION_EXTRACTOR,
                "generated_by": "hybrid" if len(generated_modes) > 1 else generated_modes[0],
                "review_flags": flags,
                "hybrid_merge": {
                    "strategy": "hybrid-merge-v1",
                    "merge_strategy": strategy,
                    "group_id": group_id,
                    "generated_by": generated_modes,
                    "merged_from_candidate_ids": [self._candidate_id(candidate) for candidate in group],
                    "merged_from_distillers": self._merged_distillers(group),
                    "source_ref_count": len(source_refs),
                },
            }
        )
        if strategy == "semantic_duplicate":
            payload["semantic_duplicate_group_id"] = group_id

        metadata = {
            **(primary.metadata or {}),
            "generated_by": payload["generated_by"],
            "merged_from_item_ids": [],
            "merged_from_candidate_ids": [self._candidate_id(candidate) for candidate in group],
            "merged_from_distillers": self._merged_distillers(group),
            "semantic_duplicate_group_id": group_id if strategy == "semantic_duplicate" else None,
            "conflict_group_id": None,
            "merge_strategy": strategy,
            "review_flags": flags,
            "source_generation_modes": generated_modes,
        }
        return primary.__class__(
            item_type=primary.item_type,
            memory_layer=primary.memory_layer,
            title=primary.title,
            content=primary.content,
            confidence=max(candidate.confidence for candidate in group),
            structured_payload=payload,
            metadata=metadata,
            needs_review=any(candidate.needs_review for candidate in group),
            distiller_name="hybrid_merger",
        )

    def _stamp_conflicts(
            self,
            candidates: list[PersonaDistillationCandidate],
            conflict_groups: list[list[PersonaDistillationCandidate]],
    ) -> list[PersonaDistillationCandidate]:
        by_id: dict[str, str] = {}
        conflicting_ids: dict[str, list[str]] = {}
        for group in conflict_groups:
            group_id = self._group_id("material_conflict", group)
            ids = [self._candidate_id(candidate) for candidate in group]
            for candidate_id in ids:
                by_id[candidate_id] = group_id
                conflicting_ids[candidate_id] = [item_id for item_id in ids if item_id != candidate_id]

        for candidate in candidates:
            candidate_id = self._candidate_id(candidate)
            group_id = by_id.get(candidate_id)
            if group_id is None:
                continue
            flags = list(dict.fromkeys([*self._review_flags(candidate), "material_conflict"]))
            payload = dict(candidate.structured_payload or {})
            payload["review_flags"] = flags
            payload["conflict_group_id"] = group_id
            payload["hybrid_merge"] = {
                **(payload.get("hybrid_merge") if isinstance(payload.get("hybrid_merge"), dict) else {}),
                "merge_strategy": "conflict_review",
                "conflict_group_id": group_id,
                "conflicting_candidate_ids": conflicting_ids[candidate_id],
            }
            metadata = {
                **(candidate.metadata or {}),
                "conflict": True,
                "conflict_group_id": group_id,
                "conflicting_candidate_ids": conflicting_ids[candidate_id],
                "merge_strategy": "conflict_review",
                "review_flags": flags,
            }
            candidate.needs_review = True
            candidate.structured_payload = payload
            candidate.metadata = metadata
        return candidates

    def _exact_duplicate_groups(
            self,
            candidates: list[PersonaDistillationCandidate],
    ) -> list[list[PersonaDistillationCandidate]]:
        grouped: dict[tuple[str, str, str], list[PersonaDistillationCandidate]] = {}
        for candidate in candidates:
            grouped.setdefault(
                (
                    candidate.item_type.value,
                    candidate.memory_layer.value,
                    self._normalized_text(candidate.content),
                ),
                [],
            ).append(candidate)
        return [group for group in grouped.values() if len(group) > 1]

    def _semantic_duplicate_groups(
            self,
            candidates: list[PersonaDistillationCandidate],
    ) -> list[list[PersonaDistillationCandidate]]:
        groups: list[list[PersonaDistillationCandidate]] = []
        used: set[str] = set()
        for candidate in candidates:
            candidate_id = self._candidate_id(candidate)
            if candidate_id in used:
                continue
            group = [candidate]
            for other in candidates:
                other_id = self._candidate_id(other)
                if other_id == candidate_id or other_id in used:
                    continue
                if self._are_semantic_duplicates(candidate, other):
                    group.append(other)
            if len(group) > 1:
                used.update(self._candidate_id(item) for item in group)
                groups.append(group)
        return groups

    def _source_subsumed_groups(
            self,
            candidates: list[PersonaDistillationCandidate],
    ) -> list[list[PersonaDistillationCandidate]]:
        groups: list[list[PersonaDistillationCandidate]] = []
        used: set[str] = set()
        deterministic = [
            candidate for candidate in candidates
            if self._generated_by(candidate) == "deterministic"
        ]
        llm = [
            candidate for candidate in candidates
            if self._generated_by(candidate) == "llm"
        ]
        for primary in deterministic:
            primary_id = self._candidate_id(primary)
            if primary_id in used:
                continue
            group = [primary]
            for candidate in llm:
                candidate_id = self._candidate_id(candidate)
                if candidate_id in used:
                    continue
                if self._source_subsumes(primary, candidate):
                    group.append(candidate)
            if len(group) > 1:
                used.update(self._candidate_id(candidate) for candidate in group)
                groups.append(group)
        return groups

    def _conflict_groups(
            self,
            candidates: list[PersonaDistillationCandidate],
    ) -> list[list[PersonaDistillationCandidate]]:
        groups: list[list[PersonaDistillationCandidate]] = []
        used_pairs: set[tuple[str, str]] = set()
        for index, candidate in enumerate(candidates):
            for other in candidates[index + 1:]:
                pair = tuple(sorted((self._candidate_id(candidate), self._candidate_id(other))))
                if pair in used_pairs:
                    continue
                used_pairs.add(pair)
                if not self._same_review_surface(candidate, other):
                    continue
                if not self._topic_overlap(candidate, other):
                    continue
                if self._claim_polarity(candidate.content) == self._claim_polarity(other.content):
                    continue
                if "neutral" in {self._claim_polarity(candidate.content), self._claim_polarity(other.content)}:
                    continue
                groups.append([candidate, other])
        return groups

    def _are_semantic_duplicates(
            self,
            left: PersonaDistillationCandidate,
            right: PersonaDistillationCandidate,
    ) -> bool:
        if not self._same_review_surface(left, right):
            return False
        if self._claim_polarity(left.content) != self._claim_polarity(right.content):
            return False
        left_text = self._normalized_text(f"{left.title} {left.content}")
        right_text = self._normalized_text(f"{right.title} {right.content}")
        if not left_text or not right_text:
            return False
        sequence_score = SequenceMatcher(None, left_text, right_text).ratio()
        token_overlap = self._token_overlap(left_text, right_text)
        if (
                self._same_source_memory(left, right)
                and left.item_type in {PersonaDistillationItemType.TOOL_USAGE, PersonaDistillationItemType.GUARDRAIL}
                and token_overlap >= 0.20
        ):
            return True
        return (
                sequence_score >= HYBRID_SEMANTIC_DUPLICATE_THRESHOLD
                or token_overlap >= HYBRID_TOKEN_DUPLICATE_THRESHOLD
        )

    @staticmethod
    def _same_review_surface(left: PersonaDistillationCandidate, right: PersonaDistillationCandidate) -> bool:
        return left.item_type == right.item_type and left.memory_layer == right.memory_layer

    def _topic_overlap(self, left: PersonaDistillationCandidate, right: PersonaDistillationCandidate) -> bool:
        return (
                self._token_overlap(
                    self._topic_text(left),
                    self._topic_text(right),
                ) >= HYBRID_TOPIC_TOKEN_OVERLAP_THRESHOLD
        )

    def _preference_key(self, candidate: PersonaDistillationCandidate) -> tuple[int, int, float, str]:
        return (
            0 if self._source_backed(candidate) else 1,
            0 if self._approved_source(candidate) else 1,
            -candidate.confidence,
            self._generated_by(candidate),
        )

    def _source_subsumed_primary(self, group: list[PersonaDistillationCandidate]) -> PersonaDistillationCandidate:
        deterministic = [
            candidate for candidate in group
            if self._generated_by(candidate) == "deterministic"
        ]
        if deterministic:
            return sorted(
                deterministic,
                key=lambda candidate: (
                    -len(self._normalized_text(candidate.content)),
                    self._preference_key(candidate),
                ),
            )[0]
        return sorted(group, key=self._preference_key)[0]

    @staticmethod
    def _source_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
        refs = payload.get("source_refs")
        if isinstance(refs, list):
            source_refs = [ref for ref in refs if isinstance(ref, dict)]
            if source_refs:
                return source_refs
        ref = payload.get("source_ref")
        return [ref] if isinstance(ref, dict) else []

    def _merged_source_refs(self, group: list[PersonaDistillationCandidate]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any, Any, Any]] = set()
        for candidate in group:
            for ref in self._source_refs(candidate.structured_payload or {}):
                evidence = ref.get("evidence") if isinstance(ref.get("evidence"), dict) else {}
                key = (
                    ref.get("source_id"),
                    ref.get("memory_id"),
                    ref.get("chunk_index"),
                    evidence.get("hash") if isinstance(evidence, dict) else None,
                )
                if key in seen:
                    continue
                seen.add(key)
                refs.append(ref)
        return refs

    @staticmethod
    def _preferred_source_ref(refs: list[dict[str, Any]]) -> dict[str, Any]:
        for ref in refs:
            evidence = ref.get("evidence") if isinstance(ref.get("evidence"), dict) else {}
            if evidence.get("verified") is True:
                return ref
        for ref in refs:
            if isinstance(ref.get("evidence"), dict):
                return ref
        return refs[0]

    def _merged_distillers(self, group: list[PersonaDistillationCandidate]) -> list[str]:
        return list(dict.fromkeys(self._distiller_name(candidate) for candidate in group))

    @staticmethod
    def _distiller_name(candidate: PersonaDistillationCandidate) -> str:
        metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
        payload = candidate.structured_payload if isinstance(candidate.structured_payload, dict) else {}
        return str(metadata.get("distiller") or payload.get("distiller") or candidate.distiller_name or "unknown")

    @staticmethod
    def _generated_by(candidate: PersonaDistillationCandidate) -> str:
        metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
        payload = candidate.structured_payload if isinstance(candidate.structured_payload, dict) else {}
        generated_by = str(metadata.get("generated_by") or payload.get("generated_by") or "").strip()
        if generated_by in {"llm", "deterministic", "hybrid"}:
            return generated_by
        extractor = str(payload.get("extractor") or "")
        return "llm" if extractor.startswith("llm") else "deterministic"

    def _candidate_id(self, candidate: PersonaDistillationCandidate) -> str:
        payload = candidate.structured_payload if isinstance(candidate.structured_payload, dict) else {}
        metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
        evidence = payload.get("source_evidence") or metadata.get("source_evidence_hash")
        digest_payload = {
            "item_type": candidate.item_type.value,
            "memory_layer": candidate.memory_layer.value,
            "title": self._normalized_text(candidate.title),
            "content": self._normalized_text(candidate.content),
            "generated_by": self._generated_by(candidate),
            "distiller": self._distiller_name(candidate),
            "evidence": evidence,
        }
        digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True).encode("utf-8")).hexdigest()
        return f"candidate-{digest[:16]}"

    def _group_id(self, prefix: str, group: list[PersonaDistillationCandidate]) -> str:
        digest = hashlib.sha256(
            json.dumps(sorted(self._candidate_id(candidate) for candidate in group)).encode("utf-8")
        ).hexdigest()
        return f"{prefix}-{digest[:12]}"

    def _source_backed(self, candidate: PersonaDistillationCandidate) -> bool:
        metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
        grounding = metadata.get("evidence_grounding")
        if isinstance(grounding, dict):
            return bool(grounding.get("verified"))
        return bool(self._source_refs(candidate.structured_payload or {}))

    def _approved_source(self, candidate: PersonaDistillationCandidate) -> bool:
        for ref in self._source_refs(candidate.structured_payload or {}):
            if ref.get("source_intelligence_review_status") == "approved":
                return True
        return False

    def _source_subsumes(
            self,
            primary: PersonaDistillationCandidate,
            candidate: PersonaDistillationCandidate,
    ) -> bool:
        if not self._same_review_surface(primary, candidate):
            return False
        if self._generated_by(primary) != "deterministic" or self._generated_by(candidate) != "llm":
            return False
        if not self._same_source_memory(primary, candidate):
            return False
        evidence = self._candidate_evidence_text(candidate)
        if not evidence:
            return False
        # Broad deterministic style/procedure items often cover several source-backed LLM micro-candidates.
        # Fold only when the LLM evidence is literally represented in the broader candidate text.
        primary_text = self._normalized_text(f"{primary.title} {primary.content}")
        return self._normalized_text(evidence) in primary_text

    def _same_source_memory(
            self,
            left: PersonaDistillationCandidate,
            right: PersonaDistillationCandidate,
    ) -> bool:
        left_ids = {
            str(ref.get("memory_id"))
            for ref in self._source_refs(left.structured_payload or {})
            if ref.get("memory_id")
        }
        right_ids = {
            str(ref.get("memory_id"))
            for ref in self._source_refs(right.structured_payload or {})
            if ref.get("memory_id")
        }
        return bool(left_ids.intersection(right_ids))

    @staticmethod
    def _candidate_evidence_text(candidate: PersonaDistillationCandidate) -> str:
        payload = candidate.structured_payload if isinstance(candidate.structured_payload, dict) else {}
        evidence = payload.get("source_evidence")
        if isinstance(evidence, str) and evidence.strip():
            return evidence.strip()
        source_ref = payload.get("source_ref") if isinstance(payload.get("source_ref"), dict) else {}
        source_evidence = source_ref.get("evidence") if isinstance(source_ref.get("evidence"), dict) else {}
        text = source_evidence.get("text") if isinstance(source_evidence, dict) else None
        return text.strip() if isinstance(text, str) else ""

    def _review_flags(self, candidate: PersonaDistillationCandidate) -> list[str]:
        metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
        payload = candidate.structured_payload if isinstance(candidate.structured_payload, dict) else {}
        return list(dict.fromkeys([
            *self._string_list(payload.get("review_flags")),
            *self._string_list(metadata.get("review_flags")),
            *self._string_list(metadata.get("review_reasons")),
        ]))

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    @staticmethod
    def _normalized_text(value: str) -> str:
        lowered = str(value or "").lower()
        lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
        return " ".join(lowered.split())

    def _topic_text(self, candidate: PersonaDistillationCandidate) -> str:
        words = [
            word
            for word in self._normalized_text(f"{candidate.title} {candidate.content}").split()
            if word not in {
                "must", "should", "shall", "not", "never", "cannot", "can", "do",
                "does", "when", "then", "rule", "decision", "pattern",
            }
        ]
        return " ".join(words)

    @staticmethod
    def _token_overlap(left: str, right: str) -> float:
        left_tokens = HybridDistillationMerger._tokens(left)
        right_tokens = HybridDistillationMerger._tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens.intersection(right_tokens)) / len(left_tokens.union(right_tokens))

    @staticmethod
    def _tokens(value: str) -> set[str]:
        stop_words = {
            "from", "when", "then", "with", "that", "this", "into", "onto", "rule", "decision",
            "pattern", "accounts",
        }
        tokens: set[str] = set()
        for token in value.split():
            if len(token) <= 3 or token in stop_words:
                continue
            if token.endswith("ies") and len(token) > 5:
                token = f"{token[:-3]}y"
            elif token.endswith("s") and len(token) > 4:
                token = token[:-1]
            tokens.add(token)
        return tokens

    @staticmethod
    def _claim_polarity(content: str) -> str:
        lowered = content.lower()
        if any(token in lowered for token in ("must not", "should not", "do not", "cannot", "avoid ", "never ")):
            return "negative"
        if any(token in lowered for token in ("must ", "should ", "can ", "rely ", "approve ", "allow ", "include ")):
            return "positive"
        return "neutral"


GOVERNANCE_ALLOWED_VALUES = {
    "persona_type": {"professional", "personal", "public_figure", "fictional", "self"},
    "capability_mode": {"persona_only", "expertise_only", "persona_plus_expertise"},
    "consent_status": {
        "unspecified",
        "self",
        "explicit_consent",
        "organization_authorized",
        "public_material",
        "fictional",
        "unverified_private_person",
    },
    "source_basis": {
        "memory_records",
        "uploaded_private_material",
        "public_sources",
        "user_description",
        "chat_export",
        "mixed",
    },
    "sensitivity_level": {"standard", "sensitive", "intimate", "regulated"},
    "visibility": {"private", "workspace", "organization", "marketplace"},
    "representation_policy": {"simulated_persona"},
}


class _LLMNormalizationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    title: str | None = None
    content: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    needs_review: bool | None = None
    rationale: str | None = None


class _LLMNormalizationSupersede(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    superseded_by_item_id: str
    reason: str | None = None


class _LLMNormalizationConflictGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_ids: list[str]
    reason: str

    @model_validator(mode="after")
    def validate_group(self) -> "_LLMNormalizationConflictGroup":
        self.item_ids = list(dict.fromkeys(item.strip() for item in self.item_ids if item.strip()))
        if len(self.item_ids) < 2:
            raise ValueError("Conflict groups require at least two item_ids.")
        return self


class _LLMNormalizationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updates: list[_LLMNormalizationUpdate] = Field(default_factory=list)
    superseded: list[_LLMNormalizationSupersede] = Field(default_factory=list)
    conflict_groups: list[_LLMNormalizationConflictGroup] = Field(default_factory=list)
    summary: str | None = None


class _LLMPackagePolishPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package: dict[str, Any]
    summary: str | None = None


@dataclass(slots=True)
class PersonaFactoryService:
    context: ApiContext

    @staticmethod
    def governance_label_catalog() -> dict[str, Any]:
        return {
            "defaults": dict(DEFAULT_GOVERNANCE_LABELS),
            "allowed_values": {
                key: sorted(values)
                for key, values in sorted(GOVERNANCE_ALLOWED_VALUES.items())
            },
            "validation_rules": [
                "Self personas require self or explicit consent.",
                "Fictional personas require fictional consent.",
                "Personal personas require explicit, self, or unverified-private-person consent.",
                "Public-material consent requires public or mixed source material.",
                "Public-figure personas require public or mixed source material.",
                "Unverified private-person personas must remain private.",
                "Intimate personas cannot use organization or marketplace visibility.",
                "Marketplace personas must be standard sensitivity and cannot be based only on private memory records, uploaded private material, or chat exports.",
                "Marketplace public-figure personas require public-material consent and public-source basis.",
            ],
        }

    async def distill_from_memories(
            self,
            *,
            persona_id: str | None,
            name: str | None,
            description: str | None,
            source_memory_ids: list[str],
            current_user: UserDefinition,
            distillation_mode: str | None = None,
            llm_model_source: str | None = None,
            model_profile_id: str | None = None,
            llm_model_provider: str | None = None,
            llm_model: str | None = None,
            governance_labels: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not source_memory_ids:
            raise PersonaDistillationError("Select at least one source memory.")
        memories = await self._load_memories(source_memory_ids)
        model_selection = await self._resolve_distillation_model_selection(
            distillation_mode=distillation_mode,
            llm_model_source=llm_model_source,
            model_profile_id=model_profile_id,
            llm_model_provider=llm_model_provider,
            llm_model=llm_model,
        )
        self._validate_distillation_scope(memories, distillation_mode=model_selection.distillation_mode)
        persona = await self._resolve_or_create_persona(
            persona_id=persona_id,
            name=name,
            description=description,
            current_user=current_user,
        )
        sources = await self._ensure_memory_sources(persona=persona, memories=memories)
        run = await self.context.persona_distillation_run_repo.create(
            PersonaDistillationRun(
                persona_id=persona.id,
                status=PersonaDistillationStatus.RUNNING,
                distillation_mode=model_selection.distillation_mode,
                llm_model_source=model_selection.llm_model_source,
                model_profile_id=model_selection.model_profile_id,
                llm_model_provider=model_selection.llm_model_provider,
                llm_model=model_selection.llm_model,
                resolved_model_provider=model_selection.resolved_model_provider,
                resolved_model=model_selection.resolved_model,
                resolved_model_profile_id=model_selection.resolved_model_profile_id,
                input_source_ids=[item.id for item in sources],
                distillation_metrics=self._initial_distillation_metrics(model_selection),
            )
        )
        try:
            package = await self._draft_package(
                persona=persona,
                memories=memories,
                sources=sources,
                run_id=run.id,
                governance_labels=governance_labels,
            )
            items = await self._extract_distillation_items(
                persona=persona,
                run=run,
                memories=memories,
                sources=sources,
            )
            run = await self.context.persona_distillation_run_repo.save(
                run.model_copy(
                    update={
                        "status": PersonaDistillationStatus.NEEDS_REVIEW,
                        "output_package": package,
                        "completed_at": utc_now(),
                    }
                )
            )
            await self.context.persona_repo.update(persona.id, {"status": PersonaStatus.IN_REVIEW.value})
            await self._append_audit_event(
                "persona.factory.distilled",
                persona_id=persona.id,
                user_id=current_user.id,
                payload={
                    "run_id": run.id,
                    "source_memory_ids": source_memory_ids,
                    "source_ids": run.input_source_ids,
                    "item_count": len(items),
                    "governance": package.get("governance"),
                    "distillation_mode": run.distillation_mode.value,
                    "llm_model_source": run.llm_model_source.value if run.llm_model_source else None,
                    "resolved_model_provider": run.resolved_model_provider,
                    "resolved_model": run.resolved_model,
                    "resolved_model_profile_id": run.resolved_model_profile_id,
                },
            )
            persona_payload = (await self.context.persona_repo.get(persona.id)).model_dump(mode="json")
            return {
                "persona": persona_payload,
                "run": run.model_dump(mode="json"),
                "sources": [item.model_dump(mode="json") for item in sources],
                "items": [item.model_dump(mode="json") for item in items],
            }
        except Exception as exc:
            run = await self.context.persona_distillation_run_repo.save(
                run.model_copy(
                    update={
                        "status": PersonaDistillationStatus.FAILED,
                        "errors": [{"message": str(exc), "type": type(exc).__name__}],
                        "completed_at": utc_now(),
                    }
                )
            )
            await self._append_audit_event(
                "persona.factory.distillation.failed",
                persona_id=persona.id,
                user_id=current_user.id,
                payload={
                    "run_id": run.id,
                    "source_memory_ids": source_memory_ids,
                    "source_ids": run.input_source_ids,
                    "distillation_mode": run.distillation_mode.value,
                    "llm_model_source": run.llm_model_source.value if run.llm_model_source else None,
                    "resolved_model_provider": run.resolved_model_provider,
                    "resolved_model": run.resolved_model,
                    "resolved_model_profile_id": run.resolved_model_profile_id,
                    "error": {"message": str(exc), "type": type(exc).__name__},
                    "distillation_metrics": run.distillation_metrics,
                },
            )
            raise PersonaDistillationError(str(exc)) from exc

    async def get_run(self, run_id: str) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        persona = await self.context.persona_repo.get(run.persona_id)
        sources = await self.context.persona_source_repo.list_by_persona(run.persona_id)
        items = await self.context.persona_distillation_item_repo.list_by_run(run.id)
        review_summary = self._run_review_summary(run, items)
        return {
            "persona": persona.model_dump(mode="json") if persona is not None else None,
            "run": run.model_dump(mode="json"),
            "sources": [item.model_dump(mode="json") for item in sources if item.id in set(run.input_source_ids)],
            "items": [self._serialize_review_item(item) for item in items],
            "review_summary": review_summary,
        }

    async def list_runs(
            self,
            *,
            persona_id: str | None = None,
            status: str | None = None,
            created_by_user_id: str | None = None,
            workspace_id: str | None = None,
    ) -> dict[str, Any]:
        if persona_id:
            runs = await self.context.persona_distillation_run_repo.list_by_persona(persona_id)
        else:
            runs = await self.context.persona_distillation_run_repo.list(include_deleted=True)
        if status:
            normalized_status = status.strip().lower()
            allowed = {item.value for item in PersonaDistillationStatus}
            if normalized_status not in allowed:
                raise PersonaDistillationError(
                    f"Invalid persona distillation run status '{status}'. Allowed values: {', '.join(sorted(allowed))}."
                )
            runs = [run for run in runs if run.status.value == normalized_status]
        if created_by_user_id is not None or workspace_id is not None:
            filtered_runs = []
            for run in runs:
                persona = await self.context.persona_repo.get(run.persona_id, include_deleted=True)
                if persona is None:
                    continue
                if created_by_user_id is not None and persona.created_by_user_id != created_by_user_id:
                    continue
                if workspace_id is not None and persona.workspace_id != workspace_id:
                    continue
                filtered_runs.append(run)
            runs = filtered_runs
        return {"items": [run.model_dump(mode="json") for run in runs]}

    async def list_run_items(
            self,
            run_id: str,
            *,
            source_key: str | None = None,
            item_type: str | None = None,
            memory_layer: str | None = None,
            review_status: str | None = None,
            needs_review: bool | None = None,
            min_confidence: float | None = None,
            max_confidence: float | None = None,
            extraction_source: str | None = None,
            distiller: str | None = None,
            review_flag: str | None = None,
            conflict_group_id: str | None = None,
            limit: int = 100,
            offset: int = 0,
    ) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        items = await self.context.persona_distillation_item_repo.list_by_run(run_id)
        normalized_filters = self._normalize_run_item_filters(
            source_key=source_key,
            item_type=item_type,
            memory_layer=memory_layer,
            review_status=review_status,
            needs_review=needs_review,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            extraction_source=extraction_source,
            distiller=distiller,
            review_flag=review_flag,
            conflict_group_id=conflict_group_id,
            limit=limit,
            offset=offset,
        )
        filtered_items = [
            item
            for item in items
            if self._run_item_matches_filters(item, normalized_filters)
        ]
        page_items = filtered_items[
            normalized_filters["offset"]:normalized_filters["offset"] + normalized_filters["limit"]
        ]
        return {
            "items": [self._serialize_review_item(item) for item in page_items],
            "total": len(items),
            "filtered_count": len(filtered_items),
            "limit": normalized_filters["limit"],
            "offset": normalized_filters["offset"],
            "filters": {
                key: value
                for key, value in normalized_filters.items()
                if key not in {"limit", "offset"} and value is not None
            },
            "counts": self._run_item_counts(filtered_items),
        }

    async def build_run_review_summary(self, run_id: str) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        items = await self.context.persona_distillation_item_repo.list_by_run(run_id)
        return self._run_review_summary(run, items)

    async def build_run_source_map(self, run_id: str) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        items = await self.context.persona_distillation_item_repo.list_by_run(run_id)
        source_map: dict[str, dict[str, Any]] = {}
        for item in items:
            for source_ref in self._source_map_refs(item):
                key = self._source_map_key(item, source_ref)
                entry = source_map.setdefault(key, self._source_map_entry(key, item, source_ref))
                self._add_source_map_item(entry, item)

        entries = sorted(
            source_map.values(),
            key=lambda entry: (
                str(entry.get("label") or "").lower(),
                str(entry.get("memory_id") or ""),
                str(entry.get("key") or ""),
            ),
        )
        return {
            "run_id": run.id,
            "persona_id": run.persona_id,
            "source_count": len(entries),
            "item_count": len(items),
            "needs_review_count": sum(1 for item in items if item.needs_review),
            "items": entries,
        }

    async def get_run_source_detail(
            self,
            run_id: str,
            source_key: str,
            *,
            item_type: str | None = None,
            memory_layer: str | None = None,
            review_status: str | None = None,
            needs_review: bool | None = None,
            min_confidence: float | None = None,
            max_confidence: float | None = None,
            extraction_source: str | None = None,
            distiller: str | None = None,
            review_flag: str | None = None,
            conflict_group_id: str | None = None,
            limit: int = 50,
            offset: int = 0,
    ) -> dict[str, Any]:
        source_map = await self.build_run_source_map(run_id)
        source = next((item for item in source_map["items"] if item.get("key") == source_key), None)
        if source is None:
            raise PersonaDistillationError(
                f"Persona distillation source '{source_key}' not found for run '{run_id}'."
            )
        page = await self.list_run_items(
            run_id,
            source_key=source_key,
            item_type=item_type,
            memory_layer=memory_layer,
            review_status=review_status,
            needs_review=needs_review,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            extraction_source=extraction_source,
            distiller=distiller,
            review_flag=review_flag,
            conflict_group_id=conflict_group_id,
            limit=limit,
            offset=offset,
        )
        return {
            "run_id": source_map["run_id"],
            "persona_id": source_map["persona_id"],
            "source": source,
            **page,
        }

    async def update_run_source_classification(
            self,
            run_id: str,
            source_key: str,
            *,
            classification: str | None = None,
            document_kind: str | None = None,
            content_roles: list[str] | None = None,
            extraction_targets: list[str] | None = None,
            memory_layers: list[str] | None = None,
            vector_tags: list[str] | None = None,
            confidence: float | None = None,
            rationale: str | None = None,
            current_user: UserDefinition | None = None,
    ) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        matching_items = await self._run_source_items(run_id, source_key)
        if not matching_items:
            raise PersonaDistillationError(
                f"Persona distillation source '{source_key}' not found for run '{run_id}'."
            )

        memories = await self._load_source_memories_for_items(matching_items)
        if not memories:
            raise PersonaDistillationError(
                f"Persona distillation source '{source_key}' has no linked source memory records."
            )
        current_payload = self._source_classification_payload_from_memory(memories[0])
        corrected_payload = self._corrected_source_classification_payload(
            current_payload=current_payload,
            classification=classification,
            document_kind=document_kind,
            content_roles=content_roles,
            extraction_targets=extraction_targets,
            memory_layers=memory_layers,
            vector_tags=vector_tags,
            confidence=confidence,
            rationale=rationale,
        )
        updated_memory_ids: list[str] = []
        for memory in memories:
            updated = await self._save_memory_source_classification(
                memory,
                classification_payload=corrected_payload,
                run_id=run.id,
                source_key=source_key,
                current_user=current_user,
            )
            updated_memory_ids.append(updated.id)
        updated_items = await self._stamp_source_items_with_classification(
            matching_items,
            classification_payload=corrected_payload,
            source_key=source_key,
            current_user=current_user,
        )
        await self._append_audit_event(
            "persona.factory.source.classification.updated",
            persona_id=run.persona_id,
            user_id=current_user.id if current_user is not None else None,
            payload={
                "run_id": run.id,
                "source_key": source_key,
                "source_memory_ids": updated_memory_ids,
                "item_ids": [item.id for item in updated_items],
                "classification": corrected_payload,
            },
        )
        detail = await self.get_run_source_detail(run.id, source_key)
        return {
            "run_id": run.id,
            "persona_id": run.persona_id,
            "source_key": source_key,
            "classification": corrected_payload,
            "updated_memory_ids": updated_memory_ids,
            "updated_item_count": len(updated_items),
            "source_detail": detail,
        }

    async def redistill_run_source(
            self,
            run_id: str,
            source_key: str,
            *,
            limit: int = 250,
            current_user: UserDefinition | None = None,
    ) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        persona = await self.context.persona_repo.get(run.persona_id)
        if persona is None:
            raise PersonaNotFoundError(f"Persona '{run.persona_id}' not found.")
        matching_items = await self._run_source_items(run_id, source_key)
        if not matching_items:
            raise PersonaDistillationError(
                f"Persona distillation source '{source_key}' not found for run '{run_id}'."
            )
        memories = await self._load_source_memories_for_items(matching_items)
        if not memories:
            raise PersonaDistillationError(
                f"Persona distillation source '{source_key}' has no linked source memory records."
            )
        if len(memories) > max(limit, 1):
            raise PersonaDistillationError(f"Source re-distill is limited to {limit} source memories per request.")

        superseded: list[PersonaDistillationItem] = []
        for item in matching_items:
            if item.review_status not in {
                PersonaDistillationItemReviewStatus.DRAFT,
                PersonaDistillationItemReviewStatus.NEEDS_REVIEW,
            }:
                continue
            metadata = dict(item.metadata or {})
            metadata["superseded_by_source_redistill"] = {
                "run_id": run.id,
                "source_key": source_key,
                "updated_by_user_id": current_user.id if current_user is not None else None,
                "updated_at": utc_now().isoformat(),
            }
            superseded.append(
                await self.update_item(
                    item.id,
                    {
                        "review_status": PersonaDistillationItemReviewStatus.SUPERSEDED.value,
                        "needs_review": False,
                        "metadata": metadata,
                    },
                    emit_audit=False,
                )
            )

        sources = [
            source
            for source in await self.context.persona_source_repo.list_by_persona(run.persona_id)
            if not run.input_source_ids or source.id in set(run.input_source_ids)
        ]
        items = await self._extract_distillation_items(
            persona=persona,
            run=run,
            memories=memories,
            sources=sources,
        )
        await self._append_audit_event(
            "persona.factory.source.redistilled",
            persona_id=run.persona_id,
            user_id=current_user.id if current_user is not None else None,
            payload={
                "run_id": run.id,
                "source_key": source_key,
                "source_memory_ids": [memory.id for memory in memories],
                "superseded_item_ids": [item.id for item in superseded],
                "created_item_ids": [item.id for item in items],
            },
        )
        detail = await self.get_run_source_detail(run.id, source_key)
        return {
            "run_id": run.id,
            "persona_id": run.persona_id,
            "source_key": source_key,
            "superseded_count": len(superseded),
            "created_count": len(items),
            "superseded_items": [item.model_dump(mode="json") for item in superseded],
            "items": [item.model_dump(mode="json") for item in items],
            "source_detail": detail,
        }

    async def item_catalog(self) -> dict[str, Any]:
        source_catalog = SourceIntelligenceService.catalog()
        model_profiles = await self.context.model_profile_repo.list()
        settings = get_settings()
        return {
            "item_types": [item.value for item in PersonaDistillationItemType],
            "memory_layers": [item.value for item in PersonaMemoryLayer],
            "review_statuses": [item.value for item in PersonaDistillationItemReviewStatus],
            "distillation_modes": [item.value for item in PersonaDistillationMode],
            "llm_model_sources": [item.value for item in PersonaLLMModelSource],
            "package_synthesis_modes": ["reviewed_items", "llm_polished"],
            "extraction_sources": sorted(PERSONA_REVIEW_EXTRACTION_SOURCES),
            "reviewer_actions": sorted(PERSONA_REVIEW_ACTIONS),
            "model_profiles": [
                {
                    "id": profile.id,
                    "name": profile.name,
                    "provider": profile.provider,
                    "model": profile.model,
                }
                for profile in model_profiles
            ],
            "operational_settings": {
                "default_distillation_mode": settings.persona_factory_default_distillation_mode,
                "default_llm_model_source": settings.persona_factory_default_llm_model_source,
                "llm_distillation_enabled": settings.persona_factory_llm_distillation_enabled,
                "hybrid_distillation_enabled": settings.persona_factory_hybrid_distillation_enabled,
                "max_documents_per_run": settings.persona_factory_max_documents_per_run,
                "max_source_memories_per_run": settings.persona_factory_max_source_memories_per_run,
                "max_source_characters_per_run": settings.persona_factory_max_source_characters_per_run,
                "llm_max_source_memories_per_run": settings.persona_factory_llm_max_source_memories_per_run,
                "llm_max_source_characters_per_run": settings.persona_factory_llm_max_source_characters_per_run,
                "llm_max_source_tokens_per_run": settings.persona_factory_llm_max_source_tokens_per_run,
                "llm_max_calls_per_run": settings.persona_factory_llm_max_calls_per_run,
                "llm_timeout_seconds": settings.persona_factory_llm_timeout_seconds,
                "llm_retry_attempts": settings.persona_factory_llm_retry_attempts,
            },
            "source_classifications": source_catalog["source_classifications"],
            "document_kinds": source_catalog["document_kinds"],
            "graph_entity_labels": source_catalog["graph_entity_labels"],
            "graph_relationship_types": source_catalog["graph_relationship_types"],
        }

    async def update_item(
            self,
            item_id: str,
            patch: dict[str, Any],
            *,
            emit_audit: bool = True,
    ) -> PersonaDistillationItem:
        current = await self.context.persona_distillation_item_repo.get(item_id)
        if current is None:
            raise PersonaDistillationError(f"Persona distillation item '{item_id}' not found.")
        allowed_fields = {
            "source_memory_id",
            "item_type",
            "memory_layer",
            "title",
            "content",
            "structured_payload",
            "confidence",
            "needs_review",
            "review_status",
            "metadata",
        }
        unknown_fields = sorted(set(patch).difference(allowed_fields))
        if unknown_fields:
            raise PersonaDistillationError(
                f"Unsupported persona distillation item fields: {', '.join(unknown_fields)}.")
        merged = current.model_dump(mode="json")
        merged.update(patch)
        if merged.get("review_status") == PersonaDistillationItemReviewStatus.APPROVED.value:
            merged["needs_review"] = False
        if merged.get("review_status") == PersonaDistillationItemReviewStatus.REJECTED.value:
            merged["needs_review"] = False
        try:
            item = PersonaDistillationItem.model_validate(merged)
        except ValueError as exc:
            raise PersonaDistillationError(str(exc)) from exc
        saved = await self.context.persona_distillation_item_repo.save(item)
        if emit_audit:
            await self._append_audit_event(
                "persona.factory.item.updated",
                persona_id=saved.persona_id,
                payload={
                    "run_id": saved.run_id,
                    "item_id": saved.id,
                    "source_memory_id": saved.source_memory_id,
                    "patched_fields": sorted(patch),
                    "item_type": saved.item_type.value,
                    "memory_layer": saved.memory_layer.value,
                    "title": saved.title,
                    "review_status": saved.review_status.value,
                    "needs_review": saved.needs_review,
                },
            )
        return saved

    async def approve_item(self, item_id: str) -> PersonaDistillationItem:
        item = await self.update_item(
            item_id,
            {
                "review_status": PersonaDistillationItemReviewStatus.APPROVED.value,
                "needs_review": False,
            },
            emit_audit=False,
        )
        await self._append_audit_event(
            "persona.factory.item.approved",
            persona_id=item.persona_id,
            payload={
                "run_id": item.run_id,
                "item_id": item.id,
                "source_memory_id": item.source_memory_id,
                "item_type": item.item_type.value,
                "memory_layer": item.memory_layer.value,
                "title": item.title,
                "review_status": item.review_status.value,
                "needs_review": item.needs_review,
            },
        )
        await self._append_approved_item_graph_hints_event(item)
        return item

    async def reject_item(self, item_id: str, *, reason: str | None = None) -> PersonaDistillationItem:
        current = await self.context.persona_distillation_item_repo.get(item_id)
        if current is None:
            raise PersonaDistillationError(f"Persona distillation item '{item_id}' not found.")
        metadata = dict(current.metadata or {})
        if reason:
            metadata["rejection_reason"] = reason
        item = await self.update_item(
            item_id,
            {
                "review_status": PersonaDistillationItemReviewStatus.REJECTED.value,
                "needs_review": False,
                "metadata": metadata,
            },
            emit_audit=False,
        )
        await self._append_audit_event(
            "persona.factory.item.rejected",
            persona_id=item.persona_id,
            payload={
                "run_id": item.run_id,
                "item_id": item.id,
                "source_memory_id": item.source_memory_id,
                "item_type": item.item_type.value,
                "memory_layer": item.memory_layer.value,
                "title": item.title,
                "review_status": item.review_status.value,
                "needs_review": item.needs_review,
                "reason": reason,
            },
        )
        return item

    async def bulk_review_items(
            self,
            *,
            item_ids: list[str],
            action: str,
            reason: str | None = None,
    ) -> list[PersonaDistillationItem]:
        unique_item_ids = list(dict.fromkeys(item_ids))
        if not unique_item_ids:
            raise PersonaDistillationError("At least one persona distillation item id is required.")
        if len(unique_item_ids) > 250:
            raise PersonaDistillationError("Bulk review is limited to 250 persona distillation items per request.")
        if action not in {"approve", "reject"}:
            raise PersonaDistillationError("Bulk review action must be 'approve' or 'reject'.")

        reviewed: list[PersonaDistillationItem] = []
        for item_id in unique_item_ids:
            if action == "approve":
                reviewed.append(await self.approve_item(item_id))
            else:
                reviewed.append(await self.reject_item(item_id, reason=reason))
        return reviewed

    async def bulk_review_run_items(
            self,
            run_id: str,
            *,
            action: str,
            reason: str | None = None,
            source_key: str | None = None,
            item_type: str | None = None,
            memory_layer: str | None = None,
            review_status: str | None = None,
            needs_review: bool | None = None,
            min_confidence: float | None = None,
            max_confidence: float | None = None,
            extraction_source: str | None = None,
            distiller: str | None = None,
            review_flag: str | None = None,
            conflict_group_id: str | None = None,
            limit: int = 250,
    ) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        if action not in {"approve", "reject"}:
            raise PersonaDistillationError("Bulk review action must be 'approve' or 'reject'.")
        normalized_filters = self._normalize_run_item_filters(
            source_key=source_key,
            item_type=item_type,
            memory_layer=memory_layer,
            review_status=review_status,
            needs_review=needs_review,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            extraction_source=extraction_source,
            distiller=distiller,
            review_flag=review_flag,
            conflict_group_id=conflict_group_id,
            limit=limit,
            offset=0,
        )
        items = await self.context.persona_distillation_item_repo.list_by_run(run_id)
        matching_items = [
            item
            for item in items
            if self._run_item_matches_filters(item, normalized_filters)
        ]
        reviewable_items = [
            item
            for item in matching_items
            if item.review_status not in {
                PersonaDistillationItemReviewStatus.APPROVED,
                PersonaDistillationItemReviewStatus.REJECTED,
                PersonaDistillationItemReviewStatus.SUPERSEDED,
            }
        ]
        selected_items = reviewable_items[:normalized_filters["limit"]]
        if not selected_items:
            raise PersonaDistillationError("No reviewable persona distillation items matched the supplied filters.")
        reviewed = await self.bulk_review_items(
            item_ids=[item.id for item in selected_items],
            action=action,
            reason=reason,
        )
        return {
            "run_id": run.id,
            "action": action,
            "count": len(reviewed),
            "matched_count": len(matching_items),
            "reviewable_count": len(reviewable_items),
            "limit": normalized_filters["limit"],
            "has_more": len(reviewable_items) > len(selected_items),
            "filters": {
                key: value
                for key, value in normalized_filters.items()
                if key not in {"limit", "offset"} and value is not None
            },
            "items": reviewed,
        }

    async def preview_bulk_review_run_items(
            self,
            run_id: str,
            *,
            action: str,
            source_key: str | None = None,
            item_type: str | None = None,
            memory_layer: str | None = None,
            review_status: str | None = None,
            needs_review: bool | None = None,
            min_confidence: float | None = None,
            max_confidence: float | None = None,
            extraction_source: str | None = None,
            distiller: str | None = None,
            review_flag: str | None = None,
            conflict_group_id: str | None = None,
            limit: int = 250,
    ) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        if action not in {"approve", "reject"}:
            raise PersonaDistillationError("Bulk review action must be 'approve' or 'reject'.")
        normalized_filters = self._normalize_run_item_filters(
            source_key=source_key,
            item_type=item_type,
            memory_layer=memory_layer,
            review_status=review_status,
            needs_review=needs_review,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            extraction_source=extraction_source,
            distiller=distiller,
            review_flag=review_flag,
            conflict_group_id=conflict_group_id,
            limit=limit,
            offset=0,
        )
        items = await self.context.persona_distillation_item_repo.list_by_run(run_id)
        matching_items = [
            item
            for item in items
            if self._run_item_matches_filters(item, normalized_filters)
        ]
        reviewable_items = [
            item
            for item in matching_items
            if item.review_status not in {
                PersonaDistillationItemReviewStatus.APPROVED,
                PersonaDistillationItemReviewStatus.REJECTED,
                PersonaDistillationItemReviewStatus.SUPERSEDED,
            }
        ]
        selected_items = reviewable_items[:normalized_filters["limit"]]
        return {
            "run_id": run.id,
            "action": action,
            "count": len(selected_items),
            "matched_count": len(matching_items),
            "reviewable_count": len(reviewable_items),
            "limit": normalized_filters["limit"],
            "has_more": len(reviewable_items) > len(selected_items),
            "filters": {
                key: value
                for key, value in normalized_filters.items()
                if key not in {"limit", "offset"} and value is not None
            },
            "items": selected_items[:10],
        }

    async def apply_run_review_action(
            self,
            run_id: str,
            *,
            action: str,
            item_ids: list[str] | None = None,
            conflict_group_id: str | None = None,
            reason: str | None = None,
            patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        normalized_action = action.strip().lower() if isinstance(action, str) else ""
        if normalized_action not in PERSONA_REVIEW_ACTIONS:
            raise PersonaDistillationError(
                f"Review action must be one of: {', '.join(sorted(PERSONA_REVIEW_ACTIONS))}."
            )
        all_items = await self.context.persona_distillation_item_repo.list_by_run(run_id)
        selected = self._select_review_action_items(
            all_items,
            item_ids=item_ids or [],
            conflict_group_id=conflict_group_id,
        )
        if not selected:
            raise PersonaDistillationError("No persona distillation items matched the review action target.")

        reviewed: list[PersonaDistillationItem] = []
        if normalized_action in {"prefer_llm", "prefer_deterministic"}:
            preferred_source = normalized_action.removeprefix("prefer_")
            preferred = [
                item for item in selected
                if self._item_review_metadata(item)["extraction_source"] == preferred_source
            ]
            if not preferred:
                raise PersonaDistillationError(f"No {preferred_source} items matched the review action target.")
            rejected_ids = {item.id for item in selected if
                            item.id not in {preferred_item.id for preferred_item in preferred}}
            for item in preferred:
                reviewed.append(await self.approve_item(item.id))
            for item in selected:
                if item.id in rejected_ids:
                    reviewed.append(await self.reject_item(item.id,
                                                           reason=reason or f"Reviewer preferred {preferred_source} output."))
        elif normalized_action == "mark_evidence_insufficient":
            for item in selected:
                reviewed.append(
                    await self._mark_item_evidence_insufficient(item, reason=reason)
                )
        else:
            reviewed = await self._mark_items_for_manual_merge(selected, reason=reason, patch=patch)

        await self._append_audit_event(
            "persona.factory.review.action_applied",
            persona_id=run.persona_id,
            payload={
                "run_id": run.id,
                "action": normalized_action,
                "item_ids": [item.id for item in reviewed],
                "conflict_group_id": conflict_group_id,
                "reason": reason,
            },
        )
        refreshed_items = await self.context.persona_distillation_item_repo.list_by_run(run_id)
        return {
            "run_id": run.id,
            "action": normalized_action,
            "count": len(reviewed),
            "items": [self._serialize_review_item(item) for item in reviewed],
            "review_summary": self._run_review_summary(run, refreshed_items),
        }

    async def capture_feedback(
            self,
            *,
            persona_id: str,
            title: str | None,
            content: str,
            item_type: str,
            memory_layer: str,
            feedback_type: str,
            confidence: float,
            source_memory_id: str | None,
            accepted_edit_of_item_id: str | None,
            source_conversation_id: str | None,
            source_message_id: str | None,
            source_run_id: str | None,
            metadata: dict[str, Any] | None,
            current_user: UserDefinition,
    ) -> dict[str, Any]:
        persona = await self.context.persona_repo.get(persona_id)
        if persona is None:
            raise PersonaNotFoundError(f"Persona '{persona_id}' not found.")
        normalized_content = content.strip()
        if not normalized_content:
            raise PersonaDistillationError("Feedback content is required.")
        try:
            normalized_item_type = PersonaDistillationItemType(item_type.strip().lower())
            normalized_memory_layer = PersonaMemoryLayer(memory_layer.strip().lower())
        except ValueError as exc:
            raise PersonaDistillationError("Feedback item_type or memory_layer is not supported.") from exc
        source_memory = await self._feedback_source_memory(
            persona=persona,
            title=title,
            content=normalized_content,
            item_type=normalized_item_type,
            memory_layer=normalized_memory_layer,
            feedback_type=feedback_type,
            source_memory_id=source_memory_id,
            accepted_edit_of_item_id=accepted_edit_of_item_id,
            source_conversation_id=source_conversation_id,
            source_message_id=source_message_id,
            source_run_id=source_run_id,
            metadata=metadata or {},
            current_user=current_user,
        )
        sources = await self._ensure_memory_sources(persona=persona, memories=[source_memory])
        run = await self.context.persona_distillation_run_repo.create(
            PersonaDistillationRun(
                persona_id=persona.id,
                status=PersonaDistillationStatus.NEEDS_REVIEW,
                input_source_ids=[item.id for item in sources],
                output_package=await self._feedback_draft_package(persona=persona),
                warnings=[
                    {
                        "type": "continuous_learning",
                        "message": "Feedback was captured as a reviewable persona memory candidate.",
                        "created_at": utc_now().isoformat(),
                    }
                ],
            )
        )
        source_ref = self._source_ref(source_memory, sources[0] if sources else None)
        # Continuous-learning feedback is intentionally review-gated so accepted edits cannot
        # silently change a published persona until a reviewer approves and publishes a new version.
        item = await self.context.persona_distillation_item_repo.create(
            PersonaDistillationItem(
                run_id=run.id,
                persona_id=persona.id,
                source_memory_id=source_memory.id,
                item_type=normalized_item_type,
                memory_layer=normalized_memory_layer,
                title=(title or source_memory.summary or self._title_from_content(normalized_content)).strip(),
                content=normalized_content,
                structured_payload={
                    "source_ref": source_ref,
                    "source_refs": [source_ref],
                    "feedback_type": feedback_type,
                    "accepted_edit_of_item_id": accepted_edit_of_item_id,
                    "source_conversation_id": source_conversation_id,
                    "source_message_id": source_message_id,
                    "source_run_id": source_run_id,
                    "pipeline": "continuous-learning-feedback-v1",
                },
                confidence=max(min(confidence, 1.0), 0.0),
                needs_review=True,
                review_status=PersonaDistillationItemReviewStatus.NEEDS_REVIEW,
                metadata={
                    **(metadata or {}),
                    "persona_slug": persona.slug,
                    "source": "persona_feedback",
                    "feedback_type": feedback_type,
                    "accepted_edit_of_item_id": accepted_edit_of_item_id,
                    "source_conversation_id": source_conversation_id,
                    "source_message_id": source_message_id,
                    "source_run_id": source_run_id,
                    "requires_review_before_publish": True,
                },
            )
        )
        if persona.status != PersonaStatus.PUBLISHED:
            await self.context.persona_repo.update(persona.id, {"status": PersonaStatus.IN_REVIEW.value})
            persona = await self.context.persona_repo.get(persona.id) or persona
        await self._append_audit_event(
            "persona.factory.feedback.captured",
            persona_id=persona.id,
            user_id=current_user.id,
            payload={
                "run_id": run.id,
                "item_id": item.id,
                "source_memory_id": source_memory.id,
                "source_ids": run.input_source_ids,
                "feedback_type": feedback_type,
                "accepted_edit_of_item_id": accepted_edit_of_item_id,
                "requires_review_before_publish": True,
            },
        )
        return {
            "persona": persona.model_dump(mode="json"),
            "run": run.model_dump(mode="json"),
            "sources": [item.model_dump(mode="json") for item in sources],
            "items": [item.model_dump(mode="json")],
            "source_memory": source_memory.model_dump(mode="json"),
        }

    async def rollback_to_version(
            self,
            *,
            persona_id: str,
            version_id: str,
            current_user: UserDefinition,
    ) -> dict[str, Any]:
        persona = await self.context.persona_repo.get(persona_id)
        if persona is None:
            raise PersonaNotFoundError(f"Persona '{persona_id}' not found.")
        version = await self.context.persona_version_repo.get(version_id, include_deleted=True)
        if version is None or version.persona_id != persona.id:
            raise PersonaNotFoundError(f"Persona version '{version_id}' not found.")
        if version.status not in {PersonaVersionStatus.APPROVED, PersonaVersionStatus.PUBLISHED}:
            raise PersonaPublishError("Only approved or published persona versions can be rolled back to.")
        previous_version_id = persona.current_version_id
        result = await self.publish_version(persona=persona, version=version, current_user=current_user)
        await self._append_audit_event(
            "persona.factory.version.rolled_back",
            persona_id=persona.id,
            user_id=current_user.id,
            payload={
                "previous_version_id": previous_version_id,
                "restored_version_id": version.id,
                "version": version.version,
            },
        )
        result["rollback"] = {
            "previous_version_id": previous_version_id,
            "restored_version_id": version.id,
        }
        return result

    async def normalize_run_items(self, run_id: str) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        persona = await self.context.persona_repo.get(run.persona_id)
        if persona is None:
            raise PersonaNotFoundError(f"Persona '{run.persona_id}' not found.")
        items = await self.context.persona_distillation_item_repo.list_by_run(run.id)
        normalized_items, summary = await self._normalize_distillation_items(items)
        if run.model_profile_id:
            normalized_items, llm_summary = await self._llm_normalize_distillation_items(
                run=run,
                items=normalized_items,
                model_profile_id=run.model_profile_id,
            )
            summary = {
                **summary,
                "llm_normalization": llm_summary,
            }
        run = await self.context.persona_distillation_run_repo.save(
            run.model_copy(
                update={
                    "status": PersonaDistillationStatus.NEEDS_REVIEW,
                    "warnings": [
                        *run.warnings,
                        {
                            "type": "normalization",
                            "message": "Persona distillation items normalized.",
                            "summary": summary,
                            "created_at": utc_now().isoformat(),
                        },
                    ],
                }
            )
        )
        await self._append_audit_event(
            "persona.factory.items.normalized",
            persona_id=persona.id,
            payload={"run_id": run.id, **summary},
        )
        return {
            "persona": persona.model_dump(mode="json"),
            "run": run.model_dump(mode="json"),
            "items": [item.model_dump(mode="json") for item in normalized_items],
            "normalization": summary,
        }

    async def synthesize_package_from_items(
            self,
            run_id: str,
            *,
            package_synthesis_mode: str = "reviewed_items",
            llm_polishing_model_profile_id: str | None = None,
    ) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        persona = await self.context.persona_repo.get(run.persona_id)
        if persona is None:
            raise PersonaNotFoundError(f"Persona '{run.persona_id}' not found.")
        normalized_mode = package_synthesis_mode.strip().lower() if isinstance(package_synthesis_mode, str) else ""
        if normalized_mode not in {"reviewed_items", "llm_polished"}:
            raise PersonaDistillationError("package_synthesis_mode must be 'reviewed_items' or 'llm_polished'.")
        all_items = await self.context.persona_distillation_item_repo.list_by_run(run.id)
        active_items = [
            item for item in all_items
            if item.review_status not in {
                PersonaDistillationItemReviewStatus.REJECTED,
                PersonaDistillationItemReviewStatus.SUPERSEDED,
            }
        ]
        if not active_items:
            raise PersonaDistillationError("Cannot synthesize a persona package without active distillation items.")
        if normalized_mode == "llm_polished":
            approved_items = [
                item for item in active_items
                if item.review_status == PersonaDistillationItemReviewStatus.APPROVED
            ]
            if not approved_items:
                raise PersonaDistillationError(
                    "LLM package polishing requires at least one approved distillation item.")
            package = self._package_from_items(persona=persona, run=run, items=approved_items, all_items=all_items)
            package = await self._llm_polish_package(
                persona=persona,
                run=run,
                base_package=package,
                approved_items=approved_items,
                model_profile_id=llm_polishing_model_profile_id,
            )
        else:
            package = self._package_from_items(persona=persona, run=run, items=active_items, all_items=all_items)
        run = await self.context.persona_distillation_run_repo.save(
            run.model_copy(update={"output_package": package, "status": PersonaDistillationStatus.NEEDS_REVIEW})
        )
        await self._append_audit_event(
            "persona.factory.package.synthesized",
            persona_id=persona.id,
            payload={
                "run_id": run.id,
                "active_item_count": len(active_items),
                "excluded_item_count": len(package.get("provenance", {}).get("excluded_item_ids", [])),
                "needs_review_count": package.get("provenance", {}).get("needs_review_count"),
                **self._package_graph_projection_payload(package),
            },
        )
        return {
            "persona": persona.model_dump(mode="json"),
            "run": run.model_dump(mode="json"),
            "items": [item.model_dump(mode="json") for item in active_items],
        }

    async def _normalize_distillation_items(
            self,
            items: list[PersonaDistillationItem],
    ) -> tuple[list[PersonaDistillationItem], dict[str, Any]]:
        active_items = [
            item for item in items
            if item.review_status not in {
                PersonaDistillationItemReviewStatus.REJECTED,
                PersonaDistillationItemReviewStatus.SUPERSEDED,
            }
        ]
        by_key: dict[tuple[str, str, str], list[PersonaDistillationItem]] = {}
        for item in active_items:
            by_key.setdefault(self._normalization_key(item), []).append(item)

        saved_by_id: dict[str, PersonaDistillationItem] = {}
        merged_count = 0
        superseded_count = 0
        for group in by_key.values():
            if len(group) == 1:
                saved_by_id[group[0].id] = group[0]
                continue
            primary, *duplicates = sorted(group, key=self._normalization_preference_key)
            merged = await self.context.persona_distillation_item_repo.save(
                self._merge_duplicate_items(primary, duplicates)
            )
            saved_by_id[merged.id] = merged
            merged_count += len(duplicates)
            for duplicate in duplicates:
                superseded = await self.context.persona_distillation_item_repo.save(
                    duplicate.model_copy(
                        update={
                            "review_status": PersonaDistillationItemReviewStatus.SUPERSEDED,
                            "needs_review": False,
                            "metadata": {
                                **(duplicate.metadata or {}),
                                "superseded_by_item_id": merged.id,
                                "normalization_reason": "duplicate",
                            },
                        }
                    )
                )
                saved_by_id[superseded.id] = superseded
                superseded_count += 1

        conflict_count = 0
        current_items = [
            item for item in saved_by_id.values()
            if item.review_status not in {
                PersonaDistillationItemReviewStatus.REJECTED,
                PersonaDistillationItemReviewStatus.SUPERSEDED,
            }
        ]
        by_topic: dict[tuple[str, str, str], list[PersonaDistillationItem]] = {}
        for item in current_items:
            by_topic.setdefault(self._conflict_topic_key(item), []).append(item)
        for group in by_topic.values():
            if len(group) < 2 or not self._group_has_conflict(group):
                continue
            conflict_ids = [item.id for item in group]
            for item in group:
                metadata = {
                    **(item.metadata or {}),
                    "conflict": True,
                    "conflicting_item_ids": [item_id for item_id in conflict_ids if item_id != item.id],
                }
                saved = await self.context.persona_distillation_item_repo.save(
                    item.model_copy(
                        update={
                            "needs_review": True,
                            "review_status": PersonaDistillationItemReviewStatus.NEEDS_REVIEW,
                            "metadata": metadata,
                        }
                    )
                )
                saved_by_id[saved.id] = saved
            conflict_count += 1

        all_items = await self.context.persona_distillation_item_repo.list_by_run(items[0].run_id) if items else []
        summary = {
            "strategy": "deterministic-normalization-v1",
            "input_count": len(items),
            "active_input_count": len(active_items),
            "merged_duplicate_count": merged_count,
            "superseded_count": superseded_count,
            "conflict_group_count": conflict_count,
            "output_count": len([
                item for item in all_items
                if item.review_status != PersonaDistillationItemReviewStatus.SUPERSEDED
            ]),
        }
        return all_items, summary

    async def _llm_normalize_distillation_items(
            self,
            *,
            run: PersonaDistillationRun,
            items: list[PersonaDistillationItem],
            model_profile_id: str,
    ) -> tuple[list[PersonaDistillationItem], dict[str, Any]]:
        active_items = [
            item for item in items
            if item.review_status not in {
                PersonaDistillationItemReviewStatus.REJECTED,
                PersonaDistillationItemReviewStatus.SUPERSEDED,
            }
        ]
        if not active_items:
            return items, {"strategy": "llm-normalization-v1", "skipped": "no_active_items"}
        profile = await self._resolve_required_model_profile(model_profile_id)
        payload = await self._generate_structured_with_profile(
            profile=profile,
            schema_name="persona_factory_normalization",
            schema=_LLMNormalizationPayload.model_json_schema(),
            system=(
                "You normalize Agency Persona distillation items. Return only schema-valid JSON. "
                "Do not invent facts. Only update wording, flag conflicts, or mark clear duplicates."
            ),
            prompt=json.dumps(
                {
                    "run_id": run.id,
                    "items": [
                        {
                            "item_id": item.id,
                            "item_type": item.item_type.value,
                            "memory_layer": item.memory_layer.value,
                            "title": item.title,
                            "content": item.content,
                            "confidence": item.confidence,
                            "review_status": item.review_status.value,
                            "needs_review": item.needs_review,
                            "source_memory_id": item.source_memory_id,
                        }
                        for item in active_items[:80]
                    ],
                    "allowed_actions": {
                        "updates": "Use for safer titles/content/confidence or to require review.",
                        "superseded": "Use only when one item is a clear duplicate of another.",
                        "conflict_groups": "Use when items materially disagree and require human review.",
                    },
                },
                ensure_ascii=True,
            ),
        )
        try:
            normalized = _LLMNormalizationPayload.model_validate(payload)
        except ValueError as exc:
            raise PersonaDistillationError(f"LLM normalization output failed schema validation: {exc}") from exc

        active_by_id = {item.id: item for item in active_items}
        all_referenced_ids: set[str] = set()
        all_referenced_ids.update(update.item_id for update in normalized.updates)
        all_referenced_ids.update(item.item_id for item in normalized.superseded)
        all_referenced_ids.update(item.superseded_by_item_id for item in normalized.superseded)
        for group in normalized.conflict_groups:
            all_referenced_ids.update(group.item_ids)
        unknown_ids = sorted(item_id for item_id in all_referenced_ids if item_id not in active_by_id)
        if unknown_ids:
            raise PersonaDistillationError(
                f"LLM normalization referenced unknown or inactive item ids: {', '.join(unknown_ids)}."
            )

        saved_by_id: dict[str, PersonaDistillationItem] = {item.id: item for item in items}
        for update in normalized.updates:
            current = saved_by_id[update.item_id]
            patch: dict[str, Any] = {
                "metadata": {
                    **(current.metadata or {}),
                    "llm_normalization": {
                        "model_profile_id": profile.id,
                        "model": profile.model,
                        "rationale": update.rationale,
                    },
                },
            }
            if update.title is not None:
                patch["title"] = update.title
            if update.content is not None:
                patch["content"] = update.content
            if update.confidence is not None:
                patch["confidence"] = update.confidence
            if update.title is not None or update.content is not None or update.needs_review is True:
                patch["needs_review"] = True
                patch["review_status"] = PersonaDistillationItemReviewStatus.NEEDS_REVIEW.value
            elif update.needs_review is not None:
                patch["needs_review"] = update.needs_review
            updated_payload = current.model_dump(mode="json")
            updated_payload.update(patch)
            saved_by_id[update.item_id] = await self.context.persona_distillation_item_repo.save(
                PersonaDistillationItem.model_validate(updated_payload)
            )

        for duplicate in normalized.superseded:
            current = saved_by_id[duplicate.item_id]
            updated_payload = current.model_dump(mode="json")
            updated_payload.update(
                {
                    "needs_review": False,
                    "review_status": PersonaDistillationItemReviewStatus.SUPERSEDED.value,
                    "metadata": {
                        **(current.metadata or {}),
                        "superseded_by_item_id": duplicate.superseded_by_item_id,
                        "normalization_reason": duplicate.reason or "llm_duplicate",
                        "llm_normalization": {
                            "model_profile_id": profile.id,
                            "model": profile.model,
                            "rationale": duplicate.reason,
                        },
                    },
                }
            )
            saved_by_id[duplicate.item_id] = await self.context.persona_distillation_item_repo.save(
                PersonaDistillationItem.model_validate(updated_payload)
            )

        for group in normalized.conflict_groups:
            for item_id in group.item_ids:
                current = saved_by_id[item_id]
                updated_payload = current.model_dump(mode="json")
                updated_payload.update(
                    {
                        "needs_review": True,
                        "review_status": PersonaDistillationItemReviewStatus.NEEDS_REVIEW.value,
                        "metadata": {
                            **(current.metadata or {}),
                            "conflict": True,
                            "conflicting_item_ids": [
                                other_id for other_id in group.item_ids if other_id != item_id
                            ],
                            "llm_normalization": {
                                "model_profile_id": profile.id,
                                "model": profile.model,
                                "rationale": group.reason,
                            },
                        },
                    }
                )
                saved_by_id[item_id] = await self.context.persona_distillation_item_repo.save(
                    PersonaDistillationItem.model_validate(updated_payload)
                )

        all_items = await self.context.persona_distillation_item_repo.list_by_run(run.id)
        return all_items, {
            "strategy": "llm-normalization-v1",
            "model_profile_id": profile.id,
            "model": profile.model,
            "update_count": len(normalized.updates),
            "superseded_count": len(normalized.superseded),
            "conflict_group_count": len(normalized.conflict_groups),
            "summary": normalized.summary,
        }

    async def update_run_package(self, run_id: str, package: dict[str, Any]) -> PersonaDistillationRun:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        self._validate_package(package)
        saved = await self.context.persona_distillation_run_repo.save(
            run.model_copy(update={"output_package": package, "status": PersonaDistillationStatus.NEEDS_REVIEW})
        )
        await self._append_audit_event(
            "persona.factory.package.updated",
            persona_id=saved.persona_id,
            payload={
                "run_id": saved.id,
                "strategy": package.get("provenance", {}).get("strategy"),
                **self._package_graph_projection_payload(package),
            },
        )
        return saved

    async def approve_run(
            self,
            run_id: str,
            *,
            current_user: UserDefinition,
            version: str | None = None,
    ) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        persona = await self.context.persona_repo.get(run.persona_id)
        if persona is None:
            raise PersonaNotFoundError(f"Persona '{run.persona_id}' not found.")
        package = dict(run.output_package or {})
        self._validate_package(package)
        self._validate_package_review_ready(package)
        version_label = version or await self._next_version_label(persona.id)
        persona_version = await self.context.persona_version_repo.create(
            PersonaVersion(
                persona_id=persona.id,
                version=version_label,
                status=PersonaVersionStatus.APPROVED,
                package=package,
                generated_from_run_id=run.id,
                approved_by_user_id=current_user.id,
            )
        )
        run = await self.context.persona_distillation_run_repo.save(
            run.model_copy(update={"status": PersonaDistillationStatus.COMPLETED, "completed_at": utc_now()})
        )
        persona = await self.context.persona_repo.update(
            persona.id,
            {
                "status": PersonaStatus.APPROVED.value,
                "current_version_id": persona_version.id,
            },
        )
        await self._append_audit_event(
            "persona.factory.run.approved",
            persona_id=persona.id,
            user_id=current_user.id,
            payload={
                "run_id": run.id,
                "persona_version_id": persona_version.id,
                "version": persona_version.version,
                **self._package_graph_projection_payload(package),
            },
        )
        persona_payload = persona.model_dump(mode="json")
        version_payload = persona_version.model_dump(mode="json")
        return {
            "persona": persona_payload,
            "run": run.model_dump(mode="json"),
            "persona_version": version_payload,
        }

    async def publish_run(self, run_id: str, *, current_user: UserDefinition) -> dict[str, Any]:
        run = await self.context.persona_distillation_run_repo.get(run_id)
        if run is None:
            raise PersonaDistillationError(f"Persona distillation run '{run_id}' not found.")
        persona = await self.context.persona_repo.get(run.persona_id)
        if persona is None:
            raise PersonaNotFoundError(f"Persona '{run.persona_id}' not found.")
        version = await self._approved_version_for_run(persona=persona, run=run, current_user=current_user)
        return await self.publish_version(persona=persona, version=version, current_user=current_user)

    async def publish_version(
            self,
            *,
            persona: PersonaDefinition,
            version: PersonaVersion,
            current_user: UserDefinition,
    ) -> dict[str, Any]:
        if version.status not in {PersonaVersionStatus.APPROVED, PersonaVersionStatus.PUBLISHED}:
            raise PersonaPublishError("Only approved persona versions can be published.")
        package = dict(version.package or {})
        self._validate_package(package)
        self._validate_package_review_ready(package)
        agent = await PersonaService(self.context).agent_definition_for_package(
            persona=persona,
            version_id=version.id,
            package=package,
        )
        existing_agent = await self.context.agent_repo.get(agent.id, include_deleted=True)
        agent = await (
            self.context.agent_repo.save(agent) if existing_agent is not None else self.context.agent_repo.create(agent)
        )
        memory_ids = await self._publish_persona_memories(
            persona=persona,
            version=version,
            package=package,
            current_user=current_user,
        )
        package.setdefault("provenance", {})["published_memory_ids"] = memory_ids
        version = await self.context.persona_version_repo.save(
            version.model_copy(
                update={
                    "status": PersonaVersionStatus.PUBLISHED,
                    "package": package,
                    "published_at": utc_now(),
                }
            )
        )
        persona = await self.context.persona_repo.update(
            persona.id,
            {
                "status": PersonaStatus.PUBLISHED.value,
                "current_version_id": version.id,
                "published_agent_id": agent.id,
            },
        )
        await self._append_audit_event(
            "persona.factory.version.published",
            persona_id=persona.id,
            user_id=current_user.id,
            payload={
                "persona_version_id": version.id,
                "version": version.version,
                "agent_id": agent.id,
                "memory_ids": memory_ids,
                **self._package_graph_projection_payload(package),
            },
        )
        persona_payload = persona.model_dump(mode="json")
        version_payload = version.model_dump(mode="json")
        return {
            "persona": persona_payload,
            "persona_version": version_payload,
            "agent": agent.model_dump(mode="json"),
            "memory_ids": memory_ids,
        }

    async def _resolve_or_create_persona(
            self,
            *,
            persona_id: str | None,
            name: str | None,
            description: str | None,
            current_user: UserDefinition,
    ) -> PersonaDefinition:
        if persona_id:
            persona = await self.context.persona_repo.get(persona_id)
            if persona is None:
                raise PersonaNotFoundError(f"Persona '{persona_id}' not found.")
            return persona
        if not name or not name.strip():
            raise PersonaDistillationError("Persona name is required when persona_id is not provided.")
        return await PersonaService(self.context).create_persona(
            {"name": name, "description": description},
            current_user=current_user,
        )

    async def _append_audit_event(
            self,
            event_type: str,
            *,
            persona_id: str,
            payload: dict[str, Any],
            user_id: str | None = None,
    ) -> None:
        if not get_settings().graph_projection_enabled:
            return
        repo = getattr(self.context, "graph_projection_event_repo", None)
        if repo is None:
            return
        try:
            persona = await self.context.persona_repo.get(persona_id)
            enriched_payload = {
                **payload,
                "persona_id": persona_id,
            }
            if persona is not None:
                enriched_payload.update(
                    {
                        "persona_slug": persona.slug,
                        "persona_name": persona.name,
                        "persona_status": persona.status.value,
                        "workspace_id": persona.workspace_id,
                    }
                )
            await repo.append(
                GraphProjectionEvent(
                    event_type=event_type,
                    aggregate_type="persona",
                    aggregate_id=persona_id,
                    user_id=user_id,
                    payload=enriched_payload,
                    source="persona_factory",
                )
            )
        except Exception:
            # Audit projection should not make the primary Persona Factory write fail.
            return

    async def _append_approved_item_graph_hints_event(self, item: PersonaDistillationItem) -> None:
        payload = self._approved_item_graph_hints_payload(item)
        if payload is None or not get_settings().graph_projection_enabled:
            return
        repo = getattr(self.context, "graph_projection_event_repo", None)
        if repo is None:
            return
        try:
            await repo.append(
                GraphProjectionEvent(
                    event_type="memory.source_intelligence.graph_hints.approved",
                    aggregate_type="memory",
                    aggregate_id=payload["memory_id"],
                    payload=payload,
                    source="persona_factory",
                )
            )
        except Exception:
            # Graph projection is a derived view; item approval remains the source of truth.
            return

    async def _load_memories(self, source_memory_ids: list[str]) -> list[MemoryRecord]:
        memories: list[MemoryRecord] = []
        for memory_id in list(dict.fromkeys(source_memory_ids)):
            memory = await self.context.memory_repo.get(memory_id)
            if memory is None:
                raise PersonaDistillationError(f"Source memory '{memory_id}' not found.")
            memories.append(memory)
        return memories

    def _validate_distillation_scope(
            self,
            memories: list[MemoryRecord],
            *,
            distillation_mode: PersonaDistillationMode,
    ) -> None:
        settings = get_settings()
        source_count = len(memories)
        if source_count > settings.persona_factory_max_source_memories_per_run:
            raise PersonaDistillationError(
                "Too many source memories selected for one persona distillation run. "
                f"Selected {source_count}; maximum is "
                f"{settings.persona_factory_max_source_memories_per_run}. "
                "Split the source material into multiple distillation runs."
            )
        document_ids = {
            metadata["document_id"]
            for memory in memories
            if isinstance((metadata := memory.metadata), dict) and isinstance(metadata.get("document_id"), str)
        }
        if len(document_ids) > settings.persona_factory_max_documents_per_run:
            raise PersonaDistillationError(
                "Too many documents selected for one persona distillation run. "
                f"Selected {len(document_ids)} documents; maximum is "
                f"{settings.persona_factory_max_documents_per_run}. "
                "Process large corpora in batches and synthesize a later persona version."
            )
        character_count = sum(len(memory.content or "") for memory in memories)
        if character_count > settings.persona_factory_max_source_characters_per_run:
            raise PersonaDistillationError(
                "Too much source text selected for one persona distillation run. "
                f"Selected {character_count} characters; maximum is "
                f"{settings.persona_factory_max_source_characters_per_run}. "
                "Reduce the selected chunks or run batch distillation."
            )
        if distillation_mode not in LLM_BACKED_DISTILLATION_MODES:
            return
        if source_count > settings.persona_factory_llm_max_source_memories_per_run:
            raise PersonaDistillationError(
                "Too many source memories selected for one LLM persona distillation run. "
                f"Selected {source_count}; maximum is "
                f"{settings.persona_factory_llm_max_source_memories_per_run}. "
                "Use deterministic mode, reduce the selected chunks, or run batch distillation."
            )
        if character_count > settings.persona_factory_llm_max_source_characters_per_run:
            raise PersonaDistillationError(
                "Too much source text selected for one LLM persona distillation run. "
                f"Selected {character_count} characters; maximum is "
                f"{settings.persona_factory_llm_max_source_characters_per_run}. "
                "Use deterministic mode, reduce the selected chunks, or run batch distillation."
            )
        estimated_tokens = self._estimate_distillation_source_tokens(memories)
        if estimated_tokens > settings.persona_factory_llm_max_source_tokens_per_run:
            raise PersonaDistillationError(
                "Too many estimated source tokens selected for one LLM persona distillation run. "
                f"Estimated {estimated_tokens} tokens; maximum is "
                f"{settings.persona_factory_llm_max_source_tokens_per_run}. "
                "Use deterministic mode, reduce the selected chunks, or run batch distillation."
            )

    @staticmethod
    def _estimate_distillation_source_tokens(memories: list[MemoryRecord]) -> int:
        character_count = sum(len(memory.content or "") for memory in memories)
        return max(1, (character_count + 3) // 4)

    async def _feedback_source_memory(
            self,
            *,
            persona: PersonaDefinition,
            title: str | None,
            content: str,
            item_type: PersonaDistillationItemType,
            memory_layer: PersonaMemoryLayer,
            feedback_type: str,
            source_memory_id: str | None,
            accepted_edit_of_item_id: str | None,
            source_conversation_id: str | None,
            source_message_id: str | None,
            source_run_id: str | None,
            metadata: dict[str, Any],
            current_user: UserDefinition,
    ) -> MemoryRecord:
        if source_memory_id:
            existing = await self.context.memory_repo.get(source_memory_id)
            if existing is None:
                raise PersonaDistillationError(f"Feedback source memory '{source_memory_id}' not found.")
            return existing
        scope = MemoryScope.WORKSPACE.value if persona.workspace_id else MemoryScope.USER.value
        return await MemoryService(self.context).create_memory(
            {
                "scope": scope,
                "created_by_user_id": current_user.id,
                "workspace_id": persona.workspace_id,
                "content": content,
                "summary": title or self._title_from_content(content),
                "tags": [
                    "persona-feedback",
                    f"persona:{persona.slug}",
                    f"feedback:{feedback_type}",
                    f"item_type:{item_type.value}",
                    f"memory_layer:{memory_layer.value}",
                ],
                "source": "persona_feedback",
                "memory_type": self._memory_type_for_layer(memory_layer.value),
                "importance": 60,
                "source_conversation_id": source_conversation_id,
                "source_execution_id": source_run_id,
                "metadata": {
                    **metadata,
                    "persona_id": persona.id,
                    "persona_slug": persona.slug,
                    "feedback_type": feedback_type,
                    "accepted_edit_of_item_id": accepted_edit_of_item_id,
                    "source_message_id": source_message_id,
                    "source_run_id": source_run_id,
                    "candidate_only": True,
                    "review_status": PersonaDistillationItemReviewStatus.NEEDS_REVIEW.value,
                    "requires_review_before_publish": True,
                },
            },
            confirmed=True,
            current_user=current_user,
            trusted_actor=True,
        )

    async def _feedback_draft_package(self, *, persona: PersonaDefinition) -> dict[str, Any]:
        if persona.current_version_id:
            current_version = await self.context.persona_version_repo.get(persona.current_version_id,
                                                                          include_deleted=True)
            if current_version is not None and isinstance(current_version.package, dict):
                package = dict(current_version.package)
                package.setdefault("provenance", {})["continuous_learning_candidate"] = True
                return package
        return {
            "schema_version": 1,
            "identity": {
                "kind": "persona",
                "slug": persona.slug,
                "display_name": persona.name,
                "persona_type": DEFAULT_GOVERNANCE_LABELS["persona_type"],
            },
            "persona": {
                "summary": persona.description or f"Reusable persona package for {persona.name}.",
                "communication_style": ["source-grounded", "concise"],
                "preferences": [],
                "escalation_style": "Escalate uncertainty, missing evidence, and high-risk actions for human review.",
                "response_style": "source-grounded and concise",
            },
            "governance": dict(DEFAULT_GOVERNANCE_LABELS),
            "knowledge": [],
            "decision_patterns": [],
            "workflows": [],
            "tools": [],
            "guardrails": self._default_governance_guardrails(dict(DEFAULT_GOVERNANCE_LABELS)),
            "examples": [],
            "memory_layers": {layer.value: [] for layer in PersonaMemoryLayer},
            "runtime": {
                "default_agent_name": persona.name,
                "default_workflow_id": None,
                "invocation_names": [persona.slug, persona.name],
                "product_concept": "persona",
                "internal_package_type": "persona",
            },
            "provenance": {
                "source_ids": [],
                "source_memory_ids": [],
                "generated_at": utc_now().isoformat(),
                "strategy": "continuous-learning-feedback-v1",
                "continuous_learning_candidate": True,
            },
        }

    async def _ensure_memory_sources(
            self,
            *,
            persona: PersonaDefinition,
            memories: list[MemoryRecord],
    ) -> list[PersonaSource]:
        existing = await self.context.persona_source_repo.list_by_persona(persona.id)
        existing_by_memory = {
            item.source_id: item
            for item in existing
            if item.source_type == PersonaSourceType.MEMORY and item.source_id
        }
        sources: list[PersonaSource] = []
        for memory in memories:
            existing_source = existing_by_memory.get(memory.id)
            if existing_source is not None:
                sources.append(existing_source)
                continue
            metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
            source = await self.context.persona_source_repo.create(
                PersonaSource(
                    persona_id=persona.id,
                    source_type=PersonaSourceType.MEMORY,
                    source_id=memory.id,
                    filename=metadata.get("filename") if isinstance(metadata.get("filename"), str) else None,
                    content_sha256=(
                        metadata.get("content_sha256")
                        if isinstance(metadata.get("content_sha256"), str)
                        else None
                    ),
                    storage_uri=metadata.get("storage_uri") if isinstance(metadata.get("storage_uri"), str) else None,
                    metadata={
                        "memory_type": memory.memory_type.value if memory.memory_type else None,
                        "summary": memory.summary,
                        "document_id": metadata.get("document_id"),
                        "chunk_index": metadata.get("chunk_index"),
                        "source": memory.source,
                    },
                )
            )
            sources.append(source)
        return sources

    async def _draft_package(
            self,
            *,
            persona: PersonaDefinition,
            memories: list[MemoryRecord],
            sources: list[PersonaSource],
            run_id: str,
            governance_labels: dict[str, Any] | None,
    ) -> dict[str, Any]:
        source_by_memory = {source.source_id: source for source in sources}
        memory_layers: dict[str, list[dict[str, Any]]] = {
            "semantic": [],
            "episodic": [],
            "procedural": [],
            "social": [],
        }
        knowledge: list[dict[str, Any]] = []
        decision_patterns: list[dict[str, Any]] = []
        workflows: list[dict[str, Any]] = []
        guardrails: list[dict[str, Any]] = []
        examples: list[dict[str, Any]] = []
        preferences: list[str] = []

        for memory in memories:
            layer = self._memory_layer(memory)
            source_ref = self._source_ref(memory, source_by_memory.get(memory.id))
            item = {
                "title": memory.summary or self._title_from_content(memory.content),
                "content": memory.content,
                "confidence": self._confidence(memory),
                "source_refs": [source_ref],
            }
            memory_layers[layer].append(item)
            knowledge.append(item)
            if layer == "procedural":
                workflows.append(
                    {
                        "name": item["title"],
                        "description": item["content"],
                        "confidence": item["confidence"],
                        "source_refs": item["source_refs"],
                    }
                )
            if memory.memory_type == MemoryType.DECISION or "decision" in memory.tags:
                decision_patterns.append(item)
            if memory.memory_type == MemoryType.PREFERENCE:
                preferences.append(memory.summary or self._title_from_content(memory.content))
            if self._looks_like_guardrail(memory.content):
                guardrails.append(item)
            if self._looks_like_example(memory.content):
                examples.append(item)

        tools = await self._suggest_tools(memories)
        governance = self._normalize_governance_labels(governance_labels)
        guardrails = [*self._default_governance_guardrails(governance), *guardrails]
        return {
            "schema_version": 1,
            "identity": {
                "kind": "persona",
                "slug": persona.slug,
                "display_name": persona.name,
                "persona_type": governance["persona_type"],
            },
            "persona": {
                "summary": persona.description or f"Reusable persona package for {persona.name}.",
                "communication_style": self._communication_style(memories),
                "preferences": list(dict.fromkeys(preferences))[:10],
                "escalation_style": self._escalation_style(memories),
                "response_style": "source-grounded and concise",
            },
            "governance": governance,
            "knowledge": knowledge[:50],
            "decision_patterns": decision_patterns[:30],
            "workflows": workflows[:30],
            "tools": tools,
            "guardrails": guardrails[:20],
            "examples": examples[:20],
            "memory_layers": memory_layers,
            "runtime": {
                "default_agent_name": persona.name,
                "default_workflow_id": None,
                "invocation_names": [persona.slug, persona.name],
                "product_concept": "persona",
                "internal_package_type": "persona",
            },
            "provenance": {
                "source_ids": [item.id for item in sources],
                "source_memory_ids": [item.id for item in memories],
                "distillation_run_id": run_id,
                "generated_at": utc_now().isoformat(),
                "strategy": "deterministic-v1",
            },
        }

    def _package_from_items(
            self,
            *,
            persona: PersonaDefinition,
            run: PersonaDistillationRun,
            items: list[PersonaDistillationItem],
            all_items: list[PersonaDistillationItem],
    ) -> dict[str, Any]:
        previous_package = run.output_package if isinstance(run.output_package, dict) else {}
        governance = self._normalize_governance_labels(previous_package.get("governance"))
        memory_layers: dict[str, list[dict[str, Any]]] = {
            layer.value: []
            for layer in PersonaMemoryLayer
        }
        knowledge: list[dict[str, Any]] = []
        decision_patterns: list[dict[str, Any]] = []
        workflows: list[dict[str, Any]] = []
        tools: list[dict[str, Any]] = []
        guardrails: list[dict[str, Any]] = []
        examples: list[dict[str, Any]] = []
        preferences: list[str] = []

        for item in items:
            entry = self._package_entry_from_item(item)
            memory_layers.setdefault(item.memory_layer.value, []).append(entry)
            if item.item_type == PersonaDistillationItemType.DECISION_PATTERN:
                decision_patterns.append(entry)
                knowledge.append(entry)
            elif item.item_type in {
                PersonaDistillationItemType.PROCEDURE,
                PersonaDistillationItemType.WORKFLOW,
            }:
                workflows.append(
                    {
                        "name": item.title,
                        "description": item.content,
                        "confidence": item.confidence,
                        "source_refs": entry["source_refs"],
                        "distillation_item_id": item.id,
                        "review_status": item.review_status.value,
                    }
                )
                knowledge.append(entry)
            elif item.item_type == PersonaDistillationItemType.WRITING_STYLE:
                preferences.append(item.title)
                preferences.append(item.content)
            elif item.item_type == PersonaDistillationItemType.TOOL_USAGE:
                tools.append(
                    {
                        "name": item.title,
                        "tool_id": item.structured_payload.get("tool_id") if isinstance(item.structured_payload,
                                                                                        dict) else None,
                        "granted": False,
                        "confidence": item.confidence,
                        "rationale": item.content,
                        "source_refs": entry["source_refs"],
                        "distillation_item_id": item.id,
                        "review_status": item.review_status.value,
                    }
                )
            elif item.item_type == PersonaDistillationItemType.GUARDRAIL:
                guardrails.append(entry)
            elif item.item_type == PersonaDistillationItemType.EXAMPLE:
                examples.append(entry)
            elif item.item_type != PersonaDistillationItemType.SOURCE_REFERENCE:
                knowledge.append(entry)

        guardrails = [*self._default_governance_guardrails(governance), *guardrails]
        source_memory_ids = list(dict.fromkeys(
            item.source_memory_id for item in items if item.source_memory_id
        ))
        source_ids = list(dict.fromkeys(run.input_source_ids))
        return {
            "schema_version": 1,
            "identity": {
                "kind": "persona",
                "slug": persona.slug,
                "display_name": persona.name,
                "persona_type": governance["persona_type"],
            },
            "persona": {
                "summary": persona.description or f"Reusable persona package for {persona.name}.",
                "communication_style": self._communication_style_from_items(items),
                "preferences": list(dict.fromkeys(preferences))[:10],
                "escalation_style": self._escalation_style_from_items(items),
                "response_style": "source-grounded and concise",
            },
            "governance": governance,
            "knowledge": knowledge[:50],
            "decision_patterns": decision_patterns[:30],
            "workflows": workflows[:30],
            "tools": tools[:30],
            "guardrails": guardrails[:20],
            "examples": examples[:20],
            "memory_layers": memory_layers,
            "runtime": {
                "default_agent_name": persona.name,
                "default_workflow_id": previous_package.get("runtime", {}).get("default_workflow_id")
                if isinstance(previous_package.get("runtime"), dict)
                else None,
                "invocation_names": [persona.slug, persona.name],
                "product_concept": "persona",
                "internal_package_type": "persona",
            },
            "provenance": {
                "source_ids": source_ids,
                "source_memory_ids": source_memory_ids,
                "distillation_run_id": run.id,
                "generated_at": utc_now().isoformat(),
                "strategy": "item-synthesis-v1",
                "package_synthesis_mode": "reviewed_items",
                "distillation_item_ids": [item.id for item in items],
                "approved_item_ids": [
                    item.id for item in items
                    if item.review_status == PersonaDistillationItemReviewStatus.APPROVED
                ],
                "llm_polishing_used": False,
                "excluded_item_ids": [
                    item.id for item in all_items
                    if item.review_status in {
                        PersonaDistillationItemReviewStatus.REJECTED,
                        PersonaDistillationItemReviewStatus.SUPERSEDED,
                    }
                ],
                "needs_review_count": sum(1 for item in items if item.needs_review),
            },
        }

    async def _llm_polish_package(
            self,
            *,
            persona: PersonaDefinition,
            run: PersonaDistillationRun,
            base_package: dict[str, Any],
            approved_items: list[PersonaDistillationItem],
            model_profile_id: str | None,
    ) -> dict[str, Any]:
        profile = await self._resolve_package_polishing_profile(run, model_profile_id=model_profile_id)
        response = await self._generate_structured_with_profile(
            profile=profile,
            schema_name=PERSONA_PACKAGE_POLISH_SCHEMA_NAME,
            schema=_LLMPackagePolishPayload.model_json_schema(),
            system=(
                "You polish Agency Persona packages. Return only schema-valid JSON. "
                "Do not add facts, capabilities, tools, workflows, memories, examples, or guardrails. "
                "You may only clarify persona summary/style wording using approved item evidence."
            ),
            prompt=json.dumps(
                {
                    "persona": {
                        "id": persona.id,
                        "slug": persona.slug,
                        "name": persona.name,
                        "description": persona.description,
                    },
                    "run_id": run.id,
                    "base_package": base_package,
                    "approved_items": [
                        {
                            "id": item.id,
                            "item_type": item.item_type.value,
                            "memory_layer": item.memory_layer.value,
                            "title": item.title,
                            "content": item.content,
                            "confidence": item.confidence,
                        }
                        for item in approved_items
                    ],
                    "rules": [
                        "Preserve all distillation_item_id values.",
                        "Do not add sections or entries that are not present in base_package.",
                        "Do not change source-backed package sections; only polish the persona wording.",
                        "Every claim must be supported by approved_items.",
                    ],
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
        )
        try:
            polished = _LLMPackagePolishPayload.model_validate(response)
        except ValueError as exc:
            raise PersonaDistillationError(f"Persona package polishing output failed schema validation: {exc}") from exc

        package = self._validated_polished_package(
            base_package=base_package,
            polished_package=polished.package,
            approved_items=approved_items,
        )
        package.setdefault("provenance", {}).update(
            {
                "package_synthesis_mode": "llm_polished",
                "approved_item_ids": [item.id for item in approved_items],
                "llm_polishing_used": True,
                "polishing_model_profile_id": profile.id,
                "polishing_model": profile.model,
                "polishing_model_provider": profile.provider,
                "polishing_prompt_version": PERSONA_PACKAGE_POLISH_PROMPT_VERSION,
                "polishing_summary": polished.summary,
            }
        )
        return package

    async def _resolve_package_polishing_profile(
            self,
            run: PersonaDistillationRun,
            *,
            model_profile_id: str | None,
    ) -> ModelProfileDefinition:
        if model_profile_id:
            return await self._resolve_required_model_profile(model_profile_id)
        profile = await self._resolve_run_model_profile(run)
        if profile is None:
            raise PersonaDistillationError(
                "LLM package polishing requires a model profile. Provide llm_polishing_model_profile_id."
            )
        return profile

    def _validated_polished_package(
            self,
            *,
            base_package: dict[str, Any],
            polished_package: dict[str, Any],
            approved_items: list[PersonaDistillationItem],
    ) -> dict[str, Any]:
        self._validate_package(polished_package)
        for section in PACKAGE_POLISH_SUPPORTED_SECTIONS:
            if self._canonical_package_value(polished_package.get(section)) != self._canonical_package_value(
                    base_package.get(section)):
                raise PersonaDistillationError(
                    f"Persona package polishing cannot modify source-backed section '{section}'."
                )
        for section in ("identity", "governance", "runtime"):
            if self._canonical_package_value(polished_package.get(section)) != self._canonical_package_value(
                    base_package.get(section)):
                raise PersonaDistillationError(
                    f"Persona package polishing cannot modify package section '{section}'."
                )
        unsupported_terms = self._unsupported_polished_persona_terms(
            polished_persona=polished_package.get("persona"),
            approved_items=approved_items,
            base_package=base_package,
        )
        if unsupported_terms:
            raise PersonaDistillationError(
                "Persona package polishing introduced unsupported terms: "
                + ", ".join(unsupported_terms[:8])
            )
        package = json.loads(json.dumps(base_package, ensure_ascii=True))
        package["persona"] = polished_package.get("persona")
        package["provenance"] = dict(base_package.get("provenance") or {})
        self._validate_package(package)
        return package

    @staticmethod
    def _canonical_package_value(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=True, sort_keys=True))

    @classmethod
    def _unsupported_polished_persona_terms(
            cls,
            *,
            polished_persona: Any,
            approved_items: list[PersonaDistillationItem],
            base_package: dict[str, Any],
    ) -> list[str]:
        if not isinstance(polished_persona, dict):
            return ["persona_section_missing"]
        source_text = " ".join(
            [
                str(base_package.get("identity", {}).get("display_name") if isinstance(base_package.get("identity"),
                                                                                       dict) else ""),
                json.dumps(base_package.get("persona") or {}, ensure_ascii=True, sort_keys=True),
                *[
                    f"{item.title} {item.content}"
                    for item in approved_items
                ],
            ]
        )
        allowed = cls._claim_terms(source_text).union(PACKAGE_POLISH_ALLOWED_GENERIC_TERMS)
        polished_text = json.dumps(polished_persona, ensure_ascii=True, sort_keys=True)
        return sorted(term for term in cls._claim_terms(polished_text) if term not in allowed)

    @staticmethod
    def _claim_terms(value: str) -> set[str]:
        terms: set[str] = set()
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
            if len(token) <= 4:
                continue
            if token.endswith("ies") and len(token) > 5:
                token = f"{token[:-3]}y"
            elif token.endswith("s") and len(token) > 5:
                token = token[:-1]
            terms.add(token)
        return terms

    @staticmethod
    def _package_entry_from_item(item: PersonaDistillationItem) -> dict[str, Any]:
        source_ref = item.structured_payload.get("source_ref") if isinstance(item.structured_payload, dict) else None
        source_refs = item.structured_payload.get("source_refs") if isinstance(item.structured_payload, dict) else None
        return {
            "title": item.title,
            "content": item.content,
            "confidence": item.confidence,
            "source_refs": (
                source_refs
                if isinstance(source_refs, list)
                else [source_ref] if isinstance(source_ref, dict) else []
            ),
            "distillation_item_id": item.id,
            "item_type": item.item_type.value,
            "memory_layer": item.memory_layer.value,
            "review_status": item.review_status.value,
            "needs_review": item.needs_review,
        }

    @staticmethod
    def _package_graph_projection_payload(package: dict[str, Any]) -> dict[str, Any]:
        def section(name: str) -> list[Any]:
            value = package.get(name)
            return value if isinstance(value, list) else []

        def item_id(entry: dict[str, Any], prefix: str) -> str:
            explicit = entry.get("id") or entry.get(f"{prefix}_id") or entry.get("tool_id")
            if explicit:
                return str(explicit)
            source_id = entry.get("distillation_item_id") or entry.get("name") or entry.get("title")
            return f"persona_{prefix}:{source_id}" if source_id else ""

        workflows: list[dict[str, Any]] = []
        for entry in section("workflows"):
            if not isinstance(entry, dict):
                continue
            workflow_id = item_id(entry, "workflow")
            if workflow_id:
                workflows.append(
                    {
                        "id": workflow_id,
                        "name": entry.get("name") or entry.get("title") or workflow_id,
                        "distillation_item_id": entry.get("distillation_item_id"),
                        "confidence": entry.get("confidence"),
                    }
                )

        tools: list[dict[str, Any]] = []
        for entry in section("tools"):
            if not isinstance(entry, dict):
                continue
            tool_id = item_id(entry, "tool")
            if tool_id:
                tools.append(
                    {
                        "id": tool_id,
                        "name": entry.get("name") or tool_id,
                        "granted": entry.get("granted"),
                        "distillation_item_id": entry.get("distillation_item_id"),
                        "confidence": entry.get("confidence"),
                    }
                )

        artifacts: list[dict[str, Any]] = []
        for entry in section("examples"):
            if not isinstance(entry, dict):
                continue
            artifact_id = item_id(entry, "artifact")
            if artifact_id:
                artifacts.append(
                    {
                        "id": artifact_id,
                        "name": entry.get("title") or entry.get("name") or artifact_id,
                        "artifact_type": "example",
                        "distillation_item_id": entry.get("distillation_item_id"),
                        "confidence": entry.get("confidence"),
                    }
                )

        return {
            "source_memory_ids": package.get("provenance", {}).get("source_memory_ids", [])
            if isinstance(package.get("provenance"), dict)
            else [],
            "workflows": workflows,
            "tools": tools,
            "artifacts": artifacts,
        }

    @staticmethod
    def _approved_item_graph_hints_payload(item: PersonaDistillationItem) -> dict[str, Any] | None:
        payload = item.structured_payload if isinstance(item.structured_payload, dict) else {}
        entities = payload.get("suggested_graph_entities") if isinstance(payload.get("suggested_graph_entities"),
                                                                         list) else []
        relationships = (
            payload.get("suggested_graph_relationships")
            if isinstance(payload.get("suggested_graph_relationships"), list)
            else []
        )
        entities = [entry for entry in entities if isinstance(entry, dict)]
        relationships = [entry for entry in relationships if isinstance(entry, dict)]
        if not entities and not relationships:
            return None
        if item.review_status != PersonaDistillationItemReviewStatus.APPROVED:
            return None
        source_ref = payload.get("source_ref") if isinstance(payload.get("source_ref"), dict) else {}
        memory_id = item.source_memory_id or source_ref.get("memory_id")
        if not memory_id:
            return None
        return {
            "memory_id": memory_id,
            "document_id": source_ref.get("document_id"),
            "filename": source_ref.get("filename"),
            "chunk_index": source_ref.get("chunk_index"),
            "persona_id": item.persona_id,
            "run_id": item.run_id,
            "distillation_item_id": item.id,
            "item_type": item.item_type.value,
            "memory_layer": item.memory_layer.value,
            "graph_hint_source": "persona_llm_distillation",
            "review": {
                "review_status": "approved",
                "reviewed_at": utc_now().isoformat(),
                "reviewed_by_user_id": None,
            },
            "entities": entities,
            "relationships": relationships,
        }

    @staticmethod
    def _normalization_key(item: PersonaDistillationItem) -> tuple[str, str, str]:
        content = PersonaFactoryService._normalized_text(item.content)
        return item.item_type.value, item.memory_layer.value, content

    @staticmethod
    def _conflict_topic_key(item: PersonaDistillationItem) -> tuple[str, str, str]:
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        explicit_topic = metadata.get("topic")
        if isinstance(explicit_topic, str) and explicit_topic.strip():
            topic = explicit_topic
        else:
            topic = item.title.split(":", 1)[-1]
        words = PersonaFactoryService._normalized_text(topic).split()
        return item.item_type.value, item.memory_layer.value, " ".join(words[:8])

    @staticmethod
    def _normalized_text(value: str) -> str:
        import re

        lowered = str(value or "").lower()
        lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
        return " ".join(lowered.split())

    @staticmethod
    def _normalization_preference_key(item: PersonaDistillationItem) -> tuple[int, int, float, str]:
        status_rank = {
            PersonaDistillationItemReviewStatus.APPROVED: 0,
            PersonaDistillationItemReviewStatus.DRAFT: 1,
            PersonaDistillationItemReviewStatus.NEEDS_REVIEW: 2,
        }.get(item.review_status, 3)
        return status_rank, int(item.needs_review), -item.confidence, item.created_at.isoformat()

    def _merge_duplicate_items(
            self,
            primary: PersonaDistillationItem,
            duplicates: list[PersonaDistillationItem],
    ) -> PersonaDistillationItem:
        group = [primary, *duplicates]
        all_source_refs = self._merged_source_refs(group)
        structured_payload = dict(primary.structured_payload or {})
        if all_source_refs:
            structured_payload["source_refs"] = all_source_refs
            structured_payload["source_ref"] = all_source_refs[0]
        metadata = {
            **(primary.metadata or {}),
            "normalized_from_item_ids": [item.id for item in group],
            "merged_item_ids": [item.id for item in duplicates],
            "normalization_strategy": "deterministic-normalization-v1",
        }
        confidence = max(item.confidence for item in group)
        review_status = self._merged_review_status(group)
        needs_review = (
                review_status == PersonaDistillationItemReviewStatus.NEEDS_REVIEW
                or any(item.needs_review for item in group)
        )
        return primary.model_copy(
            update={
                "title": primary.title,
                "content": primary.content,
                "structured_payload": structured_payload,
                "confidence": confidence,
                "needs_review": needs_review,
                "review_status": review_status,
                "metadata": metadata,
            }
        )

    @staticmethod
    def _merged_review_status(
            group: list[PersonaDistillationItem],
    ) -> PersonaDistillationItemReviewStatus:
        if all(item.review_status == PersonaDistillationItemReviewStatus.APPROVED for item in group):
            return PersonaDistillationItemReviewStatus.APPROVED
        if any(item.needs_review or item.review_status == PersonaDistillationItemReviewStatus.NEEDS_REVIEW for item in
               group):
            return PersonaDistillationItemReviewStatus.NEEDS_REVIEW
        return PersonaDistillationItemReviewStatus.DRAFT

    def _merged_source_refs(self, group: list[PersonaDistillationItem]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any, Any]] = set()
        for item in group:
            for ref in self._item_source_refs(item):
                key = (ref.get("source_id"), ref.get("memory_id"), ref.get("chunk_index"))
                if key in seen:
                    continue
                seen.add(key)
                refs.append(ref)
        return refs

    @staticmethod
    def _item_source_refs(item: PersonaDistillationItem) -> list[dict[str, Any]]:
        payload = item.structured_payload if isinstance(item.structured_payload, dict) else {}
        refs = payload.get("source_refs")
        if isinstance(refs, list):
            return [ref for ref in refs if isinstance(ref, dict)]
        ref = payload.get("source_ref")
        return [ref] if isinstance(ref, dict) else []

    @classmethod
    def _serialize_review_item(cls, item: PersonaDistillationItem) -> dict[str, Any]:
        payload = item.model_dump(mode="json")
        payload["review_metadata"] = cls._item_review_metadata(item)
        return payload

    @classmethod
    def _item_review_metadata(cls, item: PersonaDistillationItem) -> dict[str, Any]:
        structured_payload = item.structured_payload if isinstance(item.structured_payload, dict) else {}
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        provenance = (
            structured_payload.get("provenance")
            if isinstance(structured_payload.get("provenance"), dict)
            else {}
        )
        evidence = (
            provenance.get("evidence")
            if isinstance(provenance.get("evidence"), dict)
            else structured_payload.get("evidence")
            if isinstance(structured_payload.get("evidence"), dict)
            else {}
        )
        generated_by = cls._item_extraction_source(item)
        distillers = cls._item_distillers(item)
        review_flags = cls._item_review_flags(item)
        review_reasons = cls._string_list(metadata.get("review_reasons"))
        review_reasons.extend(cls._string_list(provenance.get("review_reasons")))
        review_reasons.extend(
            reason for reason in review_flags
            if reason in {"weak_source_evidence", "missing_source_span", "evidence_not_verified", "material_conflict"}
        )
        review_reasons = list(dict.fromkeys(review_reasons))
        source_evidence = (
                structured_payload.get("source_evidence")
                or metadata.get("source_evidence")
                or evidence.get("text")
        )
        source_span = (
                structured_payload.get("source_span")
                or metadata.get("source_span")
                or evidence.get("matched_span")
                or evidence.get("provided_span")
        )
        model = {
            "provider": provenance.get("model_provider") or metadata.get("model_provider"),
            "model": provenance.get("model") or metadata.get("model"),
            "model_profile_id": provenance.get("model_profile_id") or metadata.get("model_profile_id"),
            "prompt_version": provenance.get("prompt_version") or metadata.get("prompt_version"),
        }
        merge_payload = (
            structured_payload.get("hybrid_merge")
            if isinstance(structured_payload.get("hybrid_merge"), dict)
            else {}
        )
        merge = {
            "strategy": merge_payload.get("strategy"),
            "merge_strategy": metadata.get("merge_strategy") or merge_payload.get("merge_strategy"),
            "merged_from_item_ids": cls._string_list(metadata.get("merged_from_item_ids")),
            "merged_from_candidate_ids": cls._string_list(metadata.get("merged_from_candidate_ids")),
            "merged_from_distillers": cls._string_list(metadata.get("merged_from_distillers"))
                                      or cls._string_list(merge_payload.get("merged_from_distillers")),
            "semantic_duplicate_group_id": (
                    metadata.get("semantic_duplicate_group_id")
                    or structured_payload.get("semantic_duplicate_group_id")
            ),
            "conflict_group_id": metadata.get("conflict_group_id") or structured_payload.get("conflict_group_id"),
            "conflicting_candidate_ids": cls._string_list(metadata.get("conflicting_candidate_ids"))
                                         or cls._string_list(merge_payload.get("conflicting_candidate_ids")),
            "source_generation_modes": cls._string_list(metadata.get("source_generation_modes"))
                                       or cls._string_list(merge_payload.get("generated_by")),
        }
        return {
            "extraction_source": generated_by,
            "distiller": distillers[0] if distillers else None,
            "distillers": distillers,
            "distiller_version": metadata.get("distiller_version") or structured_payload.get("distiller_version"),
            "review_flags": review_flags,
            "review_reasons": review_reasons,
            "source_evidence": source_evidence if isinstance(source_evidence, str) else None,
            "source_span": source_span if isinstance(source_span, dict) else None,
            "evidence": {
                "text": source_evidence if isinstance(source_evidence, str) else None,
                "hash": evidence.get("hash") or metadata.get("source_evidence_hash"),
                "verified": evidence.get("verified"),
                "match_method": evidence.get("match_method"),
                "match_score": evidence.get("match_score"),
                "verification_reason": evidence.get("verification_reason"),
            },
            "model": model,
            "merge": merge,
            "reviewer_actions": cls._reviewer_actions_for_item(generated_by, review_flags, merge),
        }

    @classmethod
    def _item_extraction_source(cls, item: PersonaDistillationItem) -> str:
        structured_payload = item.structured_payload if isinstance(item.structured_payload, dict) else {}
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        raw = str(metadata.get("generated_by") or structured_payload.get("generated_by") or "").strip().lower()
        if raw in PERSONA_REVIEW_EXTRACTION_SOURCES:
            return raw
        if structured_payload.get("extractor") == HYBRID_DISTILLATION_EXTRACTOR:
            return "hybrid"
        if metadata.get("model_provider") or metadata.get("prompt_version"):
            return "llm"
        return "deterministic"

    @classmethod
    def _item_distillers(cls, item: PersonaDistillationItem) -> list[str]:
        structured_payload = item.structured_payload if isinstance(item.structured_payload, dict) else {}
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        values = [
            *cls._string_list(metadata.get("merged_from_distillers")),
            *cls._string_list(structured_payload.get("merged_from_distillers")),
        ]
        hybrid_merge = structured_payload.get("hybrid_merge")
        if isinstance(hybrid_merge, dict):
            values.extend(cls._string_list(hybrid_merge.get("merged_from_distillers")))
        for value in (metadata.get("distiller"), structured_payload.get("distiller")):
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
        return list(dict.fromkeys(values))

    @classmethod
    def _item_review_flags(cls, item: PersonaDistillationItem) -> list[str]:
        structured_payload = item.structured_payload if isinstance(item.structured_payload, dict) else {}
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        return list(dict.fromkeys([
            *cls._string_list(structured_payload.get("review_flags")),
            *cls._string_list(metadata.get("review_flags")),
            *cls._string_list(metadata.get("review_reasons")),
        ]))

    @staticmethod
    def _reviewer_actions_for_item(
            extraction_source: str,
            review_flags: list[str],
            merge: dict[str, Any],
    ) -> list[str]:
        actions = ["approve", "reject", "mark_evidence_insufficient"]
        if merge.get("conflict_group_id") or "material_conflict" in review_flags:
            actions.extend(["prefer_llm", "prefer_deterministic", "merge_manually"])
        elif extraction_source == "hybrid":
            actions.extend(["merge_manually"])
        return list(dict.fromkeys(actions))

    @classmethod
    def _select_review_action_items(
            cls,
            items: list[PersonaDistillationItem],
            *,
            item_ids: list[str],
            conflict_group_id: str | None,
    ) -> list[PersonaDistillationItem]:
        requested_ids = {
            item_id.strip()
            for item_id in item_ids
            if isinstance(item_id, str) and item_id.strip()
        }
        normalized_conflict_group_id = (
            conflict_group_id.strip()
            if isinstance(conflict_group_id, str) and conflict_group_id.strip()
            else None
        )
        selected: list[PersonaDistillationItem] = []
        for item in items:
            review_metadata = cls._item_review_metadata(item)
            matches_id = not requested_ids or item.id in requested_ids
            matches_conflict = (
                    normalized_conflict_group_id is None
                    or review_metadata["merge"].get("conflict_group_id") == normalized_conflict_group_id
            )
            if matches_id and matches_conflict:
                selected.append(item)
        return selected

    async def _mark_item_evidence_insufficient(
            self,
            item: PersonaDistillationItem,
            *,
            reason: str | None,
    ) -> PersonaDistillationItem:
        structured_payload = dict(item.structured_payload or {})
        metadata = dict(item.metadata or {})
        flags = list(dict.fromkeys([
            *self._item_review_flags(item),
            "evidence_insufficient",
        ]))
        structured_payload["review_flags"] = flags
        metadata["review_flags"] = flags
        metadata["evidence_insufficient"] = {
            "reason": reason,
            "marked_at": utc_now().isoformat(),
        }
        return await self.update_item(
            item.id,
            {
                "structured_payload": structured_payload,
                "metadata": metadata,
                "review_status": PersonaDistillationItemReviewStatus.NEEDS_REVIEW.value,
                "needs_review": True,
            },
            emit_audit=False,
        )

    async def _mark_items_for_manual_merge(
            self,
            items: list[PersonaDistillationItem],
            *,
            reason: str | None,
            patch: dict[str, Any] | None,
    ) -> list[PersonaDistillationItem]:
        reviewed: list[PersonaDistillationItem] = []
        for index, item in enumerate(items):
            structured_payload = dict(item.structured_payload or {})
            metadata = dict(item.metadata or {})
            flags = list(dict.fromkeys([
                *self._item_review_flags(item),
                "manual_merge_requested",
            ]))
            structured_payload["review_flags"] = flags
            metadata["review_flags"] = flags
            metadata["manual_merge_requested"] = {
                "reason": reason,
                "marked_at": utc_now().isoformat(),
            }
            update_patch: dict[str, Any] = {
                "structured_payload": structured_payload,
                "metadata": metadata,
                "review_status": PersonaDistillationItemReviewStatus.NEEDS_REVIEW.value,
                "needs_review": True,
            }
            # Optional manual merge edits apply to the first selected item; the remaining items stay review-linked.
            if index == 0 and isinstance(patch, dict):
                update_patch.update(
                    {key: value for key, value in patch.items() if key in {"title", "content", "confidence"}})
            reviewed.append(await self.update_item(item.id, update_patch, emit_audit=False))
        return reviewed

    def _normalize_run_item_filters(
            self,
            *,
            source_key: str | None,
            item_type: str | None,
            memory_layer: str | None,
            review_status: str | None,
            needs_review: bool | None,
            min_confidence: float | None,
            max_confidence: float | None,
            extraction_source: str | None,
            distiller: str | None,
            review_flag: str | None,
            conflict_group_id: str | None,
            limit: int,
            offset: int,
    ) -> dict[str, Any]:
        normalized_item_type = self._optional_enum_value(
            item_type,
            PersonaDistillationItemType,
            "item_type",
        )
        normalized_memory_layer = self._optional_enum_value(
            memory_layer,
            PersonaMemoryLayer,
            "memory_layer",
        )
        normalized_review_status = self._optional_enum_value(
            review_status,
            PersonaDistillationItemReviewStatus,
            "review_status",
        )
        if min_confidence is not None and not 0 <= min_confidence <= 1:
            raise PersonaDistillationError("min_confidence must be between 0 and 1.")
        if max_confidence is not None and not 0 <= max_confidence <= 1:
            raise PersonaDistillationError("max_confidence must be between 0 and 1.")
        if min_confidence is not None and max_confidence is not None and min_confidence > max_confidence:
            raise PersonaDistillationError("min_confidence cannot be greater than max_confidence.")
        if limit < 1 or limit > 250:
            raise PersonaDistillationError("limit must be between 1 and 250.")
        if offset < 0:
            raise PersonaDistillationError("offset must be greater than or equal to 0.")
        normalized_extraction_source = self._normalize_extraction_source(extraction_source)
        return {
            "source_key": source_key.strip() if isinstance(source_key, str) and source_key.strip() else None,
            "item_type": normalized_item_type,
            "memory_layer": normalized_memory_layer,
            "review_status": normalized_review_status,
            "needs_review": needs_review,
            "min_confidence": min_confidence,
            "max_confidence": max_confidence,
            "extraction_source": normalized_extraction_source,
            "distiller": distiller.strip() if isinstance(distiller, str) and distiller.strip() else None,
            "review_flag": review_flag.strip() if isinstance(review_flag, str) and review_flag.strip() else None,
            "conflict_group_id": (
                conflict_group_id.strip()
                if isinstance(conflict_group_id, str) and conflict_group_id.strip()
                else None
            ),
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _normalize_extraction_source(value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().lower().replace("-", "_")
        if normalized == "hybrid_merged":
            normalized = "hybrid"
        if normalized not in PERSONA_REVIEW_EXTRACTION_SOURCES:
            raise PersonaDistillationError(
                "Invalid persona distillation extraction_source "
                f"'{value}'. Allowed values: {', '.join(sorted(PERSONA_REVIEW_EXTRACTION_SOURCES))}."
            )
        return normalized

    @staticmethod
    def _optional_enum_value(value: str | None, enum_cls: Any, field_name: str) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().lower()
        allowed = {item.value for item in enum_cls}
        if normalized not in allowed:
            raise PersonaDistillationError(
                f"Invalid persona distillation item {field_name} '{value}'. "
                f"Allowed values: {', '.join(sorted(allowed))}."
            )
        return normalized

    @classmethod
    def _run_item_matches_filters(cls, item: PersonaDistillationItem, filters: dict[str, Any]) -> bool:
        review_metadata = cls._item_review_metadata(item)
        source_key = filters.get("source_key")
        if source_key and source_key not in cls._source_filter_keys(item):
            return False
        if filters.get("item_type") and item.item_type.value != filters["item_type"]:
            return False
        if filters.get("memory_layer") and item.memory_layer.value != filters["memory_layer"]:
            return False
        if filters.get("review_status") and item.review_status.value != filters["review_status"]:
            return False
        if filters.get("needs_review") is not None and item.needs_review is not filters["needs_review"]:
            return False
        min_confidence = filters.get("min_confidence")
        if min_confidence is not None and item.confidence < min_confidence:
            return False
        max_confidence = filters.get("max_confidence")
        if max_confidence is not None and item.confidence > max_confidence:
            return False
        if filters.get("extraction_source") and review_metadata["extraction_source"] != filters["extraction_source"]:
            return False
        if filters.get("distiller") and filters["distiller"] not in review_metadata["distillers"]:
            return False
        if filters.get("review_flag") and filters["review_flag"] not in review_metadata["review_flags"]:
            return False
        if filters.get("conflict_group_id") and review_metadata["merge"].get("conflict_group_id") != filters[
            "conflict_group_id"]:
            return False
        return True

    @classmethod
    def _source_filter_keys(cls, item: PersonaDistillationItem) -> set[str]:
        keys: set[str] = set()
        for source_ref in cls._source_map_refs(item):
            keys.add(cls._source_map_key(item, source_ref))
            for field in ("document_id", "source_id", "memory_id", "filename", "content_sha256"):
                value = source_ref.get(field)
                if isinstance(value, str) and value.strip():
                    keys.add(value.strip())
        if item.source_memory_id:
            keys.add(item.source_memory_id)
        return keys

    @classmethod
    def _run_item_counts(cls, items: list[PersonaDistillationItem]) -> dict[str, Any]:
        counts: dict[str, Any] = {
            "item_types": {},
            "memory_layers": {},
            "review_statuses": {},
            "source_keys": {},
            "extraction_sources": {},
            "distillers": {},
            "review_flags": {},
            "conflict_groups": {},
            "needs_review": sum(1 for item in items if item.needs_review),
            "ready": sum(1 for item in items if not item.needs_review),
        }
        for item in items:
            cls._increment_source_map_count(counts["item_types"], item.item_type.value)
            cls._increment_source_map_count(counts["memory_layers"], item.memory_layer.value)
            cls._increment_source_map_count(counts["review_statuses"], item.review_status.value)
            review_metadata = cls._item_review_metadata(item)
            cls._increment_source_map_count(counts["extraction_sources"], review_metadata["extraction_source"])
            for distiller in review_metadata["distillers"]:
                cls._increment_source_map_count(counts["distillers"], distiller)
            for flag in review_metadata["review_flags"]:
                cls._increment_source_map_count(counts["review_flags"], flag)
            conflict_group_id = review_metadata["merge"].get("conflict_group_id")
            if conflict_group_id:
                cls._increment_source_map_count(counts["conflict_groups"], conflict_group_id)
            for source_ref in cls._source_map_refs(item):
                cls._increment_source_map_count(
                    counts["source_keys"],
                    cls._source_map_key(item, source_ref),
                )
        return counts

    @classmethod
    def _source_map_refs(cls, item: PersonaDistillationItem) -> list[dict[str, Any]]:
        refs = cls._item_source_refs(item)
        if refs:
            return refs
        return [{"memory_id": item.source_memory_id}]

    @staticmethod
    def _source_map_key(item: PersonaDistillationItem, source_ref: dict[str, Any]) -> str:
        for field in ("document_id", "source_id", "memory_id", "filename", "content_sha256"):
            value = source_ref.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return item.source_memory_id or "unknown-source"

    @classmethod
    def _source_map_entry(
            cls,
            key: str,
            item: PersonaDistillationItem,
            source_ref: dict[str, Any],
    ) -> dict[str, Any]:
        payload = item.structured_payload if isinstance(item.structured_payload, dict) else {}
        routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
        classification = payload.get("source_classification") if isinstance(payload.get("source_classification"),
                                                                            dict) else {}
        label = (
                source_ref.get("filename")
                or source_ref.get("document_id")
                or source_ref.get("memory_id")
                or item.source_memory_id
                or key
        )
        document_kind = source_ref.get("document_kind") or routing.get("document_kind") or classification.get(
            "document_kind")
        classification_label = (
                source_ref.get("source_classification")
                or classification.get("label")
                or routing.get("label")
                or "unknown"
        )
        # Persona is Agency's product term. This map exposes source-to-skill-equivalent lineage
        # for CLI/backend users without requiring the frontend review table.
        return {
            "key": key,
            "label": str(label),
            "memory_id": source_ref.get("memory_id") or item.source_memory_id,
            "document_id": source_ref.get("document_id"),
            "filename": source_ref.get("filename"),
            "content_sha256": source_ref.get("content_sha256"),
            "storage_uri": source_ref.get("storage_uri"),
            "upload_mode": source_ref.get("upload_mode"),
            "chunk_index": source_ref.get("chunk_index"),
            "chunk_count": source_ref.get("chunk_count"),
            "document_kind": document_kind or "unknown",
            "classification": classification_label,
            "source_intelligence_review_status": source_ref.get("source_intelligence_review_status"),
            "upload_intelligence_source": source_ref.get("upload_intelligence_source"),
            "source_ref": dict(source_ref),
            "item_count": 0,
            "needs_review_count": 0,
            "approved_count": 0,
            "rejected_count": 0,
            "review_statuses": {},
            "item_types": {},
            "memory_layers": {},
            "distillers": [],
            "extraction_sources": {},
            "vector_tags": cls._source_map_str_list(routing.get("vector_tags")),
            "extraction_targets": cls._source_map_str_list(routing.get("extraction_targets")),
            "content_roles": cls._source_map_str_list(routing.get("content_roles")),
            "review_flags": [],
            "item_ids": [],
        }

    @classmethod
    def _add_source_map_item(cls, entry: dict[str, Any], item: PersonaDistillationItem) -> None:
        payload = item.structured_payload if isinstance(item.structured_payload, dict) else {}
        routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
        entry["item_count"] += 1
        if item.needs_review:
            entry["needs_review_count"] += 1
        if item.review_status == PersonaDistillationItemReviewStatus.APPROVED:
            entry["approved_count"] += 1
        if item.review_status == PersonaDistillationItemReviewStatus.REJECTED:
            entry["rejected_count"] += 1
        cls._increment_source_map_count(entry["review_statuses"], item.review_status.value)
        cls._increment_source_map_count(entry["item_types"], item.item_type.value)
        cls._increment_source_map_count(entry["memory_layers"], item.memory_layer.value)
        cls._append_unique(entry["item_ids"], item.id)
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        distiller = payload.get("distiller") or metadata.get("distiller")
        if isinstance(distiller, str) and distiller.strip():
            cls._append_unique(entry["distillers"], distiller.strip())
        review_metadata = cls._item_review_metadata(item)
        cls._increment_source_map_count(entry["extraction_sources"], review_metadata["extraction_source"])
        for distiller in review_metadata["distillers"]:
            cls._append_unique(entry["distillers"], distiller)
        for field in ("vector_tags", "extraction_targets", "content_roles"):
            for value in cls._source_map_str_list(routing.get(field)):
                cls._append_unique(entry[field], value)
        for flag in review_metadata["review_flags"]:
            cls._append_unique(entry["review_flags"], flag)

    @staticmethod
    def _increment_source_map_count(bucket: dict[str, int], key: str) -> None:
        bucket[key] = int(bucket.get(key, 0)) + 1

    @staticmethod
    def _append_unique(values: list[str], value: str) -> None:
        if value not in values:
            values.append(value)

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    @staticmethod
    def _source_map_str_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @classmethod
    def _run_review_summary(
            cls,
            run: PersonaDistillationRun,
            items: list[PersonaDistillationItem],
    ) -> dict[str, Any]:
        counts = cls._run_item_counts(items)
        package_provenance = (
            run.output_package.get("provenance")
            if isinstance(run.output_package, dict) and isinstance(run.output_package.get("provenance"), dict)
            else {}
        )
        review_items = [cls._item_review_metadata(item) for item in items]
        evidence = {
            "with_source_evidence": sum(1 for item in review_items if item.get("source_evidence")),
            "missing_source_evidence": sum(1 for item in review_items if not item.get("source_evidence")),
            "verified": sum(1 for item in review_items if item.get("evidence", {}).get("verified") is True),
            "unverified": sum(1 for item in review_items if item.get("evidence", {}).get("verified") is False),
            "weak_or_missing": sum(
                1
                for item in review_items
                if {
                    "weak_source_evidence",
                    "missing_source_span",
                    "evidence_not_verified",
                }.intersection(set(item.get("review_flags") or []))
            ),
        }
        conflict_groups: dict[str, list[dict[str, Any]]] = {}
        semantic_groups: dict[str, list[dict[str, Any]]] = {}
        for item, review_metadata in zip(items, review_items):
            merge = review_metadata.get("merge") if isinstance(review_metadata.get("merge"), dict) else {}
            conflict_group_id = merge.get("conflict_group_id")
            if isinstance(conflict_group_id, str) and conflict_group_id:
                conflict_groups.setdefault(conflict_group_id, []).append(cls._summary_item(item, review_metadata))
            semantic_group_id = merge.get("semantic_duplicate_group_id")
            if isinstance(semantic_group_id, str) and semantic_group_id:
                semantic_groups.setdefault(semantic_group_id, []).append(cls._summary_item(item, review_metadata))
        return {
            "run_id": run.id,
            "persona_id": run.persona_id,
            "distillation_mode": run.distillation_mode.value,
            "llm_model_source": run.llm_model_source.value if run.llm_model_source else None,
            "resolved_model": {
                "provider": run.resolved_model_provider,
                "model": run.resolved_model,
                "model_profile_id": run.resolved_model_profile_id,
            },
            "package_synthesis": {
                "mode": package_provenance.get("package_synthesis_mode"),
                "llm_polishing_used": package_provenance.get("llm_polishing_used"),
                "polishing_model_profile_id": package_provenance.get("polishing_model_profile_id"),
                "polishing_prompt_version": package_provenance.get("polishing_prompt_version"),
            },
            "counts": counts,
            "evidence": evidence,
            "hybrid_comparison": {
                "llm_only_count": counts["review_flags"].get("llm_only", 0),
                "deterministic_only_count": counts["review_flags"].get("deterministic_only", 0),
                "agreed_count": counts["review_flags"].get("both_agreed", 0),
                "conflict_group_count": len(conflict_groups),
                "conflict_groups": [
                    {"id": group_id, "items": group_items}
                    for group_id, group_items in sorted(conflict_groups.items())
                ],
                "semantic_duplicate_groups": [
                    {"id": group_id, "items": group_items}
                    for group_id, group_items in sorted(semantic_groups.items())
                ],
            },
            "filter_options": {
                "extraction_sources": sorted(counts["extraction_sources"]),
                "distillers": sorted(counts["distillers"]),
                "review_flags": sorted(counts["review_flags"]),
                "conflict_group_ids": sorted(counts["conflict_groups"]),
            },
            "reviewer_actions": sorted(PERSONA_REVIEW_ACTIONS),
        }

    @staticmethod
    def _summary_item(item: PersonaDistillationItem, review_metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.id,
            "title": item.title,
            "item_type": item.item_type.value,
            "review_status": item.review_status.value,
            "needs_review": item.needs_review,
            "confidence": item.confidence,
            "extraction_source": review_metadata.get("extraction_source"),
            "distillers": review_metadata.get("distillers") or [],
            "review_flags": review_metadata.get("review_flags") or [],
        }

    async def _run_source_items(self, run_id: str, source_key: str) -> list[PersonaDistillationItem]:
        items = await self.context.persona_distillation_item_repo.list_by_run(run_id)
        return [
            item
            for item in items
            if source_key in self._source_filter_keys(item)
        ]

    async def _load_source_memories_for_items(self, items: list[PersonaDistillationItem]) -> list[MemoryRecord]:
        memory_ids: list[str] = []
        for item in items:
            if item.source_memory_id:
                self._append_unique(memory_ids, item.source_memory_id)
            for ref in self._source_map_refs(item):
                memory_id = ref.get("memory_id")
                if isinstance(memory_id, str) and memory_id.strip():
                    self._append_unique(memory_ids, memory_id.strip())
        return await self._load_memories(memory_ids)

    @staticmethod
    def _source_classification_payload_from_memory(memory: MemoryRecord) -> dict[str, Any]:
        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
        source_intelligence = (
            metadata.get("source_intelligence")
            if isinstance(metadata.get("source_intelligence"), dict)
            else {}
        )
        classification = (
            source_intelligence.get("classification")
            if isinstance(source_intelligence.get("classification"), dict)
            else {}
        )
        upload_intelligence = (
            metadata.get("upload_intelligence")
            if isinstance(metadata.get("upload_intelligence"), dict)
            else {}
        )
        recommended = (
            upload_intelligence.get("recommended")
            if isinstance(upload_intelligence.get("recommended"), dict)
            else {}
        )
        return {
            "label": classification.get("label") or metadata.get("source_classification") or "domain_knowledge",
            "confidence": classification.get("confidence") or upload_intelligence.get("confidence") or 0.85,
            "signals": classification.get("signals") if isinstance(classification.get("signals"), list) else [],
            "document_kind": (
                    classification.get("document_kind")
                    or upload_intelligence.get("document_kind")
                    or "unknown"
            ),
            "content_roles": classification.get("content_roles") if isinstance(classification.get("content_roles"),
                                                                               list) else [],
            "extraction_targets": (
                classification.get("extraction_targets")
                if isinstance(classification.get("extraction_targets"), list)
                else []
            ),
            "memory_layers": classification.get("memory_layers") if isinstance(classification.get("memory_layers"),
                                                                               list) else [],
            "vector_tags": (
                classification.get("vector_tags")
                if isinstance(classification.get("vector_tags"), list)
                else recommended.get("tags") if isinstance(recommended.get("tags"), list)
                else []
            ),
            "should_include": classification.get("should_include", True),
            "rationale": classification.get("rationale") or upload_intelligence.get("rationale"),
        }

    def _corrected_source_classification_payload(
            self,
            *,
            current_payload: dict[str, Any],
            classification: str | None,
            document_kind: str | None,
            content_roles: list[str] | None,
            extraction_targets: list[str] | None,
            memory_layers: list[str] | None,
            vector_tags: list[str] | None,
            confidence: float | None,
            rationale: str | None,
    ) -> dict[str, Any]:
        label = self._normalize_source_classification_label(classification or current_payload.get("label"))
        normalized_document_kind = self._normalize_document_kind(document_kind or current_payload.get("document_kind"))
        normalized_memory_layers = self._normalize_memory_layer_list(
            memory_layers
            if memory_layers is not None
            else current_payload.get("memory_layers")
        )
        normalized_confidence = (
            max(min(float(confidence), 1.0), 0.0)
            if confidence is not None
            else self._float_or_default(current_payload.get("confidence"), 0.95)
        )
        signals = self._normalize_string_list(current_payload.get("signals"))
        self._append_unique(signals, "manual_persona_source_correction")
        payload = {
            "label": label,
            "confidence": normalized_confidence,
            "signals": signals,
            "document_kind": normalized_document_kind,
            "content_roles": self._normalize_string_list(
                content_roles
                if content_roles is not None
                else current_payload.get("content_roles")
            ),
            "extraction_targets": self._normalize_string_list(
                extraction_targets
                if extraction_targets is not None
                else current_payload.get("extraction_targets")
            ),
            "memory_layers": normalized_memory_layers,
            "vector_tags": self._normalize_string_list(
                vector_tags
                if vector_tags is not None
                else current_payload.get("vector_tags")
            ),
            "should_include": True,
        }
        normalized_rationale = (rationale if rationale is not None else current_payload.get("rationale"))
        if isinstance(normalized_rationale, str) and normalized_rationale.strip():
            payload["rationale"] = normalized_rationale.strip()
        return payload

    @staticmethod
    def _normalize_source_classification_label(value: Any) -> str:
        label = str(value or "domain_knowledge").strip().lower()
        if label not in SOURCE_CLASSIFICATIONS:
            raise PersonaDistillationError(
                f"Invalid source classification '{label}'. Allowed values: {', '.join(sorted(SOURCE_CLASSIFICATIONS))}."
            )
        return label

    @staticmethod
    def _normalize_document_kind(value: Any) -> str:
        document_kind = str(value or "unknown").strip().lower()
        if document_kind not in SOURCE_INTELLIGENCE_DOCUMENT_KINDS:
            raise PersonaDistillationError(
                f"Invalid document kind '{document_kind}'. "
                f"Allowed values: {', '.join(sorted(SOURCE_INTELLIGENCE_DOCUMENT_KINDS))}."
            )
        return document_kind

    @staticmethod
    def _normalize_memory_layer_list(value: Any) -> list[str]:
        allowed = {item.value for item in PersonaMemoryLayer}
        layers = PersonaFactoryService._normalize_string_list(value)
        invalid = sorted(set(layers).difference(allowed))
        if invalid:
            raise PersonaDistillationError(
                f"Invalid memory layer(s): {', '.join(invalid)}. Allowed values: {', '.join(sorted(allowed))}."
            )
        return layers

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",")]
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        for item in value:
            text = str(item).strip().lower()
            if text and text not in normalized:
                normalized.append(text)
        return normalized

    @staticmethod
    def _float_or_default(value: Any, default: float) -> float:
        try:
            return max(min(float(value), 1.0), 0.0)
        except (TypeError, ValueError):
            return default

    async def _save_memory_source_classification(
            self,
            memory: MemoryRecord,
            *,
            classification_payload: dict[str, Any],
            run_id: str,
            source_key: str,
            current_user: UserDefinition | None,
    ) -> MemoryRecord:
        metadata = dict(memory.metadata or {})
        source_intelligence = (
            dict(metadata.get("source_intelligence"))
            if isinstance(metadata.get("source_intelligence"), dict)
            else {}
        )
        history = (
            list(metadata.get("source_intelligence_history"))
            if isinstance(metadata.get("source_intelligence_history"), list)
            else []
        )
        if source_intelligence:
            history.append(
                {
                    "source": "persona_factory_manual_review",
                    "run_id": run_id,
                    "source_key": source_key,
                    "previous": source_intelligence,
                    "updated_at": utc_now().isoformat(),
                }
            )
        source_intelligence["classification"] = dict(classification_payload)
        source_intelligence["review_status"] = "approved"
        source_intelligence["source"] = "persona_factory_manual_review"
        source_intelligence["updated_from_run_id"] = run_id
        source_intelligence["updated_source_key"] = source_key
        source_intelligence["updated_by_user_id"] = current_user.id if current_user is not None else None
        source_intelligence["updated_at"] = utc_now().isoformat()
        metadata["source_intelligence"] = source_intelligence
        metadata["source_intelligence_history"] = history[-25:]
        metadata["source_classification"] = classification_payload["label"]
        metadata["memory_layer"] = (
            classification_payload["memory_layers"][0]
            if classification_payload.get("memory_layers")
            else metadata.get("memory_layer")
        )
        tags = list(memory.tags)
        for tag in classification_payload.get("vector_tags", []):
            if tag not in tags:
                tags.append(tag)
        # This is a reviewer correction, not a new upload. Keep the memory id stable so all existing
        # persona/source provenance remains traceable through the same source record.
        return await self.context.memory_repo.save(
            memory.model_copy(
                update={
                    "metadata": metadata,
                    "tags": tags,
                    "updated_at": utc_now(),
                }
            )
        )

    async def _stamp_source_items_with_classification(
            self,
            items: list[PersonaDistillationItem],
            *,
            classification_payload: dict[str, Any],
            source_key: str,
            current_user: UserDefinition | None,
    ) -> list[PersonaDistillationItem]:
        updated: list[PersonaDistillationItem] = []
        routing = {
            "label": classification_payload["label"],
            "document_kind": classification_payload["document_kind"],
            "content_roles": classification_payload.get("content_roles", []),
            "extraction_targets": classification_payload.get("extraction_targets", []),
            "memory_layers": classification_payload.get("memory_layers", []),
            "vector_tags": classification_payload.get("vector_tags", []),
        }
        for item in items:
            payload = dict(item.structured_payload or {})
            payload["source_classification"] = dict(classification_payload)
            payload["routing"] = routing
            payload["source_ref"] = self._updated_source_ref_payload(
                payload.get("source_ref"),
                item=item,
                source_key=source_key,
                classification_payload=classification_payload,
            )
            if isinstance(payload.get("source_refs"), list):
                payload["source_refs"] = [
                    self._updated_source_ref_payload(
                        ref,
                        item=item,
                        source_key=source_key,
                        classification_payload=classification_payload,
                    )
                    for ref in payload["source_refs"]
                ]
            metadata = dict(item.metadata or {})
            metadata["source_classification"] = classification_payload["label"]
            metadata["classification_confidence"] = classification_payload["confidence"]
            metadata["source_classification_review"] = {
                "source_key": source_key,
                "updated_by_user_id": current_user.id if current_user is not None else None,
                "updated_at": utc_now().isoformat(),
            }
            updated.append(
                await self.update_item(
                    item.id,
                    {
                        "structured_payload": payload,
                        "metadata": metadata,
                    },
                    emit_audit=False,
                )
            )
        return updated

    @classmethod
    def _updated_source_ref_payload(
            cls,
            value: Any,
            *,
            item: PersonaDistillationItem,
            source_key: str,
            classification_payload: dict[str, Any],
    ) -> dict[str, Any]:
        ref = dict(value) if isinstance(value, dict) else {"memory_id": item.source_memory_id}
        if source_key in cls._source_ref_filter_keys(item, ref):
            ref["document_kind"] = classification_payload["document_kind"]
            ref["source_classification"] = classification_payload["label"]
            ref["source_intelligence_review_status"] = "approved"
        return ref

    @classmethod
    def _source_ref_filter_keys(cls, item: PersonaDistillationItem, source_ref: dict[str, Any]) -> set[str]:
        keys = {cls._source_map_key(item, source_ref)}
        for field in ("document_id", "source_id", "memory_id", "filename", "content_sha256"):
            value = source_ref.get(field)
            if isinstance(value, str) and value.strip():
                keys.add(value.strip())
        if item.source_memory_id:
            keys.add(item.source_memory_id)
        return keys

    @staticmethod
    def _group_has_conflict(group: list[PersonaDistillationItem]) -> bool:
        polarities = {PersonaFactoryService._claim_polarity(item.content) for item in group}
        return "positive" in polarities and "negative" in polarities

    @staticmethod
    def _claim_polarity(content: str) -> str:
        lowered = content.lower()
        if any(token in lowered for token in ("must not", "should not", "do not", "cannot", "avoid ", "never ")):
            return "negative"
        if any(token in lowered for token in ("must ", "should ", "can ", "rely ", "approve ", "allow ")):
            return "positive"
        return "neutral"

    @staticmethod
    def _communication_style_from_items(items: list[PersonaDistillationItem]) -> list[str]:
        text = "\n".join(item.content for item in items if item.item_type == PersonaDistillationItemType.WRITING_STYLE)
        text = text.lower()
        styles = []
        for label in ("direct", "diplomatic", "technical", "concise", "management-focused", "formal"):
            if label in text:
                styles.append(label)
        return styles or ["source-grounded", "practical", "concise"]

    @staticmethod
    def _escalation_style_from_items(items: list[PersonaDistillationItem]) -> str:
        text = "\n".join(item.content for item in items).lower()
        if "escalat" in text:
            return "Escalate according to the source-backed thresholds and stakeholders."
        return "Escalate uncertainty, missing evidence, and high-risk actions for human review."

    async def _resolve_distillation_model_selection(
            self,
            *,
            distillation_mode: str | None,
            llm_model_source: str | None,
            model_profile_id: str | None,
            llm_model_provider: str | None,
            llm_model: str | None,
    ) -> _DistillationModelSelection:
        settings = get_settings()
        mode = self._normalize_distillation_mode(distillation_mode)
        self._validate_distillation_mode_enabled(mode)
        if mode not in LLM_BACKED_DISTILLATION_MODES:
            if llm_model_source or llm_model_provider or llm_model:
                raise PersonaDistillationError(
                    "LLM model selection is only supported when distillation_mode is 'llm' or 'hybrid'."
                )
            profile = await self._resolve_optional_model_profile(model_profile_id)
            return _DistillationModelSelection(
                distillation_mode=mode,
                llm_model_source=None,
                model_profile_id=profile.id if profile else None,
                llm_model_provider=None,
                llm_model=None,
                resolved_model_provider=profile.provider if profile else None,
                resolved_model=profile.model if profile else None,
                resolved_model_profile_id=profile.id if profile else None,
            )

        source = self._normalize_llm_model_source(
            llm_model_source
            or (PersonaLLMModelSource.MODEL_PROFILE.value if model_profile_id else None)
            or settings.persona_factory_default_llm_model_source
        )
        if source == PersonaLLMModelSource.MAIN_AGENT:
            profile = await self._resolve_main_agent_model_profile()
            self._ensure_model_profile_resolves_for_distillation(profile)
            return _DistillationModelSelection(
                distillation_mode=mode,
                llm_model_source=source,
                model_profile_id=profile.id,
                llm_model_provider=None,
                llm_model=None,
                resolved_model_provider=profile.provider,
                resolved_model=profile.model,
                resolved_model_profile_id=profile.id,
            )
        if source == PersonaLLMModelSource.MODEL_PROFILE:
            if not model_profile_id:
                raise PersonaDistillationError(
                    "model_profile_id is required when llm_model_source is 'model_profile'."
                )
            profile = await self._resolve_required_model_profile(model_profile_id)
            self._ensure_model_profile_resolves_for_distillation(profile)
            return _DistillationModelSelection(
                distillation_mode=mode,
                llm_model_source=source,
                model_profile_id=profile.id,
                llm_model_provider=None,
                llm_model=None,
                resolved_model_provider=profile.provider,
                resolved_model=profile.model,
                resolved_model_profile_id=profile.id,
            )

        provider = str(llm_model_provider or "").strip()
        model = str(llm_model or "").strip()
        if not provider or not model:
            raise PersonaDistillationError(
                "llm_model_provider and llm_model are required when llm_model_source is 'model'."
            )
        inline_profile = ModelProfileDefinition(
            id=f"persona-distillation-inline:{provider}:{model}"[:64],
            name=f"Persona Distillation {provider}/{model}",
            provider=provider,
            model=model,
            supports_structured_output=True,
        )
        try:
            self.context.llm_provider_registry.resolve_provider_key(inline_profile)
        except Exception as exc:
            raise PersonaDistillationError(
                f"LLM model provider '{provider}' could not be resolved for Persona Factory: {exc}"
            ) from exc
        return _DistillationModelSelection(
            distillation_mode=mode,
            llm_model_source=source,
            model_profile_id=None,
            llm_model_provider=provider,
            llm_model=model,
            resolved_model_provider=provider,
            resolved_model=model,
            resolved_model_profile_id=None,
            inline_model_profile=inline_profile,
        )

    @staticmethod
    def _initial_distillation_metrics(model_selection: _DistillationModelSelection) -> dict[str, Any]:
        mode = model_selection.distillation_mode
        deterministic_enabled = mode in {PersonaDistillationMode.DETERMINISTIC, PersonaDistillationMode.HYBRID}
        llm_enabled = mode in LLM_BACKED_DISTILLATION_MODES
        run_metadata = {
            "distillation_mode": mode.value,
            "llm_model_source": model_selection.llm_model_source.value if model_selection.llm_model_source else None,
            "model_profile_id": model_selection.model_profile_id,
            "resolved_model_provider": model_selection.resolved_model_provider,
            "resolved_model": model_selection.resolved_model,
            "resolved_model_profile_id": model_selection.resolved_model_profile_id,
            "deterministic_distiller_version": DISTILLER_VERSION if deterministic_enabled else None,
            "deterministic_extractor": (
                "deterministic-multi-distiller-v1" if deterministic_enabled else None
            ),
            "deterministic_pipeline_version": (
                DETERMINISTIC_EXTRACTION_PIPELINE_VERSION if deterministic_enabled else None
            ),
            "llm_distiller_version": LLM_DISTILLER_VERSION if llm_enabled else None,
            "llm_extractor": LLM_DISTILLATION_EXTRACTOR if llm_enabled else None,
            "merge_strategy": HYBRID_MERGE_STRATEGY if mode == PersonaDistillationMode.HYBRID else None,
            "source_classification_strategy": (
                "stored_source_intelligence_or_upload_intelligence_then_model_profile_then_deterministic"
                if model_selection.resolved_model_profile_id
                else "stored_source_intelligence_or_upload_intelligence_then_deterministic"
            ),
        }
        return {
            "run_metadata": {
                key: value
                for key, value in run_metadata.items()
                if value is not None
            }
        }

    @staticmethod
    def _normalize_distillation_mode(value: str | None) -> PersonaDistillationMode:
        normalized = str(value or get_settings().persona_factory_default_distillation_mode).strip().lower()
        try:
            return PersonaDistillationMode(normalized)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in PersonaDistillationMode)
            raise PersonaDistillationError(
                f"Unsupported distillation_mode '{value}'. Allowed values: {allowed}."
            ) from exc

    @staticmethod
    def _validate_distillation_mode_enabled(mode: PersonaDistillationMode) -> None:
        settings = get_settings()
        if mode in LLM_BACKED_DISTILLATION_MODES and not settings.persona_factory_llm_distillation_enabled:
            raise PersonaDistillationError(
                "LLM-backed persona distillation is disabled by PERSONA_FACTORY_LLM_DISTILLATION_ENABLED."
            )
        if mode == PersonaDistillationMode.HYBRID and not settings.persona_factory_hybrid_distillation_enabled:
            raise PersonaDistillationError(
                "Hybrid persona distillation is disabled by PERSONA_FACTORY_HYBRID_DISTILLATION_ENABLED."
            )

    @staticmethod
    def _normalize_llm_model_source(value: str | None) -> PersonaLLMModelSource:
        normalized = str(value or get_settings().persona_factory_default_llm_model_source).strip().lower()
        try:
            return PersonaLLMModelSource(normalized)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in PersonaLLMModelSource)
            raise PersonaDistillationError(
                f"Unsupported llm_model_source '{value}'. Allowed values: {allowed}."
            ) from exc

    async def _resolve_main_agent_model_profile(self) -> ModelProfileDefinition:
        repo = getattr(self.context, "main_agent_profile_repo", None)
        if repo is None:
            raise PersonaDistillationError("Main-agent profile repository is not available.")
        profiles = await repo.list()
        enabled = [profile for profile in profiles if getattr(profile, "enabled", False)]
        if not enabled:
            raise PersonaDistillationError(
                "Active main-agent profile was not found. Select a model profile or configure the main agent first."
            )
        enabled.sort(key=lambda profile: (profile.created_at, profile.id))
        profile = enabled[0]
        model_profile_id = getattr(profile, "default_model_profile_id", None)
        if not model_profile_id:
            raise PersonaDistillationError(
                f"Active main-agent profile '{profile.id}' has no default model profile. "
                "Select a model profile for Persona distillation or update the main-agent setup."
            )
        model_profile = await self.context.model_profile_repo.get(model_profile_id)
        if model_profile is None:
            raise PersonaDistillationError(
                f"Main-agent model profile '{model_profile_id}' was not found. "
                "Select a model profile for Persona distillation or update the main-agent setup."
            )
        return model_profile

    def _ensure_model_profile_resolves_for_distillation(self, profile: ModelProfileDefinition) -> None:
        try:
            self.context.llm_provider_registry.resolve_provider_key(profile)
        except Exception as exc:
            raise PersonaDistillationError(
                f"Model profile '{profile.id}' provider '{profile.provider}' could not be resolved "
                f"for Persona Factory: {exc}"
            ) from exc

    async def _classify_source_memory(
            self,
            *,
            pipeline: PersonaDistillationPipeline,
            memory: MemoryRecord,
            model_profile: ModelProfileDefinition | None,
    ) -> PersonaSourceClassification:
        deterministic = pipeline.classify(memory)
        if any(signal in deterministic.signals for signal in {
            "metadata:source_intelligence",
            "metadata:upload_intelligence",
        }):
            return deterministic
        if model_profile is None:
            return deterministic
        try:
            return await SourceIntelligenceService(self.context).classify_memory(
                memory,
                model_profile=model_profile,
                purpose="persona_factory",
            )
        except SourceIntelligenceError as exc:
            raise PersonaDistillationError(str(exc)) from exc

    async def _resolve_optional_model_profile(
            self,
            model_profile_id: str | None,
    ) -> ModelProfileDefinition | None:
        if not model_profile_id:
            return None
        return await self._resolve_required_model_profile(model_profile_id)

    async def _resolve_required_model_profile(self, model_profile_id: str) -> ModelProfileDefinition:
        profile = await self.context.model_profile_repo.get(model_profile_id)
        if profile is None:
            raise PersonaDistillationError(f"Model profile '{model_profile_id}' not found.")
        return profile

    async def _resolve_run_model_profile(self, run: PersonaDistillationRun) -> ModelProfileDefinition | None:
        if (
                run.distillation_mode in LLM_BACKED_DISTILLATION_MODES
                and run.llm_model_source == PersonaLLMModelSource.MODEL
                and run.llm_model_provider
                and run.llm_model
        ):
            return ModelProfileDefinition(
                id=f"persona-distillation-inline:{run.llm_model_provider}:{run.llm_model}"[:64],
                name=f"Persona Distillation {run.llm_model_provider}/{run.llm_model}",
                provider=run.llm_model_provider,
                model=run.llm_model,
                supports_structured_output=True,
            )
        return await self._resolve_optional_model_profile(run.model_profile_id)

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
            raise PersonaDistillationError(
                f"Model profile '{profile.id}' could not be resolved for Persona Factory: {exc}"
            ) from exc
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
            raise PersonaDistillationError(f"Persona Factory model call failed: {exc}") from exc
        if not isinstance(response.content, dict):
            raise PersonaDistillationError(f"Structured model response '{schema_name}' was not an object.")
        return response.content

    async def _extract_distillation_items(
            self,
            *,
            persona: PersonaDefinition,
            run: PersonaDistillationRun,
            memories: list[MemoryRecord],
            sources: list[PersonaSource],
    ) -> list[PersonaDistillationItem]:
        source_by_memory = {source.source_id: source for source in sources}
        pipeline = PersonaDistillationPipeline()
        llm_engine = LLMDistillationEngine()
        settings = get_settings()
        llm_timeout_seconds = float(settings.persona_factory_llm_timeout_seconds)
        llm_max_calls = int(settings.persona_factory_llm_max_calls_per_run)
        llm_retry_attempts = max(0, int(settings.persona_factory_llm_retry_attempts))
        llm_max_attempts_per_source = llm_retry_attempts + 1
        distillation_metrics = dict(run.distillation_metrics or {})
        llm_metrics = dict(distillation_metrics.get("llm_distillation") or {})
        llm_metrics.setdefault("call_count", 0)
        llm_metrics.setdefault("success_count", 0)
        llm_metrics.setdefault("failure_count", 0)
        llm_metrics.setdefault("timeout_count", 0)
        llm_metrics.setdefault("retry_count", 0)
        llm_metrics.setdefault("transient_failure_count", 0)
        llm_metrics.setdefault("total_latency_ms", 0)
        llm_metrics.setdefault("max_calls_per_run", llm_max_calls)
        llm_metrics.setdefault("timeout_seconds", llm_timeout_seconds)
        llm_metrics.setdefault("retry_attempts_per_source", llm_retry_attempts)
        llm_metrics.setdefault("sources", [])
        extracted: list[PersonaDistillationItem] = []
        model_profile = await self._resolve_run_model_profile(run)
        for memory in memories:
            source_ref = self._source_ref(memory, source_by_memory.get(memory.id))
            classification = await self._classify_source_memory(
                pipeline=pipeline,
                memory=memory,
                model_profile=model_profile,
            )
            deterministic_candidates = (
                pipeline.extract(
                    memory=memory,
                    source_ref=source_ref,
                    classification=classification,
                )
                if run.distillation_mode in {PersonaDistillationMode.DETERMINISTIC, PersonaDistillationMode.HYBRID}
                else []
            )
            llm_candidates = []
            if run.distillation_mode in LLM_BACKED_DISTILLATION_MODES:
                if model_profile is None:
                    raise PersonaDistillationError("LLM distillation requires a resolved model profile.")
                for attempt_index in range(1, llm_max_attempts_per_source + 1):
                    if llm_metrics["call_count"] >= llm_max_calls:
                        message = (
                            f"Persona Factory LLM distillation call limit exceeded: "
                            f"{llm_max_calls} calls per run."
                        )
                        llm_metrics["failure_count"] += 1
                        llm_metrics["last_failure_reason"] = "call_limit_exceeded"
                        llm_metrics["sources"].append(
                            {
                                "source_memory_id": memory.id,
                                "status": "failed",
                                "failure_reason": "call_limit_exceeded",
                                "message": message,
                                "attempt_index": attempt_index,
                                "retry_count": max(0, attempt_index - 1),
                            }
                        )
                        distillation_metrics["llm_distillation"] = llm_metrics
                        run.distillation_metrics = distillation_metrics
                        if run.distillation_mode == PersonaDistillationMode.LLM:
                            raise PersonaDistillationError(message)
                        run.warnings.append(
                            {
                                "type": "llm_distillation_call_limit_exceeded",
                                "source_memory_id": memory.id,
                                "message": message,
                            }
                        )
                        break
                    llm_metrics["call_count"] += 1
                    call_index = llm_metrics["call_count"]
                    started_at = time.perf_counter()
                    try:
                        llm_candidates = await asyncio.wait_for(
                            llm_engine.extract_source(
                                memory=memory,
                                source_ref=source_ref,
                                classification=classification,
                                model_profile=model_profile,
                                generate_structured=self._generate_structured_with_profile,
                            ),
                            timeout=llm_timeout_seconds,
                        )
                        latency_ms = int((time.perf_counter() - started_at) * 1000)
                        llm_metrics["success_count"] += 1
                        llm_metrics["total_latency_ms"] += latency_ms
                        llm_metrics["sources"].append(
                            {
                                "source_memory_id": memory.id,
                                "status": "success",
                                "latency_ms": latency_ms,
                                "candidate_count": len(llm_candidates),
                                "model_provider": model_profile.provider,
                                "model": model_profile.model,
                                "model_profile_id": model_profile.id,
                                "attempt_count": attempt_index,
                                "retry_count": max(0, attempt_index - 1),
                            }
                        )
                        for candidate in llm_candidates:
                            candidate.metadata["llm_distillation_call"] = {
                                "latency_ms": latency_ms,
                                "call_index": call_index,
                                "timeout_seconds": llm_timeout_seconds,
                                "attempt_count": attempt_index,
                                "retry_count": max(0, attempt_index - 1),
                            }
                        break
                    except (PersonaLLMDistillationError, PersonaDistillationError, asyncio.TimeoutError) as exc:
                        latency_ms = int((time.perf_counter() - started_at) * 1000)
                        is_timeout = isinstance(exc, asyncio.TimeoutError)
                        failure_reason = "timeout" if is_timeout else type(exc).__name__
                        message = (
                            f"Persona Factory LLM distillation timed out after {llm_timeout_seconds:g}s."
                            if is_timeout
                            else str(exc)
                        )
                        llm_metrics["total_latency_ms"] += latency_ms
                        llm_metrics["last_failure_reason"] = failure_reason
                        if is_timeout:
                            llm_metrics["timeout_count"] += 1
                        if attempt_index < llm_max_attempts_per_source:
                            llm_metrics["retry_count"] += 1
                            llm_metrics["transient_failure_count"] += 1
                            llm_metrics["sources"].append(
                                {
                                    "source_memory_id": memory.id,
                                    "status": "retrying",
                                    "latency_ms": latency_ms,
                                    "failure_reason": failure_reason,
                                    "message": message,
                                    "attempt_index": attempt_index,
                                    "next_attempt_index": attempt_index + 1,
                                    "model_provider": model_profile.provider,
                                    "model": model_profile.model,
                                    "model_profile_id": model_profile.id,
                                }
                            )
                            continue
                        llm_metrics["failure_count"] += 1
                        llm_metrics["sources"].append(
                            {
                                "source_memory_id": memory.id,
                                "status": "failed",
                                "latency_ms": latency_ms,
                                "failure_reason": failure_reason,
                                "message": message,
                                "model_provider": model_profile.provider,
                                "model": model_profile.model,
                                "model_profile_id": model_profile.id,
                                "attempt_count": attempt_index,
                                "retry_count": max(0, attempt_index - 1),
                            }
                        )
                        distillation_metrics["llm_distillation"] = llm_metrics
                        run.distillation_metrics = distillation_metrics
                        if run.distillation_mode == PersonaDistillationMode.LLM:
                            raise PersonaDistillationError(message) from exc
                        run.warnings.append(
                            {
                                "type": "llm_distillation_failed",
                                "source_memory_id": memory.id,
                                "message": message,
                                "failure_reason": failure_reason,
                                "retry_count": max(0, attempt_index - 1),
                            }
                        )
                        break
                    finally:
                        distillation_metrics["llm_distillation"] = llm_metrics
                        run.distillation_metrics = distillation_metrics
            if run.distillation_mode == PersonaDistillationMode.HYBRID:
                candidates, hybrid_merge_metrics = HybridDistillationMerger().merge(
                    deterministic_candidates=deterministic_candidates,
                    llm_candidates=llm_candidates,
                )
                distillation_metrics["hybrid_merge"] = hybrid_merge_metrics
                run.distillation_metrics = distillation_metrics
            else:
                candidates = [*deterministic_candidates, *llm_candidates]
            for candidate in candidates:
                confidence = candidate.confidence
                needs_review = self._distillation_item_needs_review(
                    confidence=confidence,
                    item_type=candidate.item_type,
                    memory=memory,
                    candidate_needs_review=candidate.needs_review,
                )
                # Distillation items are the reviewable bridge between raw uploads and final persona packages.
                # Deterministic, LLM, and hybrid paths all persist through this provenance review contract.
                item = await self.context.persona_distillation_item_repo.create(
                    PersonaDistillationItem(
                        run_id=run.id,
                        persona_id=persona.id,
                        source_memory_id=memory.id,
                        item_type=candidate.item_type,
                        memory_layer=candidate.memory_layer,
                        title=candidate.title,
                        content=candidate.content,
                        structured_payload={
                            **candidate.structured_payload,
                            "memory_type": memory.memory_type.value if memory.memory_type else None,
                            "tags": memory.tags,
                            "pipeline": "classify-extract-normalize-validate-v1",
                        },
                        confidence=confidence,
                        needs_review=needs_review,
                        review_status=(
                            PersonaDistillationItemReviewStatus.NEEDS_REVIEW
                            if needs_review
                            else PersonaDistillationItemReviewStatus.DRAFT
                        ),
                        metadata={
                            **candidate.metadata,
                            "persona_slug": persona.slug,
                            "source": memory.source,
                        },
                    )
                )
                extracted.append(item)
        return extracted

    async def _suggest_tools(self, memories: list[MemoryRecord]) -> list[dict[str, Any]]:
        text = "\n".join([memory.content for memory in memories]).lower()
        suggestions: list[dict[str, Any]] = []
        for tool in await self.context.tool_repo.list():
            needles = {tool.id.lower(), tool.name.lower()}
            display_name = getattr(tool, "display_name", None)
            if display_name:
                needles.add(str(display_name).lower())
            if any(needle and needle in text for needle in needles):
                suggestions.append(
                    {
                        "tool_id": tool.id,
                        "name": tool.name,
                        "granted": False,
                        "confidence": 0.7,
                        "rationale": "Mentioned in selected source material. Review before granting.",
                    }
                )
        return suggestions

    async def _publish_persona_memories(
            self,
            *,
            persona: PersonaDefinition,
            version: PersonaVersion,
            package: dict[str, Any],
            current_user: UserDefinition,
    ) -> list[str]:
        memory_ids: list[str] = []
        layers = package.get("memory_layers") if isinstance(package.get("memory_layers"), dict) else {}
        item_synthesized = package.get("provenance", {}).get("strategy") == "item-synthesis-v1"
        scope = MemoryScope.WORKSPACE.value if persona.workspace_id else MemoryScope.USER.value
        for layer_name, entries in layers.items():
            if layer_name not in {layer.value for layer in PersonaMemoryLayer} or not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries[:50]):
                if not isinstance(entry, dict):
                    continue
                review_status = entry.get("review_status")
                if item_synthesized and review_status != PersonaDistillationItemReviewStatus.APPROVED.value:
                    continue
                memory_id = f"skm-{version.id[:18]}-{layer_name[:3]}-{index}"
                existing = await self.context.memory_repo.get(memory_id)
                if existing is not None:
                    memory_ids.append(existing.id)
                    continue
                content = str(entry.get("content") or "").strip()
                if not content:
                    continue
                source_refs = entry.get("source_refs") if isinstance(entry.get("source_refs"), list) else []
                source_memory_id = self._source_memory_id_from_refs(source_refs)
                payload: dict[str, Any] = {
                    "id": memory_id,
                    "scope": scope,
                    "created_by_user_id": current_user.id,
                    "workspace_id": persona.workspace_id,
                    "content": content,
                    "summary": entry.get("title") or self._title_from_content(content),
                    "tags": ["persona", f"persona:{persona.slug}", f"memory_layer:{layer_name}"],
                    "source": "persona_factory",
                    "memory_type": self._memory_type_for_layer(layer_name),
                    "importance": 70,
                    "metadata": {
                        "persona_id": persona.id,
                        "persona_slug": persona.slug,
                        "persona_version_id": version.id,
                        "memory_layer": layer_name,
                        "confidence": entry.get("confidence"),
                        "source_refs": source_refs,
                        "source_memory_id": source_memory_id,
                        "distillation_item_id": entry.get("distillation_item_id"),
                        "item_type": entry.get("item_type"),
                        "review_status": review_status,
                        "needs_review": bool(entry.get("needs_review")),
                        "distillation_run_id": version.generated_from_run_id,
                    },
                }
                memory = await MemoryService(self.context).create_memory(
                    payload,
                    confirmed=True,
                    current_user=current_user,
                    trusted_actor=True,
                )
                memory_ids.append(memory.id)
        return memory_ids

    @staticmethod
    def _source_memory_id_from_refs(source_refs: list[Any]) -> str | None:
        for ref in source_refs:
            if isinstance(ref, dict) and isinstance(ref.get("memory_id"), str):
                return ref["memory_id"]
        return None

    async def _approved_version_for_run(
            self,
            *,
            persona: PersonaDefinition,
            run: PersonaDistillationRun,
            current_user: UserDefinition,
    ) -> PersonaVersion:
        versions = await self.context.persona_version_repo.list_by_persona(persona.id)
        existing = next((item for item in versions if item.generated_from_run_id == run.id), None)
        if existing is not None:
            return existing
        approved = await self.approve_run(run.id, current_user=current_user)
        return PersonaVersion.model_validate(approved["persona_version"])

    async def _next_version_label(self, persona_id: str) -> str:
        versions = await self.context.persona_version_repo.list_by_persona(persona_id)
        if not versions:
            return "1.0.0"
        return f"1.0.{len(versions)}"

    def _validate_package(self, package: dict[str, Any]) -> None:
        errors = self._package_validation_errors(package)
        if errors:
            raise PersonaDistillationError(f"Persona package validation failed: {'; '.join(errors)}")
        package["governance"] = self._normalize_governance_labels(package.get("governance"))

    @staticmethod
    def _package_validation_errors(package: Any) -> list[str]:
        if not isinstance(package, dict):
            return ["package: must be an object"]
        errors: list[str] = []
        if package.get("schema_version") != 1:
            errors.append("schema_version: must be 1")
        if not isinstance(package.get("persona"), dict):
            errors.append("persona: required object")
        if not isinstance(package.get("memory_layers"), dict):
            errors.append("memory_layers: required object")
        for section in ("knowledge", "decision_patterns", "workflows", "tools", "guardrails", "examples"):
            if section in package and not isinstance(package.get(section), list):
                errors.append(f"{section}: must be a list")
        for section in ("identity", "governance", "runtime", "provenance"):
            if section in package and not isinstance(package.get(section), dict):
                errors.append(f"{section}: must be an object")
        memory_layers = package.get("memory_layers")
        if isinstance(memory_layers, dict):
            for layer, entries in memory_layers.items():
                if not isinstance(entries, list):
                    errors.append(f"memory_layers.{layer}: must be a list")
        return errors

    def _validate_package_review_ready(self, package: dict[str, Any]) -> None:
        provenance = package.get("provenance") if isinstance(package.get("provenance"), dict) else {}
        if provenance.get("strategy") != "item-synthesis-v1":
            return
        needs_review_count = self._package_needs_review_count(package)
        if needs_review_count > 0:
            raise PersonaDistillationError(
                f"Persona package still has {needs_review_count} active distillation item(s) marked needs_review. "
                "Approve or reject those items, then synthesize the package again."
            )
        governance = package.get("governance") if isinstance(package.get("governance"), dict) else {}
        if self._requires_explicit_item_approval(governance):
            unapproved = [
                entry for entry in self._package_distillation_entries(package)
                if entry.get("distillation_item_id") and entry.get("review_status") != "approved"
            ]
            if unapproved:
                raise PersonaDistillationError(
                    "This persona uses personal, intimate, regulated, self, public-figure, or unverified-private-person "
                    "governance labels, so every active distillation item must be explicitly approved before package approval."
                )

    @staticmethod
    def _requires_explicit_item_approval(governance: dict[str, Any]) -> bool:
        return (
                governance.get("persona_type") in {"personal", "self", "public_figure"}
                or governance.get("sensitivity_level") in {"intimate", "regulated"}
                or governance.get("consent_status") == "unverified_private_person"
        )

    @staticmethod
    def _default_governance_guardrails(governance: dict[str, Any]) -> list[dict[str, Any]]:
        guardrails: list[dict[str, Any]] = []
        if governance.get("representation_policy") == "simulated_persona":
            guardrails.append(
                {
                    "title": "Simulated persona disclosure",
                    "content": (
                        "Represent this as a simulated persona based on reviewed source material; "
                        "do not claim to be the actual person."
                    ),
                    "confidence": 1.0,
                    "source_refs": [],
                    "governance_default": True,
                }
            )
        if governance.get("persona_type") in {"personal", "self", "public_figure"}:
            guardrails.extend(
                [
                    {
                        "title": "No unsupported private facts",
                        "content": "Do not invent private facts, feelings, memories, relationships, or consent status.",
                        "confidence": 1.0,
                        "source_refs": [],
                        "governance_default": True,
                    },
                    {
                        "title": "Weak source uncertainty",
                        "content": "When source support is weak or missing, state the uncertainty instead of roleplaying certainty.",
                        "confidence": 1.0,
                        "source_refs": [],
                        "governance_default": True,
                    },
                ]
            )
        return guardrails

    def _package_needs_review_count(self, package: dict[str, Any]) -> int:
        entries = self._package_distillation_entries(package)
        return sum(1 for entry in entries if entry.get("needs_review") is True)

    @staticmethod
    def _package_distillation_entries(package: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for key in ("knowledge", "decision_patterns", "guardrails", "examples"):
            value = package.get(key)
            if isinstance(value, list):
                entries.extend(item for item in value if isinstance(item, dict))
        workflows = package.get("workflows")
        if isinstance(workflows, list):
            entries.extend(item for item in workflows if isinstance(item, dict))
        tools = package.get("tools")
        if isinstance(tools, list):
            entries.extend(item for item in tools if isinstance(item, dict))
        layers = package.get("memory_layers")
        if isinstance(layers, dict):
            for value in layers.values():
                if isinstance(value, list):
                    entries.extend(item for item in value if isinstance(item, dict))

        deduped: dict[str, dict[str, Any]] = {}
        anonymous: list[dict[str, Any]] = []
        for entry in entries:
            item_id = entry.get("distillation_item_id")
            if isinstance(item_id, str) and item_id:
                deduped[item_id] = entry
            else:
                anonymous.append(entry)
        return [*deduped.values(), *anonymous]

    @staticmethod
    def _normalize_governance_labels(labels: dict[str, Any] | None) -> dict[str, str]:
        normalized = dict(DEFAULT_GOVERNANCE_LABELS)
        if not isinstance(labels, dict):
            return normalized
        for key, raw_value in labels.items():
            if key not in GOVERNANCE_ALLOWED_VALUES:
                continue
            value = str(raw_value).strip().lower()
            if not value:
                continue
            if value not in GOVERNANCE_ALLOWED_VALUES[key]:
                allowed = ", ".join(sorted(GOVERNANCE_ALLOWED_VALUES[key]))
                raise PersonaDistillationError(f"Invalid governance label {key}='{value}'. Allowed values: {allowed}.")
            normalized[key] = value
        PersonaFactoryService._validate_governance_label_combination(normalized)
        return normalized

    @staticmethod
    def _validate_governance_label_combination(governance: dict[str, str]) -> None:
        persona_type = governance["persona_type"]
        consent_status = governance["consent_status"]
        source_basis = governance["source_basis"]
        sensitivity_level = governance["sensitivity_level"]
        visibility = governance["visibility"]

        if persona_type == "self" and consent_status not in {"self", "explicit_consent"}:
            raise PersonaDistillationError("Self personas require consent_status='self' or 'explicit_consent'.")
        if persona_type == "fictional" and consent_status != "fictional":
            raise PersonaDistillationError("Fictional personas require consent_status='fictional'.")
        if consent_status == "fictional" and persona_type != "fictional":
            raise PersonaDistillationError("consent_status='fictional' is only valid for persona_type='fictional'.")
        if consent_status == "self" and persona_type != "self":
            raise PersonaDistillationError("consent_status='self' is only valid for persona_type='self'.")
        if consent_status == "public_material" and source_basis not in {"public_sources", "mixed"}:
            raise PersonaDistillationError(
                "consent_status='public_material' requires source_basis='public_sources' or 'mixed'."
            )
        if persona_type == "public_figure" and source_basis not in {"public_sources", "mixed"}:
            raise PersonaDistillationError("Public-figure personas require public or mixed source material.")
        if persona_type == "personal" and consent_status not in {
            "explicit_consent",
            "unverified_private_person",
            "self",
        }:
            raise PersonaDistillationError(
                "Personal personas require explicit, self, or unverified-private-person consent status."
            )
        if consent_status == "unverified_private_person" and visibility != "private":
            raise PersonaDistillationError("Unverified private-person personas must remain private.")
        if sensitivity_level == "intimate" and visibility in {"organization", "marketplace"}:
            raise PersonaDistillationError("Intimate personas cannot use organization or marketplace visibility.")

        if visibility == "marketplace":
            if persona_type in {"personal", "self"}:
                raise PersonaDistillationError("Personal or self personas cannot use marketplace visibility.")
            if sensitivity_level != "standard":
                raise PersonaDistillationError("Marketplace personas must use sensitivity_level='standard'.")
            if consent_status in {"unspecified", "unverified_private_person", "self"}:
                raise PersonaDistillationError(
                    "Marketplace personas require explicit, public-material, organization-authorized, or fictional consent."
                )
            if source_basis in {"memory_records", "uploaded_private_material", "chat_export"}:
                raise PersonaDistillationError(
                    "Marketplace personas cannot be based only on private memory records, uploaded private material, or chat exports."
                )
            if persona_type == "public_figure" and (
                    consent_status != "public_material" or source_basis != "public_sources"
            ):
                raise PersonaDistillationError(
                    "Marketplace public-figure personas require public_material consent and public_sources basis."
                )

    @staticmethod
    def _source_ref(memory: MemoryRecord, source: PersonaSource | None) -> dict[str, Any]:
        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
        upload_intelligence = metadata.get("upload_intelligence") if isinstance(metadata.get("upload_intelligence"),
                                                                                dict) else {}
        source_intelligence = metadata.get("source_intelligence") if isinstance(metadata.get("source_intelligence"),
                                                                                dict) else {}
        source_classification = (
            source_intelligence.get("classification")
            if isinstance(source_intelligence.get("classification"), dict)
            else {}
        )
        return {
            "source_id": source.id if source else None,
            "memory_id": memory.id,
            "document_id": metadata.get("document_id"),
            "filename": metadata.get("filename"),
            "content_sha256": metadata.get("content_sha256"),
            "storage_uri": metadata.get("storage_uri"),
            "upload_mode": metadata.get("upload_mode"),
            "chunk_index": metadata.get("chunk_index"),
            "chunk_count": metadata.get("chunk_count"),
            "start_char": metadata.get("start_char"),
            "end_char": metadata.get("end_char"),
            "document_kind": (
                    upload_intelligence.get("document_kind")
                    or source_classification.get("document_kind")
                    or "unknown"
            ),
            "source_classification": source_classification.get("label"),
            "upload_intelligence_source": upload_intelligence.get("source"),
            "source_intelligence_review_status": source_intelligence.get("review_status"),
            "confidence": 0.75,
        }

    @staticmethod
    def _memory_layer(memory: MemoryRecord) -> str:
        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
        explicit = metadata.get("memory_layer")
        if explicit in {"semantic", "episodic", "procedural", "social"}:
            return explicit
        text = f"{memory.summary or ''}\n{memory.content}".lower()
        for layer, keywords in MEMORY_LAYER_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return layer
        return "semantic"

    def _distillation_item_type(self, memory: MemoryRecord) -> PersonaDistillationItemType:
        lowered_tags = {tag.lower() for tag in memory.tags}
        text = f"{memory.summary or ''}\n{memory.content}".lower()
        if memory.memory_type == MemoryType.DECISION or "decision" in lowered_tags:
            return PersonaDistillationItemType.DECISION_PATTERN
        if memory.memory_type == MemoryType.PREFERENCE or any(
                token in text for token in ("tone", "style", "phrasing", "writes", "responds")
        ):
            return PersonaDistillationItemType.WRITING_STYLE
        if self._looks_like_tool_usage(text):
            return PersonaDistillationItemType.TOOL_USAGE
        if self._looks_like_guardrail(memory.content):
            return PersonaDistillationItemType.GUARDRAIL
        if self._looks_like_example(memory.content):
            return PersonaDistillationItemType.EXAMPLE
        if any(token in text for token in ("workflow", "lifecycle", "handoff", "sequence")):
            return PersonaDistillationItemType.WORKFLOW
        if any(token in text for token in ("process", "procedure", "sop", "checklist", "step")):
            return PersonaDistillationItemType.PROCEDURE
        layer = self._memory_layer(memory)
        if layer == "procedural":
            return PersonaDistillationItemType.PROCEDURE
        if layer == "social":
            return PersonaDistillationItemType.SOCIAL_CONTEXT
        return PersonaDistillationItemType.DOMAIN_KNOWLEDGE

    def _distillation_memory_layer(
            self,
            memory: MemoryRecord,
            item_type: PersonaDistillationItemType,
    ) -> PersonaMemoryLayer:
        if item_type == PersonaDistillationItemType.WRITING_STYLE:
            return PersonaMemoryLayer.PERSONA
        if item_type == PersonaDistillationItemType.TOOL_USAGE:
            return PersonaMemoryLayer.TOOL
        if item_type == PersonaDistillationItemType.SOCIAL_CONTEXT:
            return PersonaMemoryLayer.SOCIAL
        if item_type in {
            PersonaDistillationItemType.PROCEDURE,
            PersonaDistillationItemType.WORKFLOW,
            PersonaDistillationItemType.DECISION_PATTERN,
        }:
            return PersonaMemoryLayer.PROCEDURAL
        if item_type == PersonaDistillationItemType.EXAMPLE:
            return PersonaMemoryLayer.EPISODIC
        layer = self._memory_layer(memory)
        return {
            "semantic": PersonaMemoryLayer.SEMANTIC,
            "procedural": PersonaMemoryLayer.PROCEDURAL,
            "episodic": PersonaMemoryLayer.EPISODIC,
            "social": PersonaMemoryLayer.SOCIAL,
        }.get(layer, PersonaMemoryLayer.SEMANTIC)

    @staticmethod
    def _distillation_item_needs_review(
            *,
            confidence: float,
            item_type: PersonaDistillationItemType,
            memory: MemoryRecord,
            candidate_needs_review: bool = False,
    ) -> bool:
        if candidate_needs_review:
            return True
        if confidence < 0.8:
            return True
        if item_type in {
            PersonaDistillationItemType.WRITING_STYLE,
            PersonaDistillationItemType.TOOL_USAGE,
            PersonaDistillationItemType.GUARDRAIL,
        }:
            return True
        return bool(memory.sensitive)

    @staticmethod
    def _confidence(memory: MemoryRecord) -> float:
        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
        raw = metadata.get("confidence")
        try:
            return max(min(float(raw), 1.0), 0.0)
        except (TypeError, ValueError):
            return 0.75

    @staticmethod
    def _memory_type_for_layer(layer_name: str) -> str:
        return {
            "semantic": MemoryType.FACT.value,
            "episodic": MemoryType.ARCHIVE.value,
            "procedural": MemoryType.FACT.value,
            "social": MemoryType.FACT.value,
        }.get(layer_name, MemoryType.FACT.value)

    @staticmethod
    def _title_from_content(content: str) -> str:
        first = " ".join(content.strip().split())[:120]
        return first or "Persona memory"

    @staticmethod
    def _looks_like_guardrail(content: str) -> bool:
        lowered = content.lower()
        return any(token in lowered for token in ("must not", "never", "approval", "guardrail", "policy", "risk"))

    @staticmethod
    def _looks_like_example(content: str) -> bool:
        lowered = content.lower()
        return "example" in lowered or "sample" in lowered

    @staticmethod
    def _looks_like_tool_usage(content: str) -> bool:
        lowered = content.lower()
        return any(
            token in lowered
            for token in (
                "jira",
                "servicenow",
                "gitlab",
                "github",
                "opensearch",
                "neo4j",
                "workday",
                "mcp",
                "api",
                "dashboard",
            )
        )

    @staticmethod
    def _communication_style(memories: list[MemoryRecord]) -> list[str]:
        text = "\n".join(memory.content for memory in memories).lower()
        styles = []
        for label in ("direct", "diplomatic", "technical", "concise", "management-focused"):
            if label in text:
                styles.append(label)
        return styles or ["source-grounded", "practical", "concise"]

    @staticmethod
    def _escalation_style(memories: list[MemoryRecord]) -> str:
        text = "\n".join(memory.content for memory in memories).lower()
        if "escalat" in text:
            return "Escalate according to the source-backed thresholds and stakeholders."
        return "Escalate uncertainty, missing evidence, and high-risk actions for human review."


__all__ = ["PersonaDistillationError", "PersonaFactoryService", "PersonaPublishError"]
