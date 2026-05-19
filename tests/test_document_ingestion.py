from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.domain import ModelProfileDefinition


class _FakeEmbeddingClient:
    def __init__(self, profile):
        self.profile = profile

    def embed_texts(self, texts, **kwargs):
        return [[float(len(text) % 7), 1.0] for text in texts]


class DocumentIngestionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("MEMORY_EMBEDDING_MODEL_PROFILE_ID", None)
        reset_settings_cache()
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake_embed", lambda profile, env: _FakeEmbeddingClient(profile))
        asyncio.run(
            self.context.model_profile_repo.create(
                ModelProfileDefinition(
                    id="profile-embedding",
                    name="Fake Embeddings",
                    provider="fake_embed",
                    model="fake-embedding-model",
                )
            )
        )
        os.environ["MEMORY_EMBEDDING_MODEL_PROFILE_ID"] = "profile-embedding"
        reset_settings_cache()
        self.client = TestClient(create_app(context=self.context))
        self.headers = {
            "x-agency-user-id": "user-doc",
            "x-agency-user-email": "doc@example.com",
        }

    def tearDown(self) -> None:
        os.environ.pop("MEMORY_EMBEDDING_MODEL_PROFILE_ID", None)
        reset_settings_cache()

    def test_ingest_text_document_creates_embedded_memory_chunks(self) -> None:
        text = (
            "Agency document ingestion should extract text and create semantic memory chunks. "
            "The uploaded roadmap mentions billing workflows, refunds, and chargebacks.\n\n"
            "A second section discusses browser automation and spreadsheet exports."
        )
        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-1/roadmap.txt"]},
        ):
            response = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={"chunk_size": "80", "chunk_overlap": "10", "tags": "roadmap,billing"},
                files={"file": ("roadmap.txt", text.encode("utf-8"), "text/plain")},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["filename"], "roadmap.txt")
        self.assertGreater(payload["chunks_created"], 1)
        self.assertEqual(len(payload["memory_ids"]), payload["chunks_created"])

        memories = [
            memory
            for memory in (
                asyncio.run(self.context.memory_repo.get(memory_id))
                for memory_id in payload["memory_ids"]
            )
            if memory is not None
        ]
        self.assertTrue(memories)
        first = memories[0]
        self.assertEqual(first.source, "document_upload")
        self.assertEqual(first.memory_kind.value, "archive")
        self.assertEqual(first.embedding_model_profile_id, "profile-embedding")
        self.assertIn("document_id", first.metadata)

    def test_ingest_rejects_unsupported_document_type(self) -> None:
        response = self.client.post(
            "/documents/ingest",
            headers=self.headers,
            files={"file": ("archive.zip", b"not supported", "application/zip")},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("Unsupported document type", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
