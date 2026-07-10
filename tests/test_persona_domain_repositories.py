from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.db.repositories.personas import (
    InMemoryPersonaDistillationItemRepository,
    InMemoryPersonaDistillationRunRepository,
    InMemoryPersonaRepository,
)
from app.domain import (
    PersonaDefinition,
    PersonaDistillationItem,
    PersonaDistillationItemType,
    PersonaDistillationMode,
    PersonaDistillationRun,
    PersonaLLMModelSource,
    PersonaMemoryLayer,
)


class PersonaDomainRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def test_persona_domain_validation_normalizes_slug_and_rejects_empty_content(self) -> None:
        persona = PersonaDefinition(slug=" Audit Manager ", name=" Audit Manager ")

        self.assertEqual(persona.slug, "audit manager")
        self.assertEqual(persona.name, "Audit Manager")
        with self.assertRaises(ValidationError):
            PersonaDistillationItem(
                run_id="run-1",
                persona_id="persona-1",
                item_type=PersonaDistillationItemType.DOMAIN_KNOWLEDGE,
                memory_layer=PersonaMemoryLayer.SEMANTIC,
                title=" ",
                content="Missing title should fail.",
            )
        with self.assertRaises(ValidationError):
            PersonaDistillationItem(
                run_id="run-1",
                persona_id="persona-1",
                item_type=PersonaDistillationItemType.DOMAIN_KNOWLEDGE,
                memory_layer=PersonaMemoryLayer.SEMANTIC,
                title="Invalid confidence",
                content="Confidence must stay inside the schema range.",
                confidence=1.5,
            )

    def test_legacy_distillation_run_payload_defaults_to_deterministic(self) -> None:
        legacy = PersonaDistillationRun.model_validate(
            {
                "id": "legacy-run",
                "persona_id": "persona-1",
            }
        )
        stale_llm_metadata = PersonaDistillationRun.model_validate(
            {
                "id": "legacy-deterministic-run",
                "persona_id": "persona-1",
                "distillation_mode": "deterministic",
                "llm_model_source": "main_agent",
            }
        )
        llm = PersonaDistillationRun.model_validate(
            {
                "id": "legacy-llm-run",
                "persona_id": "persona-1",
                "distillation_mode": "llm",
                "llm_model_source": "model_profile",
                "model_profile_id": "model-1",
            }
        )

        self.assertEqual(legacy.distillation_mode, PersonaDistillationMode.DETERMINISTIC)
        self.assertIsNone(legacy.llm_model_source)
        self.assertEqual(stale_llm_metadata.distillation_mode, PersonaDistillationMode.DETERMINISTIC)
        self.assertIsNone(stale_llm_metadata.llm_model_source)
        self.assertEqual(llm.llm_model_source, PersonaLLMModelSource.MODEL_PROFILE)

    async def test_in_memory_persona_repositories_support_lookup_and_scoped_lists(self) -> None:
        persona_repo = InMemoryPersonaRepository()
        run_repo = InMemoryPersonaDistillationRunRepository()
        item_repo = InMemoryPersonaDistillationItemRepository()
        persona = await persona_repo.create(
            PersonaDefinition(id="persona-1", slug="audit-manager", name="Audit Manager")
        )
        run = await run_repo.create(PersonaDistillationRun(id="run-1", persona_id=persona.id))
        await item_repo.create(
            PersonaDistillationItem(
                id="item-1",
                run_id=run.id,
                persona_id=persona.id,
                source_memory_id="memory-1",
                item_type=PersonaDistillationItemType.DOMAIN_KNOWLEDGE,
                memory_layer=PersonaMemoryLayer.SEMANTIC,
                title="Audit evidence",
                content="Evidence includes reviewer, timestamp, control ID, and conclusion.",
            )
        )

        self.assertEqual((await persona_repo.find_by_slug("Audit-Manager")), persona)
        self.assertEqual([item.id for item in await run_repo.list_by_persona(persona.id)], ["run-1"])
        self.assertEqual([item.id for item in await item_repo.list_by_run(run.id)], ["item-1"])
        self.assertEqual([item.id for item in await item_repo.list_by_source_memory("memory-1")], ["item-1"])


if __name__ == "__main__":
    unittest.main()
