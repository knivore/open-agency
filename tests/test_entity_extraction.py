from __future__ import annotations

import unittest

from app.services.entity_extraction import MemoryEntityExtractor


class MemoryEntityExtractorTests(unittest.TestCase):
    def test_extracts_entities_from_allowlisted_projection_fields(self) -> None:
        candidates = MemoryEntityExtractor().extract(
            {
                "summary": "Acme Launch Plan references Atlas Team.",
                "tags": ["entity:Launch Plan"],
                "metadata": {
                    "entity_hints": [
                        {"name": "Acme Corp", "type": "organization", "confidence": 0.92},
                        {"name": "Low Confidence", "type": "concept", "confidence": 0.2},
                    ],
                },
            },
            min_confidence=0.7,
        )

        candidate_ids = {candidate.id for candidate in candidates}
        self.assertIn("entity:organization:acme-corp", candidate_ids)
        self.assertIn("entity:concept:launch-plan", candidate_ids)
        self.assertNotIn("entity:concept:low-confidence", candidate_ids)

    def test_sensitive_memory_does_not_extract_from_summary(self) -> None:
        candidates = MemoryEntityExtractor().extract(
            {
                "summary": "Acme Secret Token",
                "sensitive": True,
            },
            min_confidence=0.6,
        )

        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
