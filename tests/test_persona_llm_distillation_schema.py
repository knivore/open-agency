from __future__ import annotations

import asyncio
import json
import unittest

from pydantic import ValidationError

from app.domain import MemoryRecord, ModelProfileDefinition
from app.services.persona_distillation_pipeline import PersonaSourceClassification
from app.services.persona_llm_distillation import (
    LLMDistillationEngine,
    LLM_DISTILLATION_EXTRACTOR,
    LLM_MAX_CONTENT_CHARS,
    LLM_MAX_EVIDENCE_CHARS,
    LLM_MAX_STRUCTURED_PAYLOAD_BYTES,
    LLM_MAX_TITLE_CHARS,
    PersonaLLMDistillationCandidatePayload,
    PersonaLLMDistillationError,
)
from app.services.source_intelligence import SourceIntelligencePayload


class PersonaLLMDistillationSchemaTests(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            "item_type": "decision_pattern",
            "memory_layer": "procedural",
            "title": "Escalate incomplete access review scope",
            "content": "When privileged access review excludes administrators, escalate the audit observation.",
            "confidence": 0.87,
            "source_evidence": "privileged access review excludes administrators",
            "source_span": {"start": 5, "end": 54},
            "review_reasons": ["source_backed"],
            "structured_payload": {"rule": "escalate incomplete access review scope"},
        }
        payload.update(overrides)
        return payload

    def test_valid_candidate_converts_to_reviewable_distillation_candidate(self) -> None:
        candidate = PersonaLLMDistillationCandidatePayload.model_validate(
            self._payload(
                suggested_graph_entities=[
                    {
                        "label": "Workflow",
                        "name": "Access Review",
                        "confidence": 0.82,
                        "evidence": "access review excludes administrators",
                    }
                ],
                suggested_graph_relationships=[
                    {
                        "source_name": "Access Review",
                        "relationship_type": "ESCALATES_TO",
                        "target_name": "Audit Observation",
                        "confidence": 0.79,
                    }
                ],
            )
        )

        converted = candidate.to_distillation_candidate(
            source_ref={"memory_id": "memory-1"},
            source_memory_id="memory-1",
            model_provider="persona_fake",
            model_name="fake-structured",
            model_profile_id="profile-1",
            prompt_version="persona-llm-distill-v1",
            distiller_name="llm_decision_distiller",
            distiller_version="llm-distillers-v1",
        )

        self.assertEqual(converted.item_type.value, "decision_pattern")
        self.assertEqual(converted.memory_layer.value, "procedural")
        self.assertFalse(converted.needs_review)
        self.assertEqual(converted.structured_payload["extractor"], LLM_DISTILLATION_EXTRACTOR)
        self.assertEqual(converted.structured_payload["source_ref"]["memory_id"], "memory-1")
        self.assertEqual(converted.structured_payload["source_span"], {"start": 5, "end": 54})
        self.assertEqual(converted.structured_payload["provenance"]["model_provider"], "persona_fake")
        self.assertEqual(converted.metadata["source_memory_id"], "memory-1")
        self.assertEqual(len(converted.metadata["source_evidence_hash"]), 64)
        self.assertTrue(converted.metadata["has_graph_hints"])

    def test_rejects_unsupported_item_type_and_memory_layer(self) -> None:
        with self.assertRaises(ValidationError):
            PersonaLLMDistillationCandidatePayload.model_validate(
                self._payload(item_type="unsupported", memory_layer="nowhere")
            )

    def test_requires_source_evidence_and_source_span_field(self) -> None:
        missing_evidence = self._payload()
        missing_evidence.pop("source_evidence")
        with self.assertRaises(ValidationError):
            PersonaLLMDistillationCandidatePayload.model_validate(missing_evidence)

        missing_span = self._payload()
        missing_span.pop("source_span")
        with self.assertRaises(ValidationError):
            PersonaLLMDistillationCandidatePayload.model_validate(missing_span)

    def test_marks_weak_evidence_missing_span_and_risk_for_review(self) -> None:
        candidate = PersonaLLMDistillationCandidatePayload.model_validate(
            self._payload(
                source_evidence="short",
                source_span=None,
                unsupported_claim_risk=0.7,
                conflict_signals=["conflicts_with_policy_sop"],
            )
        )

        self.assertTrue(candidate.needs_review)
        self.assertIn("weak_source_evidence", candidate.review_reasons)
        self.assertIn("missing_source_span", candidate.review_reasons)
        self.assertIn("unsupported_claim_risk", candidate.review_reasons)
        self.assertIn("conflict_signals", candidate.review_reasons)

    def test_verified_text_match_clears_missing_source_span_review_reason(self) -> None:
        candidate = PersonaLLMDistillationCandidatePayload.model_validate(
            self._payload(
                source_evidence="Evidence workflow: link the Jira ticket, attach the workpaper.",
                source_span=None,
                review_reasons=[],
            )
        )

        grounding = candidate.ground_source_evidence(
            "Evidence workflow: link the Jira ticket, attach the workpaper."
        )

        self.assertTrue(grounding["verified"])
        self.assertEqual(grounding["match_method"], "exact_text")
        self.assertNotIn("missing_source_span", candidate.review_reasons)
        self.assertFalse(candidate.needs_review)

    def test_rejects_invalid_span(self) -> None:
        with self.assertRaises(ValidationError):
            PersonaLLMDistillationCandidatePayload.model_validate(
                self._payload(source_span={"start": 10, "end": 10})
            )

    def test_normalizes_llm_graph_entity_label_drift(self) -> None:
        candidate = PersonaLLMDistillationCandidatePayload.model_validate(
            self._payload(
                suggested_graph_entities=[
                    {"label": "WritingStylePreference", "name": "Concise recommendations"},
                    {"label": "EvidenceTicket", "name": "Jira evidence ticket"},
                    {"label": "Unknown", "name": "Access Review"},
                ]
            )
        )

        self.assertEqual(
            [entity.label for entity in candidate.suggested_graph_entities],
            ["Persona", "Artifact", "Knowledge"],
        )

    def test_normalizes_llm_graph_relationship_verb_drift(self) -> None:
        candidate = PersonaLLMDistillationCandidatePayload.model_validate(
            self._payload(
                suggested_graph_relationships=[
                    {
                        "source_name": "Evidence workpaper",
                        "relationship_type": "VALIDATES",
                        "target_name": "Owner response",
                    }
                ]
            )
        )

        self.assertEqual(candidate.suggested_graph_relationships[0].relationship_type, "RELATES_TO")

    def test_source_intelligence_drops_unsupported_content_roles(self) -> None:
        payload = SourceIntelligencePayload.model_validate(
            {
                "label": "personal_writing_style",
                "confidence": 0.91,
                "signals": ["llm_classifier"],
                "document_kind": "chat_export",
                "content_roles": ["user_preference", "personal_writing_style", "style_guidance"],
                "extraction_targets": ["writing_style"],
                "memory_layers": ["episodic"],
                "vector_tags": ["writing"],
                "graph_entities": [
                    {"label": "WritingStylePreference", "name": "Concise recommendations"},
                ],
                "graph_relationships": [
                    {
                        "source_name": "Writing preference",
                        "relationship_type": "TRACKS",
                        "target_name": "Persona style",
                    }
                ],
                "should_include": True,
            }
        )

        self.assertEqual(payload.content_roles, ["personal_writing_style"])
        self.assertEqual(payload.graph_entities[0].label, "Persona")
        self.assertEqual(payload.graph_relationships[0].relationship_type, "RELATES_TO")

    def test_rejects_oversized_llm_outputs(self) -> None:
        oversized_cases = [
            {"title": "x" * (LLM_MAX_TITLE_CHARS + 1)},
            {"content": "x" * (LLM_MAX_CONTENT_CHARS + 1)},
            {"source_evidence": "x" * (LLM_MAX_EVIDENCE_CHARS + 1)},
            {"structured_payload": {"blob": "x" * (LLM_MAX_STRUCTURED_PAYLOAD_BYTES + 1)}},
        ]

        for overrides in oversized_cases:
            with self.subTest(overrides=list(overrides)):
                with self.assertRaises(ValidationError):
                    PersonaLLMDistillationCandidatePayload.model_validate(self._payload(**overrides))


class PersonaLLMSpecializedDistillerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = LLMDistillationEngine()
        self.memory = MemoryRecord(
            id="llm-fixture-memory",
            scope="user",
            created_by_user_id="user-1",
            content=(
                "Source evidence phrase. Tone: concise and diplomatic. "
                "Use Jira for audit evidence tracking. If evidence is missing, escalate."
            ),
            summary="Persona distillation fixture",
        )
        self.profile = ModelProfileDefinition(
            id="profile-1",
            name="Persona Factory Fake",
            provider="persona_fake",
            model="fake-structured",
            supports_structured_output=True,
        )

    def test_routes_each_extraction_target_to_specialized_distiller(self) -> None:
        cases = [
            ("domain_knowledge", "llm_knowledge_distiller"),
            ("workflow", "llm_workflow_distiller"),
            ("procedure", "llm_workflow_distiller"),
            ("decision_pattern", "llm_decision_distiller"),
            ("writing_style", "llm_writing_style_distiller"),
            ("tool_usage", "llm_tool_usage_distiller"),
            ("guardrail", "llm_guardrail_distiller"),
            ("example", "llm_example_distiller"),
            ("social_context", "llm_social_context_distiller"),
        ]

        for target, expected_distiller in cases:
            with self.subTest(target=target):
                prompts: list[dict] = []

                async def generate_structured(**kwargs):
                    prompt = json.loads(kwargs["prompt"])
                    prompts.append(prompt)
                    return {
                        "candidates": [
                            {
                                "item_type": prompt["allowed_item_types"][0],
                                "memory_layer": prompt["allowed_memory_layers"][0],
                                "title": f"{target} candidate",
                                "content": "Source evidence phrase.",
                                "confidence": 0.86,
                                "source_evidence": "Source evidence phrase",
                                "source_span": {"start": 0, "end": 22},
                                "review_reasons": ["source_backed"],
                                "structured_payload": {},
                            }
                        ]
                    }

                candidates = asyncio.run(
                    self.engine.extract_source(
                        memory=self.memory,
                        source_ref={"memory_id": self.memory.id},
                        classification=PersonaSourceClassification(
                            label="domain_knowledge",
                            confidence=0.95,
                            extraction_targets=[target],
                        ),
                        model_profile=self.profile,
                        generate_structured=generate_structured,
                    )
                )

                self.assertEqual(prompts[0]["distiller"], expected_distiller)
                self.assertEqual(candidates[0].metadata["distiller"], expected_distiller)
                self.assertEqual(candidates[0].structured_payload["source_ref"]["memory_id"], self.memory.id)
                self.assertEqual(candidates[0].structured_payload["source_evidence"], "Source evidence phrase")

    def test_multi_target_classification_uses_broad_distiller(self) -> None:
        prompts: list[dict] = []

        async def generate_structured(**kwargs):
            prompt = json.loads(kwargs["prompt"])
            prompts.append(prompt)
            return {
                "candidates": [
                    {
                        "item_type": "workflow",
                        "memory_layer": "procedural",
                        "title": "Evidence workflow",
                        "content": "Use Jira for audit evidence tracking.",
                        "confidence": 0.9,
                        "source_evidence": "Use Jira for audit evidence tracking",
                        "source_span": {"start": 55, "end": 91},
                        "review_reasons": [],
                        "structured_payload": {},
                    },
                    {
                        "item_type": "tool_usage",
                        "memory_layer": "tool",
                        "title": "Jira tracking reference",
                        "content": "Use Jira for audit evidence tracking.",
                        "confidence": 0.88,
                        "source_evidence": "Use Jira for audit evidence tracking",
                        "source_span": {"start": 55, "end": 91},
                        "review_reasons": [],
                        "structured_payload": {},
                    },
                ]
            }

        candidates = asyncio.run(
            self.engine.extract_source(
                memory=self.memory,
                source_ref={"memory_id": self.memory.id},
                classification=PersonaSourceClassification(
                    "workflow",
                    0.95,
                    extraction_targets=["workflow", "tool_usage"],
                ),
                model_profile=self.profile,
                generate_structured=generate_structured,
            )
        )

        self.assertEqual(prompts[0]["distiller"], "llm_broad_distiller")
        self.assertEqual(candidates[0].metadata["distiller"], "llm_workflow_distiller")
        self.assertEqual(candidates[1].metadata["distiller"], "llm_tool_usage_distiller")

    def test_approved_source_intelligence_is_available_to_llm_prompt(self) -> None:
        memory = self.memory.model_copy(
            update={
                "metadata": {
                    "source_intelligence": {
                        "review_status": "approved",
                        "classification": {
                            "label": "decision",
                            "confidence": 0.93,
                            "extraction_targets": ["decision_pattern"],
                        },
                        "graph_hints": {
                            "entities": [
                                {"label": "Decision", "name": "Evidence escalation"},
                            ],
                        },
                    }
                }
            }
        )
        prompts: list[dict] = []

        async def generate_structured(**kwargs):
            prompt = json.loads(kwargs["prompt"])
            prompts.append(prompt)
            return {
                "candidates": [
                    {
                        "item_type": "decision_pattern",
                        "memory_layer": "procedural",
                        "title": "Escalate missing evidence",
                        "content": "If evidence is missing, escalate.",
                        "confidence": 0.88,
                        "source_evidence": "If evidence is missing, escalate",
                        "source_span": {"start": 78, "end": 109},
                        "review_reasons": ["source_backed"],
                        "structured_payload": {},
                    }
                ]
            }

        asyncio.run(
            self.engine.extract_source(
                memory=memory,
                source_ref={"memory_id": memory.id},
                classification=PersonaSourceClassification(
                    label="decision",
                    confidence=0.95,
                    extraction_targets=["decision_pattern"],
                ),
                model_profile=self.profile,
                generate_structured=generate_structured,
            )
        )

        approved = prompts[0]["approved_source_intelligence"]
        self.assertEqual(approved["review_status"], "approved")
        self.assertEqual(approved["classification"]["label"], "decision")
        self.assertEqual(approved["graph_hints"]["entities"][0]["name"], "Evidence escalation")

    def test_exact_span_evidence_is_grounded_in_source_ref(self) -> None:
        async def generate_structured(**kwargs):
            return {
                "candidates": [
                    {
                        "item_type": "domain_knowledge",
                        "memory_layer": "semantic",
                        "title": "Source-backed knowledge",
                        "content": "Source evidence phrase.",
                        "confidence": 0.9,
                        "source_evidence": "Source evidence phrase",
                        "source_span": {"start": 0, "end": 22},
                        "review_reasons": [],
                        "structured_payload": {},
                    }
                ]
            }

        candidates = asyncio.run(
            self.engine.extract_source(
                memory=self.memory,
                source_ref={"memory_id": self.memory.id},
                classification=PersonaSourceClassification(
                    "domain_knowledge",
                    0.95,
                    extraction_targets=["domain_knowledge"],
                ),
                model_profile=self.profile,
                generate_structured=generate_structured,
            )
        )

        evidence = candidates[0].structured_payload["source_ref"]["evidence"]
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["match_method"], "exact_span")
        self.assertEqual(evidence["matched_span"], {"start": 0, "end": 22})
        self.assertFalse(candidates[0].needs_review)

    def test_fuzzy_evidence_match_is_recorded_when_exact_text_is_absent(self) -> None:
        async def generate_structured(**kwargs):
            return {
                "candidates": [
                    {
                        "item_type": "domain_knowledge",
                        "memory_layer": "semantic",
                        "title": "Fuzzy source-backed knowledge",
                        "content": "Source evidence phrasing.",
                        "confidence": 0.84,
                        "source_evidence": "Source evidence phrasing",
                        "source_span": None,
                        "review_reasons": [],
                        "structured_payload": {},
                    }
                ]
            }

        candidates = asyncio.run(
            self.engine.extract_source(
                memory=self.memory,
                source_ref={"memory_id": self.memory.id},
                classification=PersonaSourceClassification(
                    "domain_knowledge",
                    0.95,
                    extraction_targets=["domain_knowledge"],
                ),
                model_profile=self.profile,
                generate_structured=generate_structured,
            )
        )

        evidence = candidates[0].structured_payload["source_ref"]["evidence"]
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["match_method"], "fuzzy_text")
        self.assertGreaterEqual(evidence["match_score"], 0.72)
        self.assertTrue(candidates[0].needs_review)
        self.assertIn("missing_source_span", candidates[0].metadata["review_reasons"])

    def test_unverified_evidence_is_marked_for_review(self) -> None:
        async def generate_structured(**kwargs):
            return {
                "candidates": [
                    {
                        "item_type": "domain_knowledge",
                        "memory_layer": "semantic",
                        "title": "Unsupported knowledge",
                        "content": "Unsupported claim.",
                        "confidence": 0.83,
                        "source_evidence": "unrelated unsupported claim",
                        "source_span": None,
                        "review_reasons": [],
                        "structured_payload": {},
                    }
                ]
            }

        candidates = asyncio.run(
            self.engine.extract_source(
                memory=self.memory,
                source_ref={"memory_id": self.memory.id},
                classification=PersonaSourceClassification(
                    "domain_knowledge",
                    0.95,
                    extraction_targets=["domain_knowledge"],
                ),
                model_profile=self.profile,
                generate_structured=generate_structured,
            )
        )

        evidence = candidates[0].structured_payload["source_ref"]["evidence"]
        self.assertFalse(evidence["verified"])
        self.assertEqual(evidence["verification_reason"], "evidence_not_found")
        self.assertTrue(candidates[0].needs_review)
        self.assertIn("evidence_not_verified", candidates[0].metadata["review_reasons"])

    def test_low_confidence_classification_uses_broad_distiller(self) -> None:
        prompts: list[dict] = []

        async def generate_structured(**kwargs):
            prompt = json.loads(kwargs["prompt"])
            prompts.append(prompt)
            return {
                "candidates": [
                    {
                        "item_type": "domain_knowledge",
                        "memory_layer": "semantic",
                        "title": "Broad knowledge candidate",
                        "content": "Source evidence phrase.",
                        "confidence": 0.81,
                        "source_evidence": "Source evidence phrase",
                        "source_span": {"start": 0, "end": 22},
                        "review_reasons": ["source_backed"],
                        "structured_payload": {},
                    }
                ]
            }

        candidates = asyncio.run(
            self.engine.extract_source(
                memory=self.memory,
                source_ref={"memory_id": self.memory.id},
                classification=PersonaSourceClassification("fallback", 0.5, ["fallback"]),
                model_profile=self.profile,
                generate_structured=generate_structured,
            )
        )

        self.assertEqual(prompts[0]["distiller"], "llm_broad_distiller")
        self.assertEqual(candidates[0].metadata["distiller"], "llm_knowledge_distiller")

    def test_tool_usage_candidates_are_review_only_and_never_grants(self) -> None:
        async def generate_structured(**kwargs):
            return {
                "candidates": [
                    {
                        "item_type": "tool_usage",
                        "memory_layer": "tool",
                        "title": "Use Jira for audit tracking",
                        "content": "Use Jira for audit evidence tracking.",
                        "confidence": 0.9,
                        "source_evidence": "Use Jira for audit evidence tracking",
                        "source_span": {"start": 55, "end": 91},
                        "review_reasons": [],
                        "structured_payload": {"tool_name": "Jira", "tool_grant": True},
                    }
                ]
            }

        candidates = asyncio.run(
            self.engine.extract_source(
                memory=self.memory,
                source_ref={"memory_id": self.memory.id},
                classification=PersonaSourceClassification(
                    "tool_usage",
                    0.95,
                    extraction_targets=["tool_usage"],
                ),
                model_profile=self.profile,
                generate_structured=generate_structured,
            )
        )

        self.assertTrue(candidates[0].needs_review)
        self.assertIn("tool_grant_requires_review", candidates[0].metadata["review_reasons"])
        self.assertFalse(candidates[0].structured_payload["tool_grant"])
        self.assertEqual(candidates[0].structured_payload["review_policy"], "tool_usage_candidate_only")

    def test_style_and_social_candidates_require_source_supported_evidence(self) -> None:
        async def generate_structured(**kwargs):
            return {
                "candidates": [
                    {
                        "item_type": "writing_style",
                        "memory_layer": "persona",
                        "title": "Unsupported style claim",
                        "content": "The persona is humorous.",
                        "confidence": 0.83,
                        "source_evidence": "humorous and casual",
                        "source_span": {"start": 0, "end": 18},
                        "review_reasons": [],
                        "structured_payload": {},
                    }
                ]
            }

        with self.assertRaises(PersonaLLMDistillationError):
            asyncio.run(
                self.engine.extract_source(
                    memory=self.memory,
                    source_ref={"memory_id": self.memory.id},
                    classification=PersonaSourceClassification(
                        "personal_writing_style",
                        0.95,
                        extraction_targets=["writing_style"],
                    ),
                    model_profile=self.profile,
                    generate_structured=generate_structured,
                )
            )

    def test_specialized_distiller_rejects_wrong_item_type(self) -> None:
        async def generate_structured(**kwargs):
            return {
                "candidates": [
                    {
                        "item_type": "tool_usage",
                        "memory_layer": "tool",
                        "title": "Wrong distiller output",
                        "content": "Use Jira for audit evidence tracking.",
                        "confidence": 0.83,
                        "source_evidence": "Use Jira for audit evidence tracking",
                        "source_span": {"start": 55, "end": 91},
                        "review_reasons": [],
                        "structured_payload": {},
                    }
                ]
            }

        with self.assertRaises(PersonaLLMDistillationError):
            asyncio.run(
                self.engine.extract_source(
                    memory=self.memory,
                    source_ref={"memory_id": self.memory.id},
                    classification=PersonaSourceClassification(
                        "decision",
                        0.95,
                        extraction_targets=["decision_pattern"],
                    ),
                    model_profile=self.profile,
                    generate_structured=generate_structured,
                )
            )


if __name__ == "__main__":
    unittest.main()
