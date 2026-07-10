"""Deterministic Persona Factory extraction pipeline.

Agency uses Persona as the product/API term. Other ecosystems may call the
same reusable package a skill; this pipeline creates the structured records
that eventually become that persona/skill-equivalent package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.domain import (
    MemoryRecord,
    MemoryType,
    PersonaDistillationItemType,
    PersonaMemoryLayer,
)

SOURCE_CLASSIFICATIONS = {
    "policy_sop",
    "decision",
    "example",
    "conversation",
    "workflow",
    "tool_usage",
    "personal_writing_style",
    "domain_knowledge",
}

TOOL_NAMES = (
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
    "slack",
    "teams",
)

DISTILLER_VERSION = "specialized-distillers-v1"


@dataclass(slots=True)
class PersonaSourceClassification:
    label: str
    confidence: float
    signals: list[str] = field(default_factory=list)
    document_kind: str = "unknown"
    content_roles: list[str] = field(default_factory=list)
    extraction_targets: list[str] = field(default_factory=list)
    memory_layers: list[str] = field(default_factory=list)
    vector_tags: list[str] = field(default_factory=list)
    graph_entities: list[dict[str, Any]] = field(default_factory=list)
    graph_relationships: list[dict[str, Any]] = field(default_factory=list)
    should_include: bool = True
    rationale: str | None = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "label": self.label,
            "confidence": self.confidence,
            "signals": self.signals,
            "document_kind": self.document_kind,
            "content_roles": self.content_roles,
            "extraction_targets": self.extraction_targets,
            "memory_layers": self.memory_layers,
            "vector_tags": self.vector_tags,
            "should_include": self.should_include,
        }
        if self.graph_entities:
            payload["graph_entities"] = self.graph_entities
        if self.graph_relationships:
            payload["graph_relationships"] = self.graph_relationships
        if self.rationale:
            payload["rationale"] = self.rationale
        return payload


@dataclass(slots=True)
class PersonaDistillationCandidate:
    item_type: PersonaDistillationItemType
    memory_layer: PersonaMemoryLayer
    title: str
    content: str
    confidence: float
    structured_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    needs_review: bool = True
    distiller_name: str = ""


class PersonaDistillationPipeline:
    """Classify source chunks and extract many small persona memory candidates."""

    def classify(self, memory: MemoryRecord) -> PersonaSourceClassification:
        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
        metadata_classification = self._metadata_source_classification(metadata)
        if metadata_classification is not None:
            return metadata_classification
        upload_classification = self._upload_intelligence_classification(metadata)
        if upload_classification is not None:
            return upload_classification
        explicit = str(metadata.get("persona_source_type") or metadata.get("source_classification") or "").strip()
        if explicit in SOURCE_CLASSIFICATIONS:
            return PersonaSourceClassification(explicit, 0.9, ["metadata"])

        tags = {tag.lower() for tag in memory.tags}
        text = self._memory_text(memory)
        signals: list[str] = []

        if memory.memory_type == MemoryType.DECISION or "decision" in tags:
            signals.append("memory_type:decision")
            return PersonaSourceClassification("decision", 0.9, signals)
        # Writing preference notes often arrive as archive uploads with hyphenated tags,
        # so route explicit preference language before generic conversation/domain fallbacks.
        if (
                memory.memory_type == MemoryType.PREFERENCE
                or "preference" in tags
                or "writing-style" in tags
                or self._has_any(text, ("writing preference", "communication preference"))
        ):
            signals.append("memory_type:preference")
            return PersonaSourceClassification("personal_writing_style", 0.85, signals)
        if "conversation" in tags or any(token in text for token in (" said:", " wrote:", "user:", "assistant:")):
            signals.append("conversation_markers")
            return PersonaSourceClassification("conversation", 0.75, signals)
        if self._has_any(text, ("workflow", "lifecycle", "handoff", "sequence", "step", "checklist")):
            signals.append("workflow_keywords")
            return PersonaSourceClassification("workflow", 0.82, signals)
        if self._has_any(text, ("policy", "standard", "sop", "procedure", "must", "approval")):
            signals.append("policy_keywords")
            return PersonaSourceClassification("policy_sop", 0.8, signals)
        if self._has_tool_any(text, TOOL_NAMES):
            signals.append("tool_keywords")
            return PersonaSourceClassification("tool_usage", 0.78, signals)
        if self._has_word_any(text, ("example", "sample", "draft", "template")):
            signals.append("example_keywords")
            return PersonaSourceClassification("example", 0.78, signals)
        if self._has_any(text, ("tone", "style", "phrasing", "writes", "responds")):
            signals.append("style_keywords")
            return PersonaSourceClassification("personal_writing_style", 0.78, signals)
        return PersonaSourceClassification("domain_knowledge", 0.7, ["fallback"])

    def extract(
            self,
            *,
            memory: MemoryRecord,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
    ) -> list[PersonaDistillationCandidate]:
        distillers = (
            ("decision_distiller", self._decision_distiller),
            ("workflow_distiller", self._workflow_distiller),
            ("persona_style_distiller", self._persona_style_distiller),
            ("tool_distiller", self._tool_distiller),
            ("guardrail_distiller", self._guardrail_distiller),
            ("example_distiller", self._example_distiller),
            ("knowledge_distiller", self._knowledge_distiller),
        )
        candidates: list[PersonaDistillationCandidate] = []
        for distiller_name, distiller in distillers:
            for candidate in distiller(memory=memory, source_ref=source_ref, classification=classification):
                candidate.distiller_name = distiller_name
                candidate.structured_payload["distiller"] = distiller_name
                candidate.metadata["distiller"] = distiller_name
                candidates.append(candidate)
        return self.normalize(candidates)

    def normalize(self, candidates: Iterable[PersonaDistillationCandidate]) -> list[PersonaDistillationCandidate]:
        merged: dict[tuple[str, str, str], PersonaDistillationCandidate] = {}
        for candidate in candidates:
            content = self._clean(candidate.content)
            title = self._clean(candidate.title)[:120]
            if not title or not content:
                continue
            candidate.title = title
            candidate.content = content
            candidate.confidence = max(min(float(candidate.confidence), 1.0), 0.0)
            key = (candidate.item_type.value, candidate.memory_layer.value, content.lower())
            existing = merged.get(key)
            if existing is None:
                merged[key] = candidate
                continue
            existing.confidence = max(existing.confidence, candidate.confidence)
            existing.needs_review = existing.needs_review or candidate.needs_review
            existing.structured_payload = self._merge_payloads(existing.structured_payload,
                                                               candidate.structured_payload)
        return list(merged.values())

    def _decision_distiller(
            self,
            *,
            memory: MemoryRecord,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
    ) -> list[PersonaDistillationCandidate]:
        text_units = self._units(memory.content)
        if (
                classification.label == "personal_writing_style"
                and memory.memory_type != MemoryType.DECISION
                and not self._targets(classification, "decision_pattern")
        ):
            return []
        decision_units = [
            unit for unit in text_units
            if
            self._has_any(unit.lower(), ("if ", "when ", "should ", "must ", "risk", "escalat", "approve", "threshold"))
        ]
        if self._targets(classification, "decision_pattern") and not decision_units:
            decision_units = text_units[:2]
        if memory.memory_type == MemoryType.DECISION and not decision_units:
            decision_units = text_units[:3]
        return [
            self._candidate(
                PersonaDistillationItemType.DECISION_PATTERN,
                PersonaMemoryLayer.PROCEDURAL,
                (
                    memory.summary
                    if memory.memory_type == MemoryType.DECISION and memory.summary
                    else self._title("Decision rule", unit)
                ),
                unit,
                0.86 if classification.label == "decision" else 0.78,
                source_ref,
                classification,
                {"rule": unit},
            )
            for unit in decision_units[:6]
        ]

    def _workflow_distiller(
            self,
            *,
            memory: MemoryRecord,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
    ) -> list[PersonaDistillationCandidate]:
        text = memory.content
        lowered = text.lower()
        if not self._has_any(lowered, ("workflow", "lifecycle", "process", "procedure", "sop", "checklist", "step")) \
                and not self._targets(classification, "workflow", "procedure"):
            return []
        steps = self._steps(text)
        payload = {
            "steps": steps,
            "triggers": self._workflow_field_units(text, ("trigger", "starts when", "begins when", "when ")),
            "owners": self._workflow_field_units(text, ("owner", "responsible", "assigned to", "reviewer")),
            "inputs": self._workflow_field_units(text, ("input", "requires", "evidence", "ticket")),
            "outputs": self._workflow_field_units(text, ("output", "produces", "deliverable", "record")),
            "failure_paths": self._workflow_field_units(text, ("if ", "fails", "blocked", "fallback", "escalate")),
        }
        content = "\n".join(f"{index + 1}. {step}" for index, step in enumerate(steps)) if steps else text
        item_type = (
            PersonaDistillationItemType.WORKFLOW
            if self._has_any(lowered, ("workflow", "lifecycle", "handoff", "sequence"))
               or self._targets(classification, "workflow")
            else PersonaDistillationItemType.PROCEDURE
        )
        return [
            self._candidate(
                item_type,
                PersonaMemoryLayer.PROCEDURAL,
                self._title("Workflow", content),
                content,
                0.86 if classification.label in {"workflow", "policy_sop"} else 0.76,
                source_ref,
                classification,
                payload,
            )
        ]

    def _persona_style_distiller(
            self,
            *,
            memory: MemoryRecord,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
    ) -> list[PersonaDistillationCandidate]:
        text = memory.content
        lowered = text.lower()
        if memory.memory_type != MemoryType.PREFERENCE and not self._has_any(
                lowered,
                ("tone", "style", "phrasing", "writes", "responds", "communication", "voice"),
        ) and not self._targets(classification, "writing_style"):
            return []
        return [
            self._candidate(
                PersonaDistillationItemType.WRITING_STYLE,
                PersonaMemoryLayer.PERSONA,
                self._title("Writing style", text),
                text,
                0.84 if classification.label == "personal_writing_style" else 0.74,
                source_ref,
                classification,
                {"style_signals": self._style_signals(lowered)},
            )
        ]

    def _tool_distiller(
            self,
            *,
            memory: MemoryRecord,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
    ) -> list[PersonaDistillationCandidate]:
        text = memory.content
        lowered = text.lower()
        tools = [tool for tool in TOOL_NAMES if self._has_tool(lowered, tool)]
        return [
            self._candidate(
                PersonaDistillationItemType.TOOL_USAGE,
                PersonaMemoryLayer.TOOL,
                tool.title() if tool != "api" else "API",
                self._tool_context(text, tool),
                0.82 if classification.label == "tool_usage" else 0.72,
                source_ref,
                classification,
                {"tool_id": tool, "tool_name": tool},
            )
            for tool in tools[:8]
        ]

    def _guardrail_distiller(
            self,
            *,
            memory: MemoryRecord,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
    ) -> list[PersonaDistillationCandidate]:
        units = [
            unit for unit in self._units(memory.content)
            if
            self._has_any(unit.lower(), ("must not", "never", "approval", "policy", "private", "consent", "regulated"))
        ]
        if self._targets(classification, "guardrail") and not units:
            units = self._units(memory.content)[:2]
        return [
            self._candidate(
                PersonaDistillationItemType.GUARDRAIL,
                PersonaMemoryLayer.SEMANTIC,
                self._title("Guardrail", unit),
                unit,
                0.84 if classification.label == "policy_sop" else 0.76,
                source_ref,
                classification,
                {"constraint": unit},
            )
            for unit in units[:6]
        ]

    def _example_distiller(
            self,
            *,
            memory: MemoryRecord,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
    ) -> list[PersonaDistillationCandidate]:
        text = memory.content
        lowered = text.lower()
        if not self._has_word_any(lowered, ("example", "sample", "draft", "template")) \
                and not self._targets(classification, "example"):
            return []
        return [
            self._candidate(
                PersonaDistillationItemType.EXAMPLE,
                PersonaMemoryLayer.EPISODIC,
                self._title("Example", text),
                text,
                0.84 if classification.label == "example" else 0.74,
                source_ref,
                classification,
                {"artifact_pattern": self._example_kind(lowered)},
            )
        ]

    def _knowledge_distiller(
            self,
            *,
            memory: MemoryRecord,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
    ) -> list[PersonaDistillationCandidate]:
        if classification.label in {"decision", "workflow", "personal_writing_style", "tool_usage", "example"} \
                and not self._targets(classification, "domain_knowledge"):
            return []
        units = self._units(memory.content)
        if not units:
            units = [memory.content]
        knowledge_units = [
            unit for unit in units
            if not self._has_any(unit.lower(), ("example", "sample", "writes", "tone", "style"))
        ]
        if not knowledge_units:
            knowledge_units = units[:1]
        return [
            self._candidate(
                PersonaDistillationItemType.DOMAIN_KNOWLEDGE,
                PersonaMemoryLayer.SEMANTIC,
                self._title("Knowledge", unit),
                unit,
                0.8 if classification.label in {"domain_knowledge", "policy_sop"} else 0.72,
                source_ref,
                classification,
                {"fact": unit},
            )
            for unit in knowledge_units[:5]
        ]

    def _candidate(
            self,
            item_type: PersonaDistillationItemType,
            memory_layer: PersonaMemoryLayer,
            title: str,
            content: str,
            confidence: float,
            source_ref: dict[str, Any],
            classification: PersonaSourceClassification,
            payload: dict[str, Any],
    ) -> PersonaDistillationCandidate:
        identity_signals = self._identity_claim_signals(f"{title}\n{content}")
        needs_review = confidence < 0.8 or classification.confidence < 0.8 or bool(identity_signals)
        review_reasons: list[str] = []
        if confidence < 0.8:
            review_reasons.append("low_extraction_confidence")
        if classification.confidence < 0.8:
            review_reasons.append("low_classification_confidence")
        if identity_signals:
            review_reasons.append("personal_identity_claim")
        return PersonaDistillationCandidate(
            item_type=item_type,
            memory_layer=memory_layer,
            title=title,
            content=content,
            confidence=confidence,
            needs_review=needs_review,
            structured_payload={
                **payload,
                "source_ref": source_ref,
                "source_classification": classification.as_payload(),
                "extractor": "deterministic-multi-distiller-v1",
                "distiller": "",
                "distiller_version": DISTILLER_VERSION,
                "routing": self._routing_payload(classification),
                "review_flags": review_reasons,
                "identity_claim_signals": identity_signals,
            },
            metadata={
                "source_classification": classification.label,
                "classification_confidence": classification.confidence,
                "review_reasons": review_reasons,
                "personal_identity_claim": bool(identity_signals),
                "distiller_version": DISTILLER_VERSION,
                "distiller": "",
            },
        )

    @staticmethod
    def _metadata_source_classification(metadata: dict[str, Any]) -> PersonaSourceClassification | None:
        source_intelligence = metadata.get("source_intelligence")
        if not isinstance(source_intelligence, dict):
            return None
        raw = source_intelligence.get("classification")
        if not isinstance(raw, dict):
            return None
        label = str(raw.get("label") or "").strip()
        if label not in SOURCE_CLASSIFICATIONS:
            return None
        return PersonaSourceClassification(
            label=label,
            confidence=PersonaDistillationPipeline._float(raw.get("confidence"), 0.85),
            signals=["metadata:source_intelligence", *PersonaDistillationPipeline._string_list(raw.get("signals"))],
            document_kind=str(raw.get("document_kind") or "unknown"),
            content_roles=PersonaDistillationPipeline._string_list(raw.get("content_roles")),
            extraction_targets=PersonaDistillationPipeline._string_list(raw.get("extraction_targets")),
            memory_layers=PersonaDistillationPipeline._string_list(raw.get("memory_layers")),
            vector_tags=PersonaDistillationPipeline._string_list(raw.get("vector_tags")),
            graph_entities=PersonaDistillationPipeline._dict_list(raw.get("graph_entities")),
            graph_relationships=PersonaDistillationPipeline._dict_list(raw.get("graph_relationships")),
            should_include=bool(raw.get("should_include", True)),
            rationale=str(raw.get("rationale")) if raw.get("rationale") else None,
        )

    @staticmethod
    def _upload_intelligence_classification(metadata: dict[str, Any]) -> PersonaSourceClassification | None:
        upload_intelligence = metadata.get("upload_intelligence")
        if not isinstance(upload_intelligence, dict):
            return None
        document_kind = str(upload_intelligence.get("document_kind") or "unknown").strip()
        label_by_kind = {
            "policy_sop": "policy_sop",
            "workpaper": "domain_knowledge",
            "report": "domain_knowledge",
            "email_thread": "conversation",
            "chat_export": "conversation",
            "ticket": "workflow",
            "meeting_note": "conversation",
            "code": "domain_knowledge",
        }
        label = label_by_kind.get(document_kind)
        if label is None:
            return None
        recommended = upload_intelligence.get("recommended")
        tags = []
        if isinstance(recommended, dict):
            tags = PersonaDistillationPipeline._string_list(recommended.get("tags"))
        return PersonaSourceClassification(
            label=label,
            confidence=PersonaDistillationPipeline._float(upload_intelligence.get("confidence"), 0.78),
            signals=["metadata:upload_intelligence"],
            document_kind=document_kind,
            content_roles=[label],
            extraction_targets=PersonaDistillationPipeline._targets_for_label(label, document_kind=document_kind),
            memory_layers=PersonaDistillationPipeline._layers_for_label(label),
            vector_tags=tags,
            rationale=str(upload_intelligence.get("rationale")) if upload_intelligence.get("rationale") else None,
        )

    @staticmethod
    def _targets_for_label(label: str, *, document_kind: str = "unknown") -> list[str]:
        if label == "policy_sop":
            return ["domain_knowledge", "workflow", "guardrail"]
        if label == "workflow":
            return ["workflow", "decision_pattern"]
        if label == "decision":
            return ["decision_pattern", "domain_knowledge"]
        if label == "conversation":
            return ["writing_style", "example"]
        if label == "tool_usage":
            return ["tool_usage"]
        if label == "example":
            return ["example"]
        if document_kind == "workpaper":
            return ["domain_knowledge", "decision_pattern", "example"]
        return ["domain_knowledge"]

    @staticmethod
    def _layers_for_label(label: str) -> list[str]:
        if label in {"workflow", "policy_sop", "decision"}:
            return ["procedural", "semantic"]
        if label == "conversation":
            return ["persona", "episodic"]
        if label == "tool_usage":
            return ["tool"]
        if label == "example":
            return ["episodic"]
        return ["semantic"]

    @staticmethod
    def _targets(classification: PersonaSourceClassification, *targets: str) -> bool:
        requested = {item.strip() for item in classification.extraction_targets if item.strip()}
        roles = {item.strip() for item in classification.content_roles if item.strip()}
        return bool(requested.intersection(targets) or roles.intersection(targets))

    @staticmethod
    def _routing_payload(classification: PersonaSourceClassification) -> dict[str, Any]:
        return {
            "label": classification.label,
            "document_kind": classification.document_kind,
            "content_roles": classification.content_roles,
            "extraction_targets": classification.extraction_targets,
            "memory_layers": classification.memory_layers,
            "vector_tags": classification.vector_tags,
        }

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    @staticmethod
    def _dict_list(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _float(value: Any, default: float) -> float:
        try:
            return max(min(float(value), 1.0), 0.0)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _units(content: str) -> list[str]:
        normalized = content.replace("\r\n", "\n")
        raw_units = re.split(r"(?:\n\s*[-*]\s+|\n\s*\d+[.)]\s+|\n{2,}|(?<=[.!?])\s+)", normalized)
        return [PersonaDistillationPipeline._clean(unit) for unit in raw_units if
                PersonaDistillationPipeline._clean(unit)]

    @staticmethod
    def _steps(content: str) -> list[str]:
        numbered = re.findall(r"(?:^|\n)\s*(?:[-*]|\d+[.)])\s+(.+?)(?=\n\s*(?:[-*]|\d+[.)])\s+|\Z)", content, re.S)
        steps = [PersonaDistillationPipeline._clean(step) for step in numbered if
                 PersonaDistillationPipeline._clean(step)]
        if steps:
            return steps[:12]
        text = content.replace(" then ", "\n").replace(" -> ", "\n").replace("→", "\n")
        return PersonaDistillationPipeline._units(text)[:12]

    @staticmethod
    def _workflow_field_units(content: str, markers: Iterable[str]) -> list[str]:
        matches: list[str] = []
        lowered_markers = tuple(marker.lower() for marker in markers)
        for unit in PersonaDistillationPipeline._units(content):
            lowered = unit.lower()
            if any(marker in lowered for marker in lowered_markers):
                matches.append(unit)
        return matches[:6]

    @staticmethod
    def _memory_text(memory: MemoryRecord) -> str:
        return f"{memory.summary or ''}\n{memory.content}".lower()

    @staticmethod
    def _clean(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _has_any(text: str, needles: Iterable[str]) -> bool:
        return any(needle in text for needle in needles)

    @staticmethod
    def _title(prefix: str, content: str) -> str:
        text = PersonaDistillationPipeline._clean(content)
        return f"{prefix}: {text[:90]}" if text else prefix

    @staticmethod
    def _style_signals(text: str) -> list[str]:
        return [label for label in ("direct", "diplomatic", "formal", "concise", "technical", "caring") if
                label in text]

    @staticmethod
    def _tool_context(text: str, tool: str) -> str:
        for unit in PersonaDistillationPipeline._units(text):
            if PersonaDistillationPipeline._has_tool(unit.lower(), tool):
                return unit
        return text

    @staticmethod
    def _example_kind(text: str) -> str:
        for label in ("observation", "email", "report", "template", "draft"):
            if re.search(rf"\b{re.escape(label)}\b", text):
                return label
        return "example"

    @staticmethod
    def _identity_claim_signals(text: str) -> list[str]:
        lowered = text.lower()
        patterns = {
            "family_relationship": r"\b(my|your|his|her|their)\s+(mother|father|parent|sibling|brother|sister|son|daughter|child|wife|husband|spouse|partner)\b",
            "intimate_relationship": r"\b(my|your|his|her|their)\s+(ex[- ]?girlfriend|ex[- ]?boyfriend|girlfriend|boyfriend|lover|fiancee?|romantic partner)\b",
            "self_identity": r"\b(i am|i'm|this is me|represents me|acts as me|pretend to be me)\b",
            "actual_person_claim": r"\b(actual person|real person|digital clone|clone of|copy of|simulate\s+(my|your|his|her|their)\s+)\b",
            "private_person_name": r"\b(persona|clone|simulation)\s+(of|for)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b",
        }
        return [label for label, pattern in patterns.items() if
                re.search(pattern, text if label == "private_person_name" else lowered)]

    @staticmethod
    def _has_word_any(text: str, needles: Iterable[str]) -> bool:
        return any(re.search(rf"\b{re.escape(needle)}\b", text) for needle in needles)

    @staticmethod
    def _has_tool_any(text: str, tools: Iterable[str]) -> bool:
        return any(PersonaDistillationPipeline._has_tool(text, tool) for tool in tools)

    @staticmethod
    def _has_tool(text: str, tool: str) -> bool:
        return bool(re.search(rf"\b{re.escape(tool)}\b", text))

    @staticmethod
    def _merge_payloads(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
        merged = dict(first)
        for key, value in second.items():
            if key == "source_ref":
                refs = merged.setdefault("source_refs", [])
                if isinstance(merged.get("source_ref"), dict):
                    refs.append(merged.pop("source_ref"))
                if isinstance(value, dict) and value not in refs:
                    refs.append(value)
                continue
            if key not in merged:
                merged[key] = value
        return merged


__all__ = [
    "PersonaDistillationCandidate",
    "PersonaDistillationPipeline",
    "PersonaSourceClassification",
    "SOURCE_CLASSIFICATIONS",
]
