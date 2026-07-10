from __future__ import annotations

import unittest

from app.api.context import create_test_api_context
from app.domain import (
    PersonaDefinition,
    PersonaDistillationItem,
    PersonaDistillationItemReviewStatus,
    PersonaDistillationItemType,
    PersonaDistillationRun,
    PersonaMemoryLayer,
)
from app.services.persona_factory import PersonaFactoryService


class PersonaPackageSynthesisTests(unittest.TestCase):
    def test_package_synthesis_maps_curated_items_to_runtime_sections(self) -> None:
        persona = PersonaDefinition(
            id="persona-1",
            slug="audit-manager",
            name="Audit Manager",
            description="Reviews audit observations.",
        )
        run = PersonaDistillationRun(id="run-1", persona_id=persona.id, input_source_ids=["source-1"])
        approved = [
            PersonaDistillationItem(
                id="decision-1",
                run_id=run.id,
                persona_id=persona.id,
                source_memory_id="memory-1",
                item_type=PersonaDistillationItemType.DECISION_PATTERN,
                memory_layer=PersonaMemoryLayer.PROCEDURAL,
                title="Severity rule",
                content="Escalate privileged access scope gaps.",
                review_status=PersonaDistillationItemReviewStatus.APPROVED,
                needs_review=False,
                confidence=0.92,
                structured_payload={"source_ref": {"memory_id": "memory-1"}},
            ),
            PersonaDistillationItem(
                id="workflow-1",
                run_id=run.id,
                persona_id=persona.id,
                source_memory_id="memory-2",
                item_type=PersonaDistillationItemType.WORKFLOW,
                memory_layer=PersonaMemoryLayer.PROCEDURAL,
                title="Audit review",
                content="Plan, test, validate, draft.",
                review_status=PersonaDistillationItemReviewStatus.APPROVED,
                needs_review=False,
                confidence=0.88,
            ),
            PersonaDistillationItem(
                id="tool-1",
                run_id=run.id,
                persona_id=persona.id,
                source_memory_id="memory-3",
                item_type=PersonaDistillationItemType.TOOL_USAGE,
                memory_layer=PersonaMemoryLayer.TOOL,
                title="Jira",
                content="Use Jira to track evidence requests.",
                review_status=PersonaDistillationItemReviewStatus.APPROVED,
                needs_review=False,
                confidence=0.84,
                structured_payload={"tool_id": "jira"},
            ),
            PersonaDistillationItem(
                id="style-1",
                run_id=run.id,
                persona_id=persona.id,
                source_memory_id="memory-4",
                item_type=PersonaDistillationItemType.WRITING_STYLE,
                memory_layer=PersonaMemoryLayer.PERSONA,
                title="Formal style",
                content="Write in a formal and concise tone.",
                review_status=PersonaDistillationItemReviewStatus.APPROVED,
                needs_review=False,
                confidence=0.86,
            ),
        ]
        rejected = PersonaDistillationItem(
            id="rejected-1",
            run_id=run.id,
            persona_id=persona.id,
            source_memory_id="memory-5",
            item_type=PersonaDistillationItemType.EXAMPLE,
            memory_layer=PersonaMemoryLayer.EPISODIC,
            title="Rejected example",
            content="This example should not be active.",
            review_status=PersonaDistillationItemReviewStatus.REJECTED,
            needs_review=False,
        )

        package = PersonaFactoryService(create_test_api_context())._package_from_items(
            persona=persona,
            run=run,
            items=approved,
            all_items=[*approved, rejected],
        )

        self.assertEqual(package["schema_version"], 1)
        self.assertEqual(package["provenance"]["strategy"], "item-synthesis-v1")
        self.assertEqual(package["decision_patterns"][0]["distillation_item_id"], "decision-1")
        self.assertEqual(package["workflows"][0]["distillation_item_id"], "workflow-1")
        self.assertEqual(package["tools"][0]["tool_id"], "jira")
        self.assertIn("formal", package["persona"]["communication_style"])
        self.assertIn("rejected-1", package["provenance"]["excluded_item_ids"])
        self.assertEqual(package["memory_layers"]["tool"][0]["distillation_item_id"], "tool-1")


if __name__ == "__main__":
    unittest.main()
