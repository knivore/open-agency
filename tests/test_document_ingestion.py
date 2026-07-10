from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.graph.neo4j_projection import Neo4jGraphProjector
from app.graph.neo4j_read import GraphReadDocument, GraphReadEdge, GraphReadNode
from app.graph.parity import Neo4jGraphParityChecker
from app.graph.parity import _expected_counts_from_outbox
from app.domain import (
    Conversation,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    ModelProfileDefinition,
)
from app.llm.base import ModelResponse
from app.services.conversations.core import ConversationService
from app.services.main_agent_setup.service import MainAgentSetupConfig, MainAgentSetupService


class _FakeNeo4jSession:
    def __init__(self, calls: list[dict]):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, exc, _tb):
        return None

    async def run(self, cypher: str, **params):
        self.calls.append({"cypher": cypher, "params": params})


class _FakeNeo4jDriver:
    def __init__(self):
        self.calls: list[dict] = []

    def session(self, **kwargs):
        return _FakeNeo4jSession(self.calls)


class _FakeCountResult:
    def __init__(self, count: int):
        self.count = count
        self.sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return {"count": self.count}


class _FakeParitySession:
    def __init__(self, counts: dict[str, int]):
        self.counts = counts

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, exc, _tb):
        return None

    async def run(self, cypher: str, **params):
        for key, value in self.counts.items():
            if f"`{key}`" in cypher:
                return _FakeCountResult(value)
        return _FakeCountResult(0)


class _FakeParityDriver:
    def __init__(self, counts: dict[str, int]):
        self.counts = counts

    def session(self, **kwargs):
        return _FakeParitySession(self.counts)


class _FakeEmbeddingClient:
    def __init__(self, profile):
        self.profile = profile

    def embed_texts(self, texts, **kwargs):
        return [[float(len(text) % 7), 1.0] for text in texts]


class _FakeSourceIntelligenceClient:
    def __init__(self, profile, env):
        self.profile = profile
        self.env = env

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(
            content={
                "label": "workflow",
                "confidence": 0.91,
                "signals": ["workflow_steps"],
                "document_kind": "policy_sop",
                "content_roles": ["workflow", "policy_sop"],
                "extraction_targets": ["workflow", "guardrail"],
                "memory_layers": ["procedural", "semantic"],
                "vector_tags": ["release", "approval"],
                "graph_entities": [
                    {"label": "Workflow", "name": "Release Workflow", "confidence": 0.9},
                    {"label": "Artifact", "name": "Approval Record", "confidence": 0.84},
                ],
                "graph_relationships": [
                    {
                        "source_name": "Release Workflow",
                        "relationship_type": "PRODUCES",
                        "target_name": "Approval Record",
                        "confidence": 0.82,
                    }
                ],
                "should_include": True,
                "rationale": "The document describes a release workflow and approval artifact.",
            },
            provider=self.profile.provider,
            model=self.profile.model,
        )


class _FakeUploadIntelligenceClient:
    def __init__(self, profile, env):
        self.profile = profile
        self.env = env

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(
            content={
                "summary": "Audit workpaper upload with testing evidence.",
                "document_kind": "workpaper",
                "recommended_scope": "user",
                "recommended_workspace_id": None,
                "recommended_conversation_id": None,
                "recommended_workflow_id": None,
                "recommended_agent_id": None,
                "tags": ["audit", "workpaper", "evidence"],
                "chunk_size": 900,
                "chunk_overlap": 120,
                "governance_labels": {
                    "persona_type": "professional",
                    "capability_mode": "persona_plus_expertise",
                    "consent_status": "organization_authorized",
                    "source_basis": "uploaded_private_material",
                    "sensitivity_level": "sensitive",
                    "visibility": "private",
                    "representation_policy": "simulated_persona",
                },
                "confidence": 0.88,
                "rationale": "The document mentions audit testing evidence and workpaper review.",
            },
            provider=self.profile.provider,
            model=self.profile.model,
        )


class _FakeSmokeGraphReadService:
    def __init__(self, memory_id: str):
        self.memory_id = memory_id
        self.calls: list[dict] = []

    async def get_neighborhood(self, node_id: str, **kwargs):
        self.calls.append({"node_id": node_id, **kwargs})
        return GraphReadDocument(
            nodes=[
                GraphReadNode(id=node_id, type="Memory", labels=["Memory"], properties={"summary": "Release SOP chunk"}),
                GraphReadNode(
                    id="source-intelligence:workflow:smoke",
                    type="Workflow",
                    labels=["Entity", "Workflow"],
                    properties={"name": "Release Workflow"},
                ),
            ],
            edges=[
                GraphReadEdge(
                    id="edge-smoke",
                    source=node_id,
                    target="source-intelligence:workflow:smoke",
                    type="MENTIONS",
                )
            ],
            meta={"source": "fake-smoke"},
        )


class DocumentIngestionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("MEMORY_EMBEDDING_MODEL_PROFILE_ID", None)
        os.environ.pop("GRAPH_DOCUMENT_PROJECTION_MAX_CHUNKS", None)
        reset_settings_cache()
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake_embed", lambda profile, env: _FakeEmbeddingClient(profile))
        self.context.llm_provider_registry.register(
            "fake_source_intelligence",
            lambda profile, env: _FakeSourceIntelligenceClient(profile, env),
        )
        self.context.llm_provider_registry.register(
            "fake_upload_intelligence",
            lambda profile, env: _FakeUploadIntelligenceClient(profile, env),
        )
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
        asyncio.run(
            self.context.model_profile_repo.create(
                ModelProfileDefinition(
                    id="profile-source-intelligence",
                    name="Fake Source Intelligence",
                    provider="fake_source_intelligence",
                    model="fake-source-intelligence-model",
                    supports_structured_output=True,
                )
            )
        )
        asyncio.run(
            self.context.model_profile_repo.create(
                ModelProfileDefinition(
                    id="profile-upload-intelligence",
                    name="Fake Upload Intelligence",
                    provider="fake_upload_intelligence",
                    model="fake-upload-intelligence-model",
                    supports_structured_output=True,
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
        self.client.post("/users/sync", json={"id": "user-doc", "email": "doc@example.com"})

    def _create_main_agent_for_upload_intelligence(self) -> None:
        asyncio.run(
            MainAgentSetupService(self.context).create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_description="Main agent for upload intelligence.",
                    agent_instructions="Classify uploads and recommend ingestion settings.",
                    model_profile_id="profile-upload-intelligence",
                    profile_id="main-agent-profile",
                )
            )
        )

    def tearDown(self) -> None:
        os.environ.pop("MEMORY_EMBEDDING_MODEL_PROFILE_ID", None)
        os.environ.pop("GRAPH_DOCUMENT_PROJECTION_MAX_CHUNKS", None)
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

        listed = self.client.get(
            "/memories",
            headers=self.headers,
            params={"q": "refund chargeback billing", "limit": 5},
        )
        self.assertEqual(listed.status_code, 200)
        memories = listed.json()["items"]
        self.assertTrue(memories)
        first = memories[0]
        self.assertEqual(first["source"], "document_upload")
        self.assertEqual(first["memory_type"], "archive")
        self.assertEqual(first["embedding_model_profile_id"], "profile-embedding")
        self.assertIn("document_id", first["metadata"])
        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        collection_events = [
            event for event in projection_events if event.event_type == "document_memory_collection.created"
        ]
        self.assertEqual(len(collection_events), 1)
        self.assertEqual(collection_events[0].aggregate_id, payload["document_id"])
        self.assertEqual(set(collection_events[0].payload["memory_ids"]), set(payload["memory_ids"]))
        self.assertEqual(collection_events[0].payload["scope"], "user")
        self.assertEqual(collection_events[0].payload["created_by_user_id"], "user-doc")
        self.assertEqual(collection_events[0].payload["chunk_count"], payload["chunks_created"])
        self.assertEqual(collection_events[0].payload["projected_chunk_count"], payload["chunks_created"])
        self.assertEqual(collection_events[0].payload["omitted_chunk_count"], 0)
        self.assertFalse(collection_events[0].payload["projection_capped"])
        document = asyncio.run(self.context.uploaded_document_repo.get(payload["document_id"]))
        self.assertIsNotNone(document)
        assert document is not None
        observability = document.metadata["upload_observability"]
        self.assertEqual(observability["upload_mode"], "vector")
        self.assertFalse(observability["direct_context_attachment"])
        self.assertTrue(observability["archive_memory_created"])
        self.assertEqual(observability["memory_ids"], payload["memory_ids"])
        self.assertTrue(observability["projection_event_created"])
        self.assertIsNone(observability["context_attachment_id"])
        self.assertIsNone(observability["direct_context_max_tokens"])
        for event in projection_events:
            self.assertNotIn("content", event.payload)
            self.assertNotIn("embedding", event.payload)

    def test_main_agent_upload_intelligence_recommends_and_applies_document_settings(self) -> None:
        self._create_main_agent_for_upload_intelligence()
        text = (
            "Audit workpaper testing evidence for privileged access review. "
            "The reviewer validates exceptions and stores evidence."
        )

        intelligence = self.client.post(
            "/documents/intelligence",
            headers=self.headers,
            data={"purpose": "persona_factory", "tags": "persona-source"},
            files={"file": ("access-workpaper.txt", text.encode("utf-8"), "text/plain")},
        )
        self.assertEqual(intelligence.status_code, 200, intelligence.text)
        recommendation = intelligence.json()
        self.assertEqual(recommendation["source"], "main_agent_llm")
        self.assertEqual(recommendation["model_profile_id"], "profile-upload-intelligence")
        self.assertEqual(recommendation["document_kind"], "workpaper")
        self.assertEqual(recommendation["recommended"]["chunk_size"], 900)
        self.assertEqual(recommendation["recommended"]["chunk_overlap"], 120)
        self.assertEqual(recommendation["recommended"]["governance_labels"]["sensitivity_level"], "sensitive")
        self.assertIn("audit", recommendation["recommended"]["tags"])

        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-ai/access-workpaper.txt"]},
        ):
            ingest = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={
                    "tags": "persona-source",
                    "auto_intelligence": "true",
                    "allow_scope_suggestion": "true",
                    "allow_agent_suggestion": "true",
                    "purpose": "persona_factory",
                },
                files={"file": ("access-workpaper.txt", text.encode("utf-8"), "text/plain")},
            )
        self.assertEqual(ingest.status_code, 200, ingest.text)
        memory_id = ingest.json()["memory_ids"][0]
        memory = self.client.get(f"/memories/{memory_id}", headers=self.headers)
        self.assertEqual(memory.status_code, 200, memory.text)
        payload = memory.json()
        self.assertIn("audit", payload["tags"])
        self.assertIn("persona-source", payload["tags"])
        upload_intelligence = payload["metadata"]["upload_intelligence"]
        self.assertEqual(upload_intelligence["source"], "main_agent_llm")
        self.assertEqual(upload_intelligence["recommended"]["chunk_size"], 900)
        self.assertEqual(upload_intelligence["applied"]["chunk_size"], 900)
        self.assertEqual(
            upload_intelligence["recommended"]["governance_labels"]["source_basis"],
            "uploaded_private_material",
        )

    def test_context_upload_creates_document_reference_without_archive_memories(self) -> None:
        conversation = asyncio.run(
            self.context.conversation_repo.create(
                Conversation(id="conversation-context-doc", created_by_user_id="user-doc")
            )
        )
        text = "This file is only for the next chat turn. It should not become archive memory."

        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-context/context.txt"]},
        ):
            response = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={
                    "scope": "conversation",
                    "conversation_id": conversation.id,
                    "upload_mode": "context",
                    "tags": "chat-upload",
                },
                files={"file": ("context.txt", text.encode("utf-8"), "text/plain")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["upload_mode"], "context")
        self.assertEqual(payload["context_attachment_id"], payload["document_id"])
        self.assertEqual(payload["chunks_created"], 0)
        self.assertEqual(payload["memory_ids"], [])
        self.assertEqual(asyncio.run(self.context.memory_repo.list()), [])

        document = asyncio.run(self.context.uploaded_document_repo.get(payload["document_id"]))
        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.conversation_id, conversation.id)
        self.assertEqual(document.upload_mode.value, "context")
        self.assertIn("only for the next chat turn", document.extracted_text)
        observability = document.metadata["upload_observability"]
        self.assertEqual(observability["upload_mode"], "context")
        self.assertTrue(observability["direct_context_attachment"])
        self.assertEqual(observability["context_attachment_id"], payload["document_id"])
        self.assertFalse(observability["archive_memory_created"])
        self.assertEqual(observability["chunks_created"], 0)
        self.assertEqual(observability["memory_ids"], [])
        self.assertFalse(observability["projection_event_created"])
        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        collection_events = [
            event for event in projection_events if event.event_type == "document_memory_collection.created"
        ]
        self.assertEqual(collection_events, [])

        prompt = asyncio.run(
            ConversationService(self.context)._direct_document_context_prompt(
                ConversationMessage(
                    conversation_id=conversation.id,
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Use the attachment.",
                    metadata={"context_attachment_ids": [payload["document_id"]]},
                )
            )
        )
        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertIn("Direct Uploaded Document Context", prompt)
        self.assertIn("untrusted source text", prompt)
        self.assertIn("only for the next chat turn", prompt)

    def test_context_usage_reports_direct_context_document_budget(self) -> None:
        self._create_main_agent_for_upload_intelligence()
        conversation = asyncio.run(
            self.context.conversation_repo.create(
                Conversation(id="conversation-context-usage-doc", created_by_user_id="user-doc")
            )
        )

        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-usage/usage.txt"]},
        ):
            response = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={
                    "scope": "conversation",
                    "conversation_id": conversation.id,
                    "upload_mode": "context",
                },
                files={"file": ("usage.txt", b"Direct context evidence for the latest model turn.", "text/plain")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        document_id = response.json()["document_id"]
        asyncio.run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    conversation_id=conversation.id,
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Use the uploaded evidence.",
                    metadata={"context_attachment_ids": [document_id]},
                )
            )
        )

        usage = asyncio.run(ConversationService(self.context).get_context_usage(conversation.id))

        direct_context = usage["direct_document_context"]
        self.assertEqual(direct_context["attachment_count"], 1)
        self.assertEqual(direct_context["included_count"], 1)
        self.assertEqual(direct_context["skipped_count"], 0)
        self.assertGreater(direct_context["estimated_tokens"], 0)
        self.assertEqual(direct_context["documents"][0]["document_id"], document_id)
        self.assertEqual(direct_context["documents"][0]["status"], "included")
        self.assertGreaterEqual(usage["estimated_context_tokens"], direct_context["estimated_tokens"])

    def test_uploaded_document_list_and_detail_are_owner_scoped(self) -> None:
        conversation = asyncio.run(
            self.context.conversation_repo.create(
                Conversation(id="conversation-owner-doc", created_by_user_id="user-doc")
            )
        )
        text = "Owner scoped context document."

        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-owner/owner.txt"]},
        ):
            response = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={
                    "scope": "conversation",
                    "conversation_id": conversation.id,
                    "upload_mode": "context",
                    "tags": "chat-upload",
                },
                files={"file": ("owner.txt", text.encode("utf-8"), "text/plain")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        document_id = response.json()["document_id"]

        listed = self.client.get(
            "/documents",
            headers=self.headers,
            params={"conversation_id": conversation.id, "scope": "conversation"},
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["id"] for item in listed.json()["items"]], [document_id])
        self.assertNotIn("extracted_text", listed.json()["items"][0])

        detail = self.client.get(f"/documents/{document_id}", headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["upload_mode"], "context")
        self.assertNotIn("extracted_text", detail.json())

        other_headers = {
            "x-agency-user-id": "user-other",
            "x-agency-user-email": "other@example.com",
        }
        self.client.post("/users/sync", json={"id": "user-other", "email": "other@example.com"})
        other_listed = self.client.get(
            "/documents",
            headers=other_headers,
            params={"conversation_id": conversation.id, "scope": "conversation"},
        )
        self.assertEqual(other_listed.status_code, 200, other_listed.text)
        self.assertEqual(other_listed.json()["items"], [])

        other_detail = self.client.get(f"/documents/{document_id}", headers=other_headers)
        self.assertEqual(other_detail.status_code, 404)

    def test_delete_context_uploaded_document_tombstones_without_memory_delete(self) -> None:
        conversation = asyncio.run(
            self.context.conversation_repo.create(
                Conversation(id="conversation-delete-context-doc", created_by_user_id="user-doc")
            )
        )
        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-delete-context/context.txt"]},
        ):
            response = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={
                    "scope": "conversation",
                    "conversation_id": conversation.id,
                    "upload_mode": "context",
                },
                files={"file": ("context.txt", b"Temporary direct context.", "text/plain")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        document_id = response.json()["document_id"]

        delete_response = self.client.delete(f"/documents/{document_id}", headers=self.headers)

        self.assertEqual(delete_response.status_code, 200, delete_response.text)
        self.assertEqual(
            delete_response.json(),
            {
                "deleted": True,
                "document_id": document_id,
                "upload_mode": "context",
                "document_status": "deleted",
                "memory_ids": [],
                "deleted_memory_count": 0,
            },
        )
        self.assertIsNone(asyncio.run(self.context.uploaded_document_repo.get(document_id)))
        deleted_document = asyncio.run(self.context.uploaded_document_repo.get(document_id, include_deleted=True))
        self.assertIsNotNone(deleted_document)
        assert deleted_document is not None
        self.assertEqual(deleted_document.status.value, "deleted")
        self.assertIsNone(deleted_document.extracted_text)
        self.assertTrue(deleted_document.metadata["upload_observability"]["deleted"])
        self.assertEqual(deleted_document.metadata["upload_observability"]["deleted_memory_count"], 0)
        self.assertEqual(asyncio.run(self.context.memory_repo.list()), [])
        self.assertEqual(asyncio.run(self.context.graph_projection_event_repo.list_events()), [])

        prompt = asyncio.run(
            ConversationService(self.context)._direct_document_context_prompt(
                ConversationMessage(
                    conversation_id=conversation.id,
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Use deleted attachment.",
                    metadata={"context_attachment_ids": [document_id]},
                )
            )
        )
        self.assertIsNone(prompt)

    def test_delete_uploaded_document_is_owner_scoped(self) -> None:
        conversation = asyncio.run(
            self.context.conversation_repo.create(
                Conversation(id="conversation-delete-owner-doc", created_by_user_id="user-doc")
            )
        )
        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-delete-owner/owner.txt"]},
        ):
            response = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={
                    "scope": "conversation",
                    "conversation_id": conversation.id,
                    "upload_mode": "context",
                },
                files={"file": ("owner.txt", b"Owner only context.", "text/plain")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        document_id = response.json()["document_id"]

        other_headers = {
            "x-agency-user-id": "user-other",
            "x-agency-user-email": "other@example.com",
        }
        self.client.post("/users/sync", json={"id": "user-other", "email": "other@example.com"})
        other_delete = self.client.delete(f"/documents/{document_id}", headers=other_headers)
        self.assertEqual(other_delete.status_code, 404)
        self.assertIsNotNone(asyncio.run(self.context.uploaded_document_repo.get(document_id)))

    def test_context_upload_rejects_oversized_direct_context(self) -> None:
        conversation = asyncio.run(
            self.context.conversation_repo.create(
                Conversation(id="conversation-large-context-doc", created_by_user_id="user-doc")
            )
        )
        text = "large context " * 9000

        response = self.client.post(
            "/documents/ingest",
            headers=self.headers,
            data={
                "scope": "conversation",
                "conversation_id": conversation.id,
                "upload_mode": "context",
            },
            files={"file": ("too-large.txt", text.encode("utf-8"), "text/plain")},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Direct context upload is too large", response.text)
        self.assertEqual(asyncio.run(self.context.uploaded_document_repo.list()), [])

    def test_both_upload_creates_document_reference_and_archive_memories(self) -> None:
        conversation = asyncio.run(
            self.context.conversation_repo.create(
                Conversation(id="conversation-both-doc", created_by_user_id="user-doc")
            )
        )
        text = " ".join(f"Reusable policy section {index}." for index in range(20))

        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-both/both.txt"]},
        ):
            response = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={
                    "scope": "conversation",
                    "conversation_id": conversation.id,
                    "upload_mode": "both",
                    "chunk_size": "120",
                    "chunk_overlap": "0",
                    "tags": "policy",
                },
                files={"file": ("both.txt", text.encode("utf-8"), "text/plain")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["upload_mode"], "both")
        self.assertEqual(payload["context_attachment_id"], payload["document_id"])
        self.assertGreater(payload["chunks_created"], 0)
        self.assertEqual(len(payload["memory_ids"]), payload["chunks_created"])
        document = asyncio.run(self.context.uploaded_document_repo.get(payload["document_id"]))
        self.assertIsNotNone(document)
        assert document is not None
        observability = document.metadata["upload_observability"]
        self.assertEqual(observability["upload_mode"], "both")
        self.assertTrue(observability["direct_context_attachment"])
        self.assertEqual(observability["context_attachment_id"], payload["document_id"])
        self.assertTrue(observability["archive_memory_created"])
        self.assertEqual(observability["chunks_created"], payload["chunks_created"])
        self.assertEqual(observability["memory_ids"], payload["memory_ids"])
        self.assertTrue(observability["projection_event_created"])
        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        collection_events = [
            event for event in projection_events if event.event_type == "document_memory_collection.created"
        ]
        self.assertEqual(len(collection_events), 1)
        self.assertEqual(collection_events[0].aggregate_id, payload["document_id"])

    def test_delete_both_uploaded_document_tombstones_document_and_archive_memories(self) -> None:
        conversation = asyncio.run(
            self.context.conversation_repo.create(
                Conversation(id="conversation-delete-both-doc", created_by_user_id="user-doc")
            )
        )
        text = " ".join(f"Reusable control evidence section {index}." for index in range(24))

        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-delete-both/both.txt"]},
        ):
            response = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={
                    "scope": "conversation",
                    "conversation_id": conversation.id,
                    "upload_mode": "both",
                    "chunk_size": "120",
                    "chunk_overlap": "0",
                },
                files={"file": ("both.txt", text.encode("utf-8"), "text/plain")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertGreater(payload["chunks_created"], 0)

        delete_response = self.client.delete(f"/documents/{payload['document_id']}", headers=self.headers)

        self.assertEqual(delete_response.status_code, 200, delete_response.text)
        delete_payload = delete_response.json()
        self.assertEqual(delete_payload["document_id"], payload["document_id"])
        self.assertEqual(delete_payload["upload_mode"], "both")
        self.assertEqual(delete_payload["document_status"], "deleted")
        self.assertEqual(set(delete_payload["memory_ids"]), set(payload["memory_ids"]))
        self.assertEqual(delete_payload["deleted_memory_count"], payload["chunks_created"])
        self.assertEqual(asyncio.run(self.context.memory_repo.list()), [])
        deleted_document = asyncio.run(
            self.context.uploaded_document_repo.get(payload["document_id"], include_deleted=True)
        )
        self.assertIsNotNone(deleted_document)
        assert deleted_document is not None
        self.assertEqual(deleted_document.status.value, "deleted")
        self.assertIsNone(deleted_document.extracted_text)
        observability = deleted_document.metadata["upload_observability"]
        self.assertTrue(observability["deleted"])
        self.assertEqual(set(observability["deleted_memory_ids"]), set(payload["memory_ids"]))
        self.assertEqual(observability["deleted_memory_count"], payload["chunks_created"])

        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        deleted_events = [
            event for event in projection_events if event.event_type == "document_memory_collection.deleted"
        ]
        self.assertEqual(len(deleted_events), 1)
        self.assertEqual(deleted_events[0].aggregate_id, payload["document_id"])
        self.assertEqual(set(deleted_events[0].payload["memory_ids"]), set(payload["memory_ids"]))

    def test_direct_context_prompt_skips_inaccessible_conversation_attachment(self) -> None:
        source_conversation = asyncio.run(
            self.context.conversation_repo.create(
                Conversation(id="conversation-source-doc", created_by_user_id="user-doc")
            )
        )
        target_conversation = asyncio.run(
            self.context.conversation_repo.create(
                Conversation(id="conversation-target-doc", created_by_user_id="user-doc")
            )
        )

        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-source/source.txt"]},
        ):
            response = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={
                    "scope": "conversation",
                    "conversation_id": source_conversation.id,
                    "upload_mode": "context",
                },
                files={"file": ("source.txt", b"source-only context", "text/plain")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        document_id = response.json()["document_id"]

        prompt = asyncio.run(
            ConversationService(self.context)._direct_document_context_prompt(
                ConversationMessage(
                    conversation_id=target_conversation.id,
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Use the attachment.",
                    metadata={"context_attachment_ids": [document_id]},
                )
            )
        )
        self.assertIsNone(prompt)

    def test_invalid_upload_mode_returns_422(self) -> None:
        response = self.client.post(
            "/documents/ingest",
            headers=self.headers,
            data={"upload_mode": "temporary"},
            files={"file": ("bad-mode.txt", b"Bad mode", "text/plain")},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Invalid upload_mode", response.text)

    def test_backend_source_intelligence_to_graph_projection_smoke_flow(self) -> None:
        text = (
            "Release workflow requires approval and produces an approval record before deployment. "
            "The release manager reviews the approval record and stores the evidence."
        )
        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-smoke/release-sop.txt"]},
        ):
            ingest = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={"chunk_size": "400", "chunk_overlap": "0", "tags": "release,approval"},
                files={"file": ("release-sop.txt", text.encode("utf-8"), "text/plain")},
            )
        self.assertEqual(ingest.status_code, 200, ingest.text)
        memory_id = ingest.json()["memory_ids"][0]

        analyze = self.client.post(
            "/memories/source-intelligence/analyze",
            headers=self.headers,
            json={"memory_ids": [memory_id], "model_profile_id": "profile-source-intelligence"},
        )
        self.assertEqual(analyze.status_code, 200, analyze.text)
        self.assertEqual(analyze.json()["items"][0]["graph_hints"]["review_status"], "needs_review")

        review = self.client.patch(
            f"/memories/{memory_id}/source-intelligence",
            headers=self.headers,
            json={
                "source_intelligence_review_status": "approved",
                "graph_hints_review_status": "approved",
                "review_note": "Smoke-test approval.",
            },
        )
        self.assertEqual(review.status_code, 200, review.text)

        projector_driver = _FakeNeo4jDriver()
        projector = Neo4jGraphProjector(projector_driver)
        projection_result = asyncio.run(projector.project_pending(self.context.graph_projection_event_repo, limit=50))
        self.assertEqual(projection_result.failed, 0)
        self.assertGreaterEqual(projection_result.processed, 1)
        cypher = "\n".join(call["cypher"] for call in projector_driver.calls)
        self.assertIn("source_intelligence_graph_hints_review_status", cypher)
        self.assertIn("edge:PRODUCES", cypher)

        expected, _, _ = asyncio.run(_expected_counts_from_outbox(self.context.graph_projection_event_repo, limit=100))
        parity_counts = {name: count for (_kind, name), count in expected.items()}
        parity = asyncio.run(Neo4jGraphParityChecker(_FakeParityDriver(parity_counts)).check(self.context.graph_projection_event_repo))
        self.assertTrue(parity.ok, parity.to_dict())
        self.assertGreaterEqual(parity.node_counts_by_type["Memory"], 1)
        self.assertGreaterEqual(parity.edge_counts_by_type["MENTIONS"], 1)

        self.context.graph_read_service = _FakeSmokeGraphReadService(memory_id)
        graph_read = self.client.get(f"/graph/read/nodes/{memory_id}/neighborhood", headers=self.headers)
        self.assertEqual(graph_read.status_code, 200, graph_read.text)
        self.assertEqual(graph_read.json()["nodes"][0]["id"], memory_id)
        self.assertEqual(graph_read.json()["edges"][0]["type"], "MENTIONS")

    def test_ingest_document_projection_caps_large_chunk_fanout(self) -> None:
        os.environ["GRAPH_DOCUMENT_PROJECTION_MAX_CHUNKS"] = "2"
        reset_settings_cache()
        text = " ".join(f"billing refund chargeback section {index}." for index in range(80))

        with patch(
            "app.services.document_ingestion.upload_to_s3",
            return_value={"uploaded_files": ["user_user-doc/workflow_documents/run_doc-2/large.txt"]},
        ):
            response = self.client.post(
                "/documents/ingest",
                headers=self.headers,
                data={"chunk_size": "80", "chunk_overlap": "0", "tags": "large"},
                files={"file": ("large.txt", text.encode("utf-8"), "text/plain")},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreater(payload["chunks_created"], 2)

        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        collection_event = next(
            event for event in projection_events if event.event_type == "document_memory_collection.created"
        )
        self.assertEqual(collection_event.payload["chunk_count"], payload["chunks_created"])
        self.assertEqual(collection_event.payload["projected_chunk_count"], 2)
        self.assertEqual(collection_event.payload["omitted_chunk_count"], payload["chunks_created"] - 2)
        self.assertTrue(collection_event.payload["projection_capped"])
        self.assertEqual(collection_event.payload["projection_max_chunks"], 2)
        self.assertEqual(collection_event.payload["memory_ids"], payload["memory_ids"][:2])

        listed = self.client.get(
            "/memories",
            headers=self.headers,
            params={"source": "document_upload", "limit": 100},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["items"]), payload["chunks_created"])

    def test_ingest_rejects_unsupported_document_type(self) -> None:
        response = self.client.post(
            "/documents/ingest",
            headers=self.headers,
            files={"file": ("archive.zip", b"not supported", "application/zip")},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("Unsupported document type", response.json()["detail"])

    def test_ingest_rejects_global_scope(self) -> None:
        response = self.client.post(
            "/documents/ingest",
            headers=self.headers,
            data={"scope": "global"},
            files={"file": ("policy.txt", b"global policy", "text/plain")},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("Invalid document memory scope", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
