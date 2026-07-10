from __future__ import annotations

import unittest

from app.domain import MemoryRecord, MemoryType
from app.services.persona_distillation_pipeline import PersonaDistillationPipeline


class PersonaDistillationPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline = PersonaDistillationPipeline()

    def test_extracts_each_deterministic_distiller_contract_from_fixture_sources(self) -> None:
        fixtures = [
            (
                "decision",
                MemoryRecord(
                    id="fixture-decision",
                    scope="user",
                    created_by_user_id="user-1",
                    content="If SOC coverage is incomplete, escalate the observation before relying on controls.",
                    memory_type=MemoryType.DECISION,
                ),
                "decision_pattern",
            ),
            (
                "workflow",
                MemoryRecord(
                    id="fixture-workflow",
                    scope="user",
                    created_by_user_id="user-1",
                    content=(
                        "Audit workflow starts when planning is approved.\n"
                        "Owner: audit manager.\n"
                        "Inputs: evidence request list.\n"
                        "1. Plan testing.\n2. Validate evidence.\n"
                        "Outputs: reviewed workpaper.\n"
                        "If evidence is missing, escalate."
                    ),
                    memory_type=MemoryType.FACT,
                ),
                "workflow",
            ),
            (
                "writing_style",
                MemoryRecord(
                    id="fixture-style",
                    scope="user",
                    created_by_user_id="user-1",
                    content="Tone: formal, concise, diplomatic, and management-focused.",
                    memory_type=MemoryType.PREFERENCE,
                ),
                "writing_style",
            ),
            (
                "tool_usage",
                MemoryRecord(
                    id="fixture-tool",
                    scope="user",
                    created_by_user_id="user-1",
                    content="Use Jira and ServiceNow to track audit evidence and approvals.",
                    memory_type=MemoryType.FACT,
                ),
                "tool_usage",
            ),
            (
                "guardrail",
                MemoryRecord(
                    id="fixture-guardrail",
                    scope="user",
                    created_by_user_id="user-1",
                    content="Teams must not include private employee data without explicit approval.",
                    memory_type=MemoryType.FACT,
                ),
                "guardrail",
            ),
            (
                "example",
                MemoryRecord(
                    id="fixture-example",
                    scope="user",
                    created_by_user_id="user-1",
                    content="Example observation: access review scope excluded administrator accounts.",
                    memory_type=MemoryType.FACT,
                ),
                "example",
            ),
            (
                "knowledge",
                MemoryRecord(
                    id="fixture-knowledge",
                    scope="user",
                    created_by_user_id="user-1",
                    content="Audit evidence should include reviewer, timestamp, control ID, and conclusion.",
                    memory_type=MemoryType.FACT,
                ),
                "domain_knowledge",
            ),
        ]

        for label, memory, expected_type in fixtures:
            with self.subTest(label=label):
                classification = self.pipeline.classify(memory)
                candidates = self.pipeline.extract(
                    memory=memory,
                    source_ref={"memory_id": memory.id},
                    classification=classification,
                )
                item_types = {candidate.item_type.value for candidate in candidates}

                self.assertIn(expected_type, item_types)
                self.assertTrue(all(self._candidate_references_memory(candidate, memory.id) for candidate in candidates))
                self.assertTrue(all(candidate.structured_payload["extractor"] == "deterministic-multi-distiller-v1" for candidate in candidates))
                self.assertTrue(all(candidate.structured_payload["distiller"] for candidate in candidates))
                self.assertTrue(all(candidate.structured_payload["distiller_version"] == "specialized-distillers-v1" for candidate in candidates))

    def test_upload_intelligence_routes_policy_sources_to_multiple_distillers(self) -> None:
        memory = MemoryRecord(
            id="upload-intelligence-source",
            scope="user",
            created_by_user_id="user-1",
            content=(
                "Procedure requires approval before release. "
                "Teams must not deploy without evidence. "
                "If evidence is missing, escalate to the release owner."
            ),
            memory_type=MemoryType.ARCHIVE,
            tags=["persona-source"],
            metadata={
                "filename": "release-sop.md",
                "document_id": "doc-release",
                "chunk_index": 0,
                "chunk_count": 1,
                "upload_mode": "vector",
                "upload_intelligence": {
                    "source": "main_agent_llm",
                    "document_kind": "policy_sop",
                    "confidence": 0.91,
                    "recommended": {"tags": ["release", "approval"]},
                },
            },
        )

        classification = self.pipeline.classify(memory)
        candidates = self.pipeline.extract(
            memory=memory,
            source_ref={"memory_id": memory.id, "document_id": "doc-release"},
            classification=classification,
        )
        item_types = {candidate.item_type.value for candidate in candidates}

        self.assertEqual(classification.label, "policy_sop")
        self.assertEqual(classification.document_kind, "policy_sop")
        self.assertIn("metadata:upload_intelligence", classification.signals)
        self.assertIn("domain_knowledge", item_types)
        self.assertIn("guardrail", item_types)
        self.assertIn("workflow", item_types)
        for candidate in candidates:
            routing = candidate.structured_payload["routing"]
            self.assertEqual(routing["document_kind"], "policy_sop")
            self.assertIn("release", routing["vector_tags"])
            self.assertEqual(candidate.structured_payload["source_ref"]["document_id"], "doc-release")

    def test_source_intelligence_metadata_routes_decision_without_inventing_unseen_tools(self) -> None:
        memory = MemoryRecord(
            id="source-intelligence-source",
            scope="user",
            created_by_user_id="user-1",
            content="When access review excludes vendor administrators, escalate the audit observation.",
            memory_type=MemoryType.ARCHIVE,
            metadata={
                "source_intelligence": {
                    "review_status": "approved",
                    "classification": {
                        "label": "decision",
                        "confidence": 0.93,
                        "document_kind": "workpaper",
                        "extraction_targets": ["decision_pattern", "domain_knowledge"],
                        "memory_layers": ["procedural", "semantic"],
                        "vector_tags": ["access-review"],
                    },
                }
            },
        )

        classification = self.pipeline.classify(memory)
        candidates = self.pipeline.extract(
            memory=memory,
            source_ref={"memory_id": memory.id},
            classification=classification,
        )
        item_types = {candidate.item_type.value for candidate in candidates}
        all_content = "\n".join(candidate.content for candidate in candidates).lower()

        self.assertEqual(classification.label, "decision")
        self.assertIn("metadata:source_intelligence", classification.signals)
        self.assertIn("decision_pattern", item_types)
        self.assertIn("domain_knowledge", item_types)
        self.assertNotIn("neo4j", all_content)
        self.assertNotIn("servicenow", all_content)

    @staticmethod
    def _candidate_references_memory(candidate, memory_id: str) -> bool:
        source_ref = candidate.structured_payload.get("source_ref")
        if isinstance(source_ref, dict) and source_ref.get("memory_id") == memory_id:
            return True
        source_refs = candidate.structured_payload.get("source_refs")
        if isinstance(source_refs, list):
            return any(isinstance(ref, dict) and ref.get("memory_id") == memory_id for ref in source_refs)
        return False


if __name__ == "__main__":
    unittest.main()
