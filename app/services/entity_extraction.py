"""Deterministic entity extraction boundary for Agency Graph projection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

ENTITY_EXTRACTOR_VERSION = "deterministic-memory-entity-v1"
ENTITY_TAG_PREFIX = "entity:"
DEFAULT_ENTITY_TYPE = "concept"


@dataclass(frozen=True, slots=True)
class EntityCandidate:
    id: str
    name: str
    normalized_name: str
    entity_type: str
    confidence: float
    source_fields: list[str]

    def to_projection_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "normalized_name": self.normalized_name,
            "entity_type": self.entity_type,
            "confidence": self.confidence,
            "source_fields": self.source_fields,
            "extractor_version": ENTITY_EXTRACTOR_VERSION,
        }


class MemoryEntityExtractor:
    """Extract graph-safe entity candidates from allowlisted memory projection fields.

    This intentionally avoids raw memory content. The first boundary supports
    explicit metadata hints, `entity:<name>` tags, and conservative summary
    phrase extraction. LLM/entity-linking providers can be introduced behind
    this contract later.
    """

    def extract(self, payload: dict[str, Any], *, min_confidence: float = 0.7) -> list[EntityCandidate]:
        candidates: dict[str, EntityCandidate] = {}
        for candidate in [
            *self._metadata_candidates(payload),
            *self._tag_candidates(payload),
            *self._summary_candidates(payload),
        ]:
            if candidate.confidence < min_confidence:
                continue
            existing = candidates.get(candidate.id)
            if existing is None or candidate.confidence > existing.confidence:
                candidates[candidate.id] = candidate
        return sorted(candidates.values(), key=lambda item: (item.entity_type, item.normalized_name))

    def _metadata_candidates(self, payload: dict[str, Any]) -> list[EntityCandidate]:
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return []
        raw_entities = metadata.get("entity_hints") or metadata.get("entities")
        if not isinstance(raw_entities, list):
            return []
        candidates: list[EntityCandidate] = []
        for item in raw_entities:
            if isinstance(item, str):
                candidate = self._candidate(item, source_field="metadata.entity_hints", confidence=0.85)
            elif isinstance(item, dict):
                candidate = self._candidate(
                    item.get("name"),
                    entity_type=item.get("type") or item.get("entity_type"),
                    source_field="metadata.entity_hints",
                    confidence=item.get("confidence", 0.9),
                )
            else:
                candidate = None
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _tag_candidates(self, payload: dict[str, Any]) -> list[EntityCandidate]:
        tags = payload.get("tags")
        if not isinstance(tags, list):
            return []
        candidates = []
        for tag in tags:
            if not isinstance(tag, str) or not tag.lower().startswith(ENTITY_TAG_PREFIX):
                continue
            candidates.append(self._candidate(tag[len(ENTITY_TAG_PREFIX):], source_field="tags", confidence=0.75))
        return [candidate for candidate in candidates if candidate is not None]

    def _summary_candidates(self, payload: dict[str, Any]) -> list[EntityCandidate]:
        if payload.get("sensitive"):
            return []
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return []
        candidates = []
        for match in re.finditer(r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){1,3})\b", summary):
            candidates.append(self._candidate(match.group(1), source_field="summary", confidence=0.65))
        return [candidate for candidate in candidates if candidate is not None]

    def _candidate(
            self,
            name: Any,
            *,
            entity_type: Any = None,
            source_field: str,
            confidence: Any,
    ) -> EntityCandidate | None:
        normalized_name = normalize_entity_name(name)
        if not normalized_name:
            return None
        normalized_type = normalize_entity_type(entity_type)
        try:
            normalized_confidence = float(confidence)
        except (TypeError, ValueError):
            normalized_confidence = 0.0
        normalized_confidence = max(0.0, min(normalized_confidence, 1.0))
        return EntityCandidate(
            id=f"entity:{normalized_type}:{slugify_entity(normalized_name)}",
            name=str(name).strip(),
            normalized_name=normalized_name,
            entity_type=normalized_type,
            confidence=normalized_confidence,
            source_fields=[source_field],
        )


def normalize_entity_name(value: Any) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    return normalized[:160]


def normalize_entity_type(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or DEFAULT_ENTITY_TYPE).lower()).strip("_")
    return normalized or DEFAULT_ENTITY_TYPE


def slugify_entity(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


__all__ = [
    "ENTITY_EXTRACTOR_VERSION",
    "EntityCandidate",
    "MemoryEntityExtractor",
    "normalize_entity_name",
    "normalize_entity_type",
    "slugify_entity",
]
