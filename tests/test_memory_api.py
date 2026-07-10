from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.domain import (
    Conversation,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    ModelProfileDefinition,
)
from app.llm.base import ModelResponse


class _FakeEmbeddingClient:
    def __init__(self, profile):
        self.profile = profile

    def embed_texts(self, texts, **kwargs):
        return [self._embed(text) for text in texts]

    def _embed(self, text):
        lowered = text.lower()
        if any(token in lowered for token in {"invoice", "refund", "chargeback", "billing"}):
            return [1.0, 0.0]
        return [0.0, 1.0]


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
                    {
                        "label": "Workflow",
                        "name": "Release approval workflow",
                        "confidence": 0.9,
                        "evidence": "release workflow requires approval",
                    }
                ],
                "graph_relationships": [
                    {
                        "source_name": "Release approval workflow",
                        "relationship_type": "PRODUCES",
                        "target_name": "Approval record",
                        "confidence": 0.84,
                        "evidence": "produces an approval record",
                    }
                ],
                "should_include": True,
                "rationale": "The memory describes release workflow steps and approval artifacts.",
            },
            provider=self.profile.provider,
            model=self.profile.model,
        )


class MemoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("MEMORY_EMBEDDING_MODEL_PROFILE_ID", None)
        os.environ.pop("MEMORY_DAILY_SUMMARY_ENABLED", None)
        os.environ.pop("MEMORY_CONTEXT_PACK_ENABLED", None)
        os.environ.pop("GRAPH_PROJECTION_ENABLED", None)
        os.environ.pop("GRAPH_ENTITY_EXTRACTION_ENABLED", None)
        os.environ.pop("GRAPH_ENTITY_EXTRACTION_MIN_CONFIDENCE", None)
        reset_settings_cache()
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake_embed", lambda profile, env: _FakeEmbeddingClient(profile))
        self.context.llm_provider_registry.register(
            "fake_source_intelligence",
            lambda profile, env: _FakeSourceIntelligenceClient(profile, env),
        )
        self.client = TestClient(create_app(context=self.context))
        self.user_1_headers = {
            "x-agency-user-id": "user-1",
            "x-agency-user-email": "user1@example.com",
        }
        self.user_2_headers = {
            "x-agency-user-id": "user-2",
            "x-agency-user-email": "user2@example.com",
        }
        self.admin_headers = {
            "x-agency-user-id": "admin-1",
            "x-agency-user-email": "admin@example.com",
        }
        self.client.post("/users/sync", json={"id": "user-1", "email": "user1@example.com"})
        self.client.post("/users/sync", json={"id": "user-2", "email": "user2@example.com"})
        self.client.post(
            "/users/sync",
            json={"id": "admin-1", "email": "admin@example.com", "roles": ["admin"]},
        )

    def tearDown(self) -> None:
        os.environ.pop("MEMORY_EMBEDDING_MODEL_PROFILE_ID", None)
        os.environ.pop("MEMORY_DAILY_SUMMARY_ENABLED", None)
        os.environ.pop("MEMORY_CONTEXT_PACK_ENABLED", None)
        os.environ.pop("GRAPH_PROJECTION_ENABLED", None)
        os.environ.pop("GRAPH_ENTITY_EXTRACTION_ENABLED", None)
        os.environ.pop("GRAPH_ENTITY_EXTRACTION_MIN_CONFIDENCE", None)
        reset_settings_cache()

    def _enable_fake_embeddings(self) -> None:
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

    def _create_source_intelligence_profile(self) -> None:
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

    def test_memory_source_intelligence_analyze_and_review(self) -> None:
        self._create_source_intelligence_profile()
        create_response = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-source-intelligence",
                    "scope": "user",
                    "content": (
                        "Release workflow requires approval and produces an approval record before deployment."
                    ),
                    "summary": "Release workflow approval rule",
                    "tags": ["release"],
                    "memory_type": "archive",
                    "metadata": {"document_id": "doc-release", "filename": "release-sop.md", "chunk_index": 0},
                }
            },
        )
        self.assertEqual(create_response.status_code, 200, create_response.text)

        catalog = self.client.get("/memories/source-intelligence/catalog", headers=self.user_1_headers)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertIn("policy_sop", catalog.json()["document_kinds"])
        self.assertIn("Workflow", catalog.json()["graph_entity_labels"])

        analyze = self.client.post(
            "/memories/source-intelligence/analyze",
            headers=self.user_1_headers,
            json={
                "memory_ids": ["memory-source-intelligence"],
                "model_profile_id": "profile-source-intelligence",
            },
        )
        self.assertEqual(analyze.status_code, 200, analyze.text)
        analyzed = analyze.json()["items"][0]
        source_intelligence = analyzed["source_intelligence"]
        graph_hints = analyzed["graph_hints"]
        self.assertEqual(source_intelligence["classifier"], "llm")
        self.assertEqual(source_intelligence["classification"]["label"], "workflow")
        self.assertEqual(source_intelligence["classification"]["document_kind"], "policy_sop")
        self.assertEqual(source_intelligence["classification"]["vector_tags"], ["release", "approval"])
        self.assertEqual(graph_hints["review_status"], "needs_review")
        self.assertEqual(graph_hints["entities"][0]["label"], "Workflow")

        review = self.client.patch(
            "/memories/memory-source-intelligence/source-intelligence",
            headers=self.user_1_headers,
            json={
                "source_intelligence_review_status": "approved",
                "graph_hints_review_status": "approved",
                "review_note": "Graph hints match the SOP.",
            },
        )
        self.assertEqual(review.status_code, 200, review.text)
        metadata = review.json()["metadata"]
        self.assertEqual(metadata["source_intelligence"]["review_status"], "approved")
        self.assertEqual(metadata["graph_hints"]["review_status"], "approved")
        self.assertEqual(metadata["graph_hints"]["review"]["note"], "Graph hints match the SOP.")
        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events(limit=20))
        graph_hint_event = next(
            event
            for event in projection_events
            if event.event_type == "memory.source_intelligence.graph_hints.approved"
        )
        self.assertEqual(graph_hint_event.aggregate_id, "memory-source-intelligence")
        self.assertEqual(graph_hint_event.source, "source_intelligence")
        self.assertTrue(graph_hint_event.source_event_id.startswith("memory-graph-hints:"))
        self.assertEqual(graph_hint_event.payload["document_id"], "doc-release")
        self.assertEqual(graph_hint_event.payload["entities"][0]["label"], "Workflow")
        self.assertEqual(graph_hint_event.payload["relationships"][0]["relationship_type"], "PRODUCES")
        reapprove = self.client.patch(
            "/memories/memory-source-intelligence/source-intelligence",
            headers=self.user_1_headers,
            json={
                "source_intelligence_review_status": "approved",
                "graph_hints_review_status": "approved",
                "review_note": "Re-approve without changing hints.",
            },
        )
        self.assertEqual(reapprove.status_code, 200, reapprove.text)
        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events(limit=20))
        graph_hint_events = [
            event
            for event in projection_events
            if event.event_type == "memory.source_intelligence.graph_hints.approved"
        ]
        self.assertEqual(len(graph_hint_events), 1)

    def test_memory_crud_and_query_filters(self) -> None:
        create_response = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-1",
                    "scope": "user",
                    "content": "The user's timezone preference is Asia/Singapore.",
                    "summary": "Timezone preference is Asia/Singapore.",
                    "tags": ["preference"],
                }
            },
        )
        self.assertEqual(create_response.status_code, 200)

        other_response = self.client.post(
            "/memories",
            headers=self.user_2_headers,
            json={
                "memory": {
                    "id": "memory-2",
                    "scope": "user",
                    "content": "The user's timezone preference is America/New_York.",
                }
            },
        )
        self.assertEqual(other_response.status_code, 200)

        list_response = self.client.get(
            "/memories",
            headers=self.user_1_headers,
            params={"scope": "user", "q": "timezone"},
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual([item["id"] for item in list_response.json()["items"]], ["memory-1"])

        tag_response = self.client.get(
            "/memories",
            headers=self.user_1_headers,
            params={"scope": "user", "tag": "preference"},
        )
        self.assertEqual(tag_response.status_code, 200)
        self.assertEqual([item["id"] for item in tag_response.json()["items"]], ["memory-1"])

        update_response = self.client.patch(
            "/memories/memory-1",
            headers=self.user_1_headers,
            json={"patch": {"summary": "Timezone preference is Singapore."}},
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["summary"], "Timezone preference is Singapore.")

        delete_response = self.client.delete("/memories/memory-2")
        self.assertEqual(delete_response.status_code, 403)
        delete_response = self.client.delete("/memories/memory-2", headers=self.user_2_headers)
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json()["deleted"], True)
        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        memory_events = [event for event in projection_events if event.aggregate_type == "memory"]
        self.assertEqual(
            [event.event_type for event in memory_events],
            ["memory.created", "memory.created", "memory.updated", "memory.deleted"],
        )
        self.assertNotIn("content", memory_events[0].payload)
        self.assertNotIn("embedding", memory_events[0].payload)
        self.assertEqual(memory_events[0].payload["memory_id"], "memory-1")
        self.assertEqual(memory_events[0].payload["summary"], "Timezone preference is Asia/Singapore.")

    def test_graph_projection_feature_flag_disables_memory_outbox(self) -> None:
        os.environ["GRAPH_PROJECTION_ENABLED"] = "false"
        reset_settings_cache()
        response = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-no-projection",
                    "scope": "user",
                    "content": "Projection disabled memory.",
                }
            },
        )
        self.assertEqual(response.status_code, 200)
        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        self.assertEqual(projection_events, [])

    def test_memory_projection_payload_includes_provenance_without_raw_content(self) -> None:
        response = self.client.post(
            "/memories",
            headers=self.admin_headers,
            json={
                "memory": {
                    "id": "memory-provenance-1",
                    "scope": "workflow",
                    "workflow_id": "workflow-1",
                    "agent_id": "agent-1",
                    "content": "Raw provenance content should stay in Postgres only.",
                    "summary": "Projection-safe provenance summary.",
                    "tags": ["document", "launch"],
                    "source": "document_upload",
                    "memory_type": "archive",
                    "source_conversation_id": "conversation-1",
                    "source_execution_id": "execution-1",
                    "supersedes_memory_id": "memory-old",
                    "metadata": {
                        "document_id": "doc-1",
                        "filename": "launch.md",
                        "content_type": "text/markdown",
                        "content_sha256": "abc123",
                        "chunk_index": 2,
                        "chunk_count": 4,
                        "start_char": 200,
                        "end_char": 420,
                        "semantic_hint": "Launch document",
                        "storage_uri": "s3://should-not-project",
                    },
                    "embedding": [1.0, 0.0],
                }
            },
        )
        self.assertEqual(response.status_code, 200)

        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        memory_event = next(event for event in projection_events if event.aggregate_id == "memory-provenance-1")
        payload = memory_event.payload
        self.assertEqual(payload["workflow_id"], "workflow-1")
        self.assertEqual(payload["agent_id"], "agent-1")
        self.assertEqual(payload["source_conversation_id"], "conversation-1")
        self.assertEqual(payload["source_execution_id"], "execution-1")
        self.assertEqual(payload["supersedes_memory_id"], "memory-old")
        self.assertEqual(payload["metadata"]["document_id"], "doc-1")
        self.assertEqual(payload["metadata"]["chunk_index"], 2)
        self.assertEqual(payload["metadata"]["start_char"], 200)
        self.assertNotIn("storage_uri", payload["metadata"])
        self.assertNotIn("entity_hints", payload["metadata"])
        self.assertNotIn("content", payload)
        self.assertNotIn("embedding", payload)

    def test_memory_entity_extraction_requires_feature_flag(self) -> None:
        response = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-entity-disabled",
                    "scope": "user",
                    "content": "Entity extraction disabled content.",
                    "summary": "Acme Launch Plan",
                    "metadata": {
                        "entity_hints": [{"name": "Acme Corp", "type": "organization", "confidence": 0.95}],
                    },
                }
            },
        )
        self.assertEqual(response.status_code, 200)

        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        self.assertFalse(any(event.event_type == "memory.entities.extracted" for event in projection_events))

    def test_memory_entity_extraction_emits_confident_graph_projection_event(self) -> None:
        os.environ["GRAPH_ENTITY_EXTRACTION_ENABLED"] = "true"
        os.environ["GRAPH_ENTITY_EXTRACTION_MIN_CONFIDENCE"] = "0.7"
        reset_settings_cache()

        response = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-entity-enabled",
                    "scope": "user",
                    "content": "Raw memory content is not projected.",
                    "summary": "Acme Launch Plan",
                    "tags": ["entity:Launch Plan"],
                    "metadata": {
                        "entity_hints": [
                            {"name": "Acme Corp", "type": "organization", "confidence": 0.95},
                            {"name": "Weak Guess", "type": "concept", "confidence": 0.2},
                        ],
                    },
                }
            },
        )
        self.assertEqual(response.status_code, 200)

        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        entity_events = [event for event in projection_events if event.event_type == "memory.entities.extracted"]
        self.assertEqual(len(entity_events), 1)
        entity_event = entity_events[0]
        self.assertEqual(entity_event.aggregate_id, "memory-entity-enabled")
        self.assertEqual(entity_event.source, "memory_entity_extraction")
        entity_ids = {entity["id"] for entity in entity_event.payload["entities"]}
        self.assertIn("entity:organization:acme-corp", entity_ids)
        self.assertIn("entity:concept:launch-plan", entity_ids)
        self.assertNotIn("entity:concept:weak-guess", entity_ids)
        projected_hints = next(
            event.payload["metadata"]["entity_hints"]
            for event in projection_events
            if event.aggregate_id == "memory-entity-enabled" and event.event_type == "memory.created"
        )
        self.assertEqual(projected_hints[0]["name"], "Acme Corp")
        self.assertEqual(projected_hints[0]["confidence"], 0.95)
        self.assertNotIn("content", entity_event.payload)
        self.assertNotIn("embedding", entity_event.payload)

    def test_delete_document_memories_removes_matching_chunks(self) -> None:
        for memory_id, document_id, tags in (
            ("memory-doc-1", "document-1", ["workflow-rag", "workflow:workflow-1", "task:task-1"]),
            ("memory-doc-2", "document-1", ["workflow-rag", "workflow:workflow-1", "task:task-1"]),
            ("memory-doc-3", "document-2", ["workflow-rag", "workflow:workflow-1", "task:task-2"]),
        ):
            response = self.client.post(
                "/memories",
                headers=self.admin_headers,
                json={
                    "memory": {
                        "id": memory_id,
                        "scope": "workflow",
                        "workflow_id": "workflow-1",
                        "content": f"Chunk for {document_id}",
                        "tags": tags,
                        "source": "document_upload",
                        "memory_type": "archive",
                        "metadata": {
                            "document_id": document_id,
                            "filename": f"{document_id}.md",
                        },
                    }
                },
            )
            self.assertEqual(response.status_code, 200)

        delete_response = self.client.delete(
            "/memories/documents/document-1",
            headers=self.admin_headers,
            params={"scope": "workflow", "workflow_id": "workflow-1", "tag": "task:task-1"},
        )
        self.assertEqual(delete_response.status_code, 200)
        payload = delete_response.json()
        self.assertEqual(payload["document_id"], "document-1")
        self.assertEqual(payload["deleted_count"], 2)
        self.assertEqual(set(payload["memory_ids"]), {"memory-doc-1", "memory-doc-2"})

        remaining = self.client.get(
            "/memories",
            headers=self.admin_headers,
            params={"scope": "workflow", "workflow_id": "workflow-1", "source": "document_upload"},
        )
        self.assertEqual(remaining.status_code, 200)
        self.assertEqual([item["id"] for item in remaining.json()["items"]], ["memory-doc-3"])
        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        document_events = [
            event
            for event in projection_events
            if event.event_type == "document_memory_collection.deleted"
        ]
        self.assertEqual(len(document_events), 1)
        self.assertEqual(document_events[0].aggregate_id, "document-1")
        self.assertEqual(set(document_events[0].payload["memory_ids"]), {"memory-doc-1", "memory-doc-2"})

    def test_sensitive_memory_requires_confirmation(self) -> None:
        blocked = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-sensitive",
                    "scope": "user",
                    "content": "The user's API key is sk-test.",
                    "sensitive": True,
                }
            },
        )
        self.assertEqual(blocked.status_code, 409)

        confirmed = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "confirmed": True,
                "memory": {
                    "id": "memory-sensitive",
                    "scope": "user",
                    "content": "The user's API key is sk-test.",
                    "sensitive": True,
                },
            },
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertTrue(confirmed.json()["sensitive"])

    def test_workspace_memory_access_uses_owner_metadata(self) -> None:
        created = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "workspace-memory-1",
                    "scope": "workspace",
                    "workspace_id": "workspace-1",
                    "content": "Workspace launch plans should use the enterprise template.",
                }
            },
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["metadata"]["owner_ids"], ["user-1"])

        blocked = self.client.get("/memories/workspace-memory-1", headers=self.user_2_headers)
        self.assertEqual(blocked.status_code, 403)

        allowed = self.client.get("/memories/workspace-memory-1", headers=self.user_1_headers)
        self.assertEqual(allowed.status_code, 200)

    def test_memory_search_ranks_semantic_matches_before_recency(self) -> None:
        recent = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-recent",
                    "scope": "user",
                    "content": "The user likes concise meeting notes.",
                }
            },
        )
        self.assertEqual(recent.status_code, 200)
        relevant = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-relevant",
                    "scope": "user",
                    "content": "The user prefers timezone scheduling in Asia/Singapore.",
                    "tags": ["timezone", "scheduling"],
                }
            },
        )
        self.assertEqual(relevant.status_code, 200)

        response = self.client.get(
            "/memories",
            headers=self.user_1_headers,
            params={"q": "timezone scheduling", "limit": 2},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["id"], "memory-relevant")

    def test_memory_create_stores_embedding_when_profile_configured(self) -> None:
        self._enable_fake_embeddings()

        response = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-embedded",
                    "scope": "user",
                    "content": "Billing invoices require finance approval.",
                }
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["embedding_model_profile_id"], "profile-embedding")
        self.assertEqual(payload["embedding_dimensions"], 2)
        self.assertEqual(payload["embedding"], [1.0, 0.0])

    def test_memory_create_and_query_supports_summary_fields(self) -> None:
        asyncio.run(
            self.context.conversation_repo.create(
                Conversation(
                    id="conversation-1",
                    created_by_user_id="user-1",
                    channel_type="api",
                )
            )
        )
        response = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-summary-1",
                    "scope": "conversation",
                    "conversation_id": "conversation-1",
                    "content": "The day focused on memory architecture and rollout sequencing.",
                    "summary": "Locked the DB-first memory rollout plan.",
                    "memory_type": "daily_summary",
                    "status": "active",
                    "importance": 60,
                    "summary_date": "2026-05-07",
                    "archived_window_start": "2026-05-07T00:00:00Z",
                    "archived_window_end": "2026-05-07T23:59:59Z",
                    "source_conversation_id": "conversation-1",
                }
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["memory_type"], "daily_summary")
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["importance"], 60)
        self.assertEqual(payload["summary_date"], "2026-05-07")
        self.assertEqual(payload["source_conversation_id"], "conversation-1")

        listed = self.client.get(
            "/memories",
            headers=self.user_1_headers,
            params={"conversation_id": "conversation-1"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"][0]["memory_type"], "daily_summary")

        filtered = self.client.get(
            "/memories",
            headers=self.user_1_headers,
            params={
                "memory_type": "daily_summary",
                "status": "active",
                "source_conversation_id": "conversation-1",
                "summary_date_from": "2026-05-07",
                "summary_date_to": "2026-05-07",
            },
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual([item["id"] for item in filtered.json()["items"]], ["memory-summary-1"])

    def test_daily_summary_admin_run_endpoint_creates_memory(self) -> None:
        os.environ["MEMORY_DAILY_SUMMARY_ENABLED"] = "true"
        reset_settings_cache()
        asyncio.run(
            self.context.conversation_repo.create(
                Conversation(
                    id="conversation-summary-run",
                    created_by_user_id="user-1",
                    main_agent_profile_id="main-agent-profile",
                    channel_type="api",
                )
            )
        )
        asyncio.run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-summary-run-1",
                    conversation_id="conversation-summary-run",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Please capture the rollout decisions for today.",
                    created_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
                )
            )
        )
        asyncio.run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-summary-run-2",
                    conversation_id="conversation-summary-run",
                    role=ConversationRole.ASSISTANT,
                    message_type=ConversationMessageType.ASSISTANT_TEXT,
                    plain_text="We locked the DB-first memory approach.",
                    created_at=datetime(2026, 5, 7, 12, 5, tzinfo=timezone.utc),
                )
            )
        )

        forbidden = self.client.post(
            "/memories/daily-summaries/run",
            headers=self.user_1_headers,
            json={"target_date": "2026-05-07", "conversation_id": "conversation-summary-run"},
        )
        self.assertEqual(forbidden.status_code, 403)

        response = self.client.post(
            "/memories/daily-summaries/run",
            headers=self.admin_headers,
            json={"target_date": "2026-05-07", "conversation_id": "conversation-summary-run"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 1)

        listed = self.client.get(
            "/memories",
            headers=self.user_1_headers,
            params={
                "memory_type": "daily_summary",
                "source_conversation_id": "conversation-summary-run",
            },
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["items"]), 1)

    def test_daily_summary_backfill_endpoint_aggregates_days(self) -> None:
        asyncio.run(
            self.context.conversation_repo.create(
                Conversation(
                    id="conversation-summary-backfill",
                    created_by_user_id="user-1",
                    main_agent_profile_id="main-agent-profile",
                    channel_type="api",
                )
            )
        )
        for message_id, text, created_at in [
            ("backfill-message-1", "Day one memory notes.", datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc)),
            ("backfill-message-2", "Day two memory notes.", datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)),
        ]:
            asyncio.run(
                self.context.conversation_message_repo.create(
                    ConversationMessage(
                        id=message_id,
                        conversation_id="conversation-summary-backfill",
                        role=ConversationRole.USER,
                        message_type=ConversationMessageType.USER_TEXT,
                        plain_text=text,
                        created_at=created_at,
                    )
                )
            )

        response = self.client.post(
            "/memories/daily-summaries/backfill",
            headers=self.admin_headers,
            json={
                "start_date": "2026-05-05",
                "end_date": "2026-05-06",
                "conversation_id": "conversation-summary-backfill",
                "dry_run": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["processed"], 2)
        self.assertEqual(len(payload["runs"]), 2)

    def test_compact_backfill_dry_run_reports_would_create_without_memory(self) -> None:
        asyncio.run(
            self.context.conversation_repo.create(
                Conversation(
                    id="conversation-compact-backfill-dry-run",
                    created_by_user_id="user-1",
                    channel_type="api",
                )
            )
        )
        asyncio.run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-compact-backfill-dry-run",
                    conversation_id="conversation-compact-backfill-dry-run",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Summarize this conversation for handoff.",
                )
            )
        )

        response = self.client.post(
            "/memories/compact/backfill",
            headers=self.admin_headers,
            json={
                "conversation_id": "conversation-compact-backfill-dry-run",
                "mode": "handoff",
                "dry_run": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["processed"], 1)
        self.assertEqual(payload["created"], 0)
        self.assertEqual(payload["results"][0]["status"], "would_create")
        self.assertIn("progress", payload)
        self.assertIn("progress", payload["results"][0])
        self.assertEqual(payload["progress"]["failed_steps"], 0)
        self.assertIn("finish", [event["step"] for event in payload["progress"]["events"]])
        memories = asyncio.run(self.context.memory_repo.list())
        self.assertEqual(memories, [])

    def test_compact_backfill_creates_context_pack(self) -> None:
        asyncio.run(
            self.context.conversation_repo.create(
                Conversation(
                    id="conversation-compact-backfill-create",
                    created_by_user_id="user-1",
                    channel_type="api",
                )
            )
        )
        asyncio.run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-compact-backfill-create",
                    conversation_id="conversation-compact-backfill-create",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Decision: use context packs for reusable state.",
                )
            )
        )

        response = self.client.post(
            "/memories/compact/backfill",
            headers=self.admin_headers,
            json={
                "conversation_id": "conversation-compact-backfill-create",
                "mode": "technical",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["created"], 1)
        self.assertEqual(payload["progress"]["failed_steps"], 0)
        self.assertIn("progress", payload["results"][0])
        memory_id = payload["results"][0]["memory_id"]
        memory = asyncio.run(self.context.memory_repo.get(memory_id))
        assert memory is not None
        self.assertEqual(memory.memory_type.value, "context_pack")
        self.assertEqual(memory.metadata["mode"], "technical")
        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        memory_event = next(event for event in projection_events if event.aggregate_id == memory_id)
        self.assertEqual(memory_event.event_type, "memory.created")
        self.assertEqual(memory_event.payload["memory_type"], "context_pack")
        self.assertEqual(memory_event.payload["source_conversation_id"], "conversation-compact-backfill-create")
        self.assertEqual(memory_event.payload["metadata"]["mode"], "technical")
        self.assertEqual(memory_event.payload["metadata"]["source_range"], "full")
        self.assertEqual(memory_event.payload["metadata"]["source_message_start_id"], "message-compact-backfill-create")
        self.assertEqual(memory_event.payload["metadata"]["source_message_end_id"], "message-compact-backfill-create")
        self.assertEqual(memory_event.payload["metadata"]["source_message_count"], 1)
        self.assertNotIn("content", memory_event.payload)
        self.assertNotIn("structured", memory_event.payload["metadata"])

    def test_compact_backfill_skips_existing_active_pack(self) -> None:
        asyncio.run(
            self.context.conversation_repo.create(
                Conversation(
                    id="conversation-compact-backfill-existing",
                    created_by_user_id="user-1",
                    channel_type="api",
                )
            )
        )
        asyncio.run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-compact-backfill-existing",
                    conversation_id="conversation-compact-backfill-existing",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Prepare a handoff pack.",
                )
            )
        )
        created = self.client.post(
            "/memories/compact/backfill",
            headers=self.admin_headers,
            json={
                "conversation_id": "conversation-compact-backfill-existing",
                "mode": "handoff",
            },
        )
        self.assertEqual(created.status_code, 200)

        skipped = self.client.post(
            "/memories/compact/backfill",
            headers=self.admin_headers,
            json={
                "conversation_id": "conversation-compact-backfill-existing",
                "mode": "handoff",
                "skip_existing": True,
            },
        )

        self.assertEqual(skipped.status_code, 200)
        payload = skipped.json()
        self.assertEqual(payload["created"], 0)
        self.assertEqual(payload["skipped"], 1)
        self.assertEqual(payload["results"][0]["reason"], "existing_active_context_pack")

    def test_compact_backfill_respects_feature_flag(self) -> None:
        with patch.dict(os.environ, {"MEMORY_CONTEXT_PACK_ENABLED": "false"}, clear=False):
            reset_settings_cache()
            response = self.client.post(
                "/memories/compact/backfill",
                headers=self.admin_headers,
                json={"mode": "handoff", "dry_run": True},
            )
            reset_settings_cache()

        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"])

    def test_compact_backfill_filters_by_user_and_workspace(self) -> None:
        for conversation_id, user_id, workspace_id in [
            ("conversation-compact-backfill-filter-1", "user-1", "workspace-a"),
            ("conversation-compact-backfill-filter-2", "user-2", "workspace-a"),
            ("conversation-compact-backfill-filter-3", "user-1", "workspace-b"),
        ]:
            asyncio.run(
                self.context.conversation_repo.create(
                    Conversation(
                        id=conversation_id,
                        created_by_user_id=user_id,
                        workspace_id=workspace_id,
                        channel_type="api",
                    )
                )
            )
            asyncio.run(
                self.context.conversation_message_repo.create(
                    ConversationMessage(
                        id=f"message-{conversation_id}",
                        conversation_id=conversation_id,
                        role=ConversationRole.USER,
                        message_type=ConversationMessageType.USER_TEXT,
                        plain_text=f"Create a context pack for {conversation_id}.",
                    )
                )
            )

        response = self.client.post(
            "/memories/compact/backfill",
            headers=self.admin_headers,
            json={
                "mode": "brief",
                "dry_run": True,
                "user_id": "user-1",
                "workspace_id": "workspace-a",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["processed"], 1)
        self.assertEqual(payload["filters"]["user_id"], "user-1")
        self.assertEqual(payload["filters"]["workspace_id"], "workspace-a")
        self.assertEqual(
            [item["conversation_id"] for item in payload["results"]],
            ["conversation-compact-backfill-filter-1"],
        )

    def test_compact_backfill_conversation_id_can_be_filtered_out(self) -> None:
        asyncio.run(
            self.context.conversation_repo.create(
                Conversation(
                    id="conversation-compact-backfill-filtered-out",
                    created_by_user_id="user-1",
                    workspace_id="workspace-a",
                    channel_type="api",
                )
            )
        )
        asyncio.run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-compact-backfill-filtered-out",
                    conversation_id="conversation-compact-backfill-filtered-out",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="This should not match the requested workspace filter.",
                )
            )
        )

        response = self.client.post(
            "/memories/compact/backfill",
            headers=self.admin_headers,
            json={
                "conversation_id": "conversation-compact-backfill-filtered-out",
                "workspace_id": "workspace-b",
                "dry_run": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["processed"], 0)
        self.assertEqual(payload["skipped"], 1)
        self.assertEqual(payload["results"][0]["reason"], "conversation_filtered_out")

    def test_compact_backfill_idempotency_key_returns_existing_pack(self) -> None:
        asyncio.run(
            self.context.conversation_repo.create(
                Conversation(
                    id="conversation-compact-backfill-idempotent",
                    created_by_user_id="user-1",
                    channel_type="api",
                )
            )
        )
        asyncio.run(
            self.context.conversation_message_repo.create(
                ConversationMessage(
                    id="message-compact-backfill-idempotent",
                    conversation_id="conversation-compact-backfill-idempotent",
                    role=ConversationRole.USER,
                    message_type=ConversationMessageType.USER_TEXT,
                    plain_text="Create this compact pack once.",
                )
            )
        )

        first = self.client.post(
            "/memories/compact/backfill",
            headers=self.admin_headers,
            json={
                "conversation_id": "conversation-compact-backfill-idempotent",
                "idempotency_key": "backfill-request-1",
            },
        )
        second = self.client.post(
            "/memories/compact/backfill",
            headers=self.admin_headers,
            json={
                "conversation_id": "conversation-compact-backfill-idempotent",
                "idempotency_key": "backfill-request-1",
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["created"], 1)
        self.assertEqual(second.json()["created"], 0)
        self.assertEqual(second.json()["results"][0]["status"], "existing")
        self.assertEqual(
            second.json()["results"][0]["memory_id"],
            first.json()["results"][0]["memory_id"],
        )

    def test_memory_catalog_groups_linkable_memory_resources(self) -> None:
        asyncio.run(
            self.context.conversation_repo.create(
                Conversation(
                    id="conversation-memory-catalog",
                    created_by_user_id="user-1",
                    workspace_id="workspace-catalog",
                    channel_type="api",
                )
            )
        )
        for memory in [
            {
                "id": "catalog-manual-decision",
                "scope": "user",
                "content": "Use compact memory packs for long handoffs.",
                "summary": "Compact handoff decision.",
                "memory_type": "decision",
                "tags": ["decision", "handoff"],
            },
            {
                "id": "catalog-context-pack",
                "scope": "conversation",
                "conversation_id": "conversation-memory-catalog",
                "source_conversation_id": "conversation-memory-catalog",
                "source": "compact_tool",
                "content": "Compact context pack for the memory ops rework.",
                "summary": "Memory ops context pack.",
                "memory_type": "context_pack",
                "tags": ["context_pack", "handoff"],
                "metadata": {"mode": "handoff"},
            },
            {
                "id": "catalog-daily-summary",
                "scope": "conversation",
                "conversation_id": "conversation-memory-catalog",
                "source_conversation_id": "conversation-memory-catalog",
                "content": "Daily summary of the memory architecture discussion.",
                "summary": "Memory architecture daily summary.",
                "memory_type": "daily_summary",
                "summary_date": "2026-05-23",
                "archived_window_start": "2026-05-23T00:00:00+00:00",
                "archived_window_end": "2026-05-23T23:59:59+00:00",
                "tags": ["daily_summary"],
            },
            {
                "id": "catalog-run-summary",
                "scope": "user",
                "content": "Run summary says vector backfill completed.",
                "summary": "Vector backfill run summary.",
                "memory_type": "run_summary",
                "source_execution_id": "execution-catalog",
                "tags": ["run_summary"],
            },
            {
                "id": "catalog-document-chunk-1",
                "scope": "user",
                "source": "document_upload",
                "content": "Document chunk one covers memory catalog requirements.",
                "summary": "Catalog requirements chunk.",
                "memory_type": "archive",
                "tags": ["document", "catalog"],
                "metadata": {"document_id": "document-catalog", "filename": "memory-catalog.md"},
            },
            {
                "id": "catalog-document-chunk-2",
                "scope": "user",
                "source": "document_upload",
                "content": "Document chunk two covers graph node linking.",
                "summary": "Graph linking chunk.",
                "memory_type": "archive",
                "tags": ["document", "graph"],
                "metadata": {"document_id": "document-catalog", "filename": "memory-catalog.md"},
            },
        ]:
            response = self.client.post(
                "/memories",
                headers=self.user_1_headers,
                json={"memory": memory},
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.get(
            "/memories/catalog",
            headers=self.user_1_headers,
            params={"limit_per_group": 10},
        )

        self.assertEqual(response.status_code, 200)
        groups = {group["key"]: group for group in response.json()["groups"]}
        self.assertEqual(groups["manual"]["items"][0]["id"], "catalog-manual-decision")
        self.assertEqual(groups["manual"]["items"][0]["memoryType"], "decision")
        self.assertEqual(groups["compact_packs"]["items"][0]["mode"], "handoff")
        self.assertEqual(groups["compact_packs"]["items"][0]["memoryType"], "context_pack")
        self.assertEqual(groups["conversation_summaries"]["items"][0]["id"], "catalog-daily-summary")
        self.assertEqual(groups["run_summaries"]["items"][0]["id"], "catalog-run-summary")
        document_item = groups["documents"]["items"][0]
        self.assertEqual(document_item["refType"], "memory_collection")
        self.assertEqual(document_item["memoryType"], "archive")
        self.assertEqual(document_item["documentId"], "document-catalog")
        self.assertEqual(document_item["documentFilename"], "memory-catalog.md")
        self.assertEqual(document_item["chunkCount"], 2)
        self.assertEqual(set(document_item["memoryIds"]), {"catalog-document-chunk-1", "catalog-document-chunk-2"})

        search_response = self.client.get(
            "/memories/catalog",
            headers=self.user_1_headers,
            params={"q": "graph node linking", "limit_per_group": 10},
        )
        self.assertEqual(search_response.status_code, 200)
        search_groups = {group["key"]: group for group in search_response.json()["groups"]}
        self.assertEqual(search_groups["documents"]["items"][0]["documentId"], "document-catalog")

    def test_memory_catalog_filters_sensitive_and_scope_access(self) -> None:
        hidden = self.client.post(
            "/memories",
            headers=self.user_2_headers,
            json={
                "memory": {
                    "id": "catalog-other-user",
                    "scope": "user",
                    "content": "Other user's private memory.",
                    "summary": "Other user memory.",
                }
            },
        )
        self.assertEqual(hidden.status_code, 200)
        sensitive = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "confirmed": True,
                "memory": {
                    "id": "catalog-sensitive",
                    "scope": "user",
                    "content": "The secret launch token is fake-token.",
                    "summary": "Sensitive launch token.",
                    "sensitive": True,
                },
            },
        )
        self.assertEqual(sensitive.status_code, 200)
        visible = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "catalog-visible",
                    "scope": "user",
                    "content": "Visible memory for catalog search.",
                    "summary": "Visible catalog memory.",
                }
            },
        )
        self.assertEqual(visible.status_code, 200)

        default_response = self.client.get("/memories/catalog", headers=self.user_1_headers)
        self.assertEqual(default_response.status_code, 200)
        default_ids = {
            item["id"]
            for group in default_response.json()["groups"]
            for item in group["items"]
        }
        self.assertIn("catalog-visible", default_ids)
        self.assertNotIn("catalog-sensitive", default_ids)
        self.assertNotIn("catalog-other-user", default_ids)

        sensitive_response = self.client.get(
            "/memories/catalog",
            headers=self.user_1_headers,
            params={"include_sensitive": True},
        )
        self.assertEqual(sensitive_response.status_code, 200)
        sensitive_items = [
            item
            for group in sensitive_response.json()["groups"]
            for item in group["items"]
            if item["id"] == "catalog-sensitive"
        ]
        self.assertEqual(len(sensitive_items), 1)
        self.assertFalse(sensitive_items[0]["canLink"])
        self.assertIn("Sensitive", sensitive_items[0]["blockedReason"])

    def test_memory_exclusions_mark_catalog_items_unlinkable_for_target(self) -> None:
        created = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "catalog-excluded-memory",
                    "scope": "user",
                    "content": "Memory should be skipped for workflow planning.",
                    "summary": "Workflow skip memory.",
                }
            },
        )
        self.assertEqual(created.status_code, 200)

        added = self.client.post(
            "/memories/catalog-excluded-memory/exclusions",
            headers=self.user_1_headers,
            json={
                "targetType": "workflow",
                "targetId": "workflow-memory-skip",
                "reason": "Do not use this memory for the workflow rework.",
            },
        )

        self.assertEqual(added.status_code, 200)
        exclusion = added.json()
        self.assertEqual(exclusion["memoryId"], "catalog-excluded-memory")
        self.assertEqual(exclusion["targetType"], "workflow")
        self.assertEqual(exclusion["targetId"], "workflow-memory-skip")

        stored = self.client.get("/memories/catalog-excluded-memory", headers=self.user_1_headers)
        self.assertEqual(stored.status_code, 200)
        self.assertNotIn("memory_id", stored.json()["metadata"]["exclusions"][0])

        untargeted_catalog = self.client.get(
            "/memories/catalog",
            headers=self.user_1_headers,
            params={"q": "workflow skip"},
        )
        self.assertEqual(untargeted_catalog.status_code, 200)
        untargeted_items = [
            item
            for group in untargeted_catalog.json()["groups"]
            for item in group["items"]
            if item["id"] == "catalog-excluded-memory"
        ]
        self.assertEqual(len(untargeted_items), 1)
        self.assertFalse(untargeted_items[0]["excluded"])
        self.assertTrue(untargeted_items[0]["canLink"])

        targeted_catalog = self.client.get(
            "/memories/catalog",
            headers=self.user_1_headers,
            params={
                "q": "workflow skip",
                "target_type": "workflow",
                "target_id": "workflow-memory-skip",
            },
        )
        self.assertEqual(targeted_catalog.status_code, 200)
        targeted_items = [
            item
            for group in targeted_catalog.json()["groups"]
            for item in group["items"]
            if item["id"] == "catalog-excluded-memory"
        ]
        self.assertEqual(len(targeted_items), 1)
        self.assertTrue(targeted_items[0]["excluded"])
        self.assertFalse(targeted_items[0]["canLink"])
        self.assertEqual(
            targeted_items[0]["exclusionReason"],
            "Do not use this memory for the workflow rework.",
        )
        self.assertEqual(targeted_items[0]["excludedFor"][0]["id"], exclusion["id"])

        listed = self.client.get(
            "/memories/exclusions",
            headers=self.user_1_headers,
            params={"target_type": "workflow", "target_id": "workflow-memory-skip"},
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item["id"] for item in listed.json()["items"]], [exclusion["id"]])

        deleted = self.client.delete(
            f"/memories/catalog-excluded-memory/exclusions/{exclusion['id']}",
            headers=self.user_1_headers,
        )
        self.assertEqual(deleted.status_code, 200)

        after_delete = self.client.get(
            "/memories/catalog",
            headers=self.user_1_headers,
            params={
                "q": "workflow skip",
                "target_type": "workflow",
                "target_id": "workflow-memory-skip",
            },
        )
        self.assertEqual(after_delete.status_code, 200)
        after_items = [
            item
            for group in after_delete.json()["groups"]
            for item in group["items"]
            if item["id"] == "catalog-excluded-memory"
        ]
        self.assertEqual(len(after_items), 1)
        self.assertFalse(after_items[0]["excluded"])
        self.assertTrue(after_items[0]["canLink"])

    def test_global_memory_exclusion_applies_without_target_context(self) -> None:
        created = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "catalog-global-excluded-memory",
                    "scope": "user",
                    "content": "Memory should be globally skipped.",
                    "summary": "Global skip memory.",
                }
            },
        )
        self.assertEqual(created.status_code, 200)

        added = self.client.post(
            "/memories/catalog-global-excluded-memory/exclusions",
            headers=self.user_1_headers,
            json={"target_type": "global", "reason": "Outdated memory."},
        )
        self.assertEqual(added.status_code, 200)

        catalog = self.client.get(
            "/memories/catalog",
            headers=self.user_1_headers,
            params={"q": "globally skipped"},
        )
        self.assertEqual(catalog.status_code, 200)
        items = [
            item
            for group in catalog.json()["groups"]
            for item in group["items"]
            if item["id"] == "catalog-global-excluded-memory"
        ]
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["excluded"])
        self.assertFalse(items[0]["canLink"])
        self.assertEqual(items[0]["exclusionReason"], "Outdated memory.")

    def test_memory_vector_search_can_rank_without_text_match(self) -> None:
        self._enable_fake_embeddings()
        recent = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-unrelated-vector",
                    "scope": "user",
                    "content": "The user likes matcha during afternoon planning.",
                }
            },
        )
        self.assertEqual(recent.status_code, 200)
        relevant = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-vector-relevant",
                    "scope": "user",
                    "content": "Invoices must be reconciled before finance reports are sent.",
                }
            },
        )
        self.assertEqual(relevant.status_code, 200)

        response = self.client.get(
            "/memories",
            headers=self.user_1_headers,
            params={"q": "refund chargeback", "limit": 2},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["id"], "memory-vector-relevant")

    def test_memory_vector_search_is_unchanged_by_graph_entity_projection(self) -> None:
        os.environ["GRAPH_PROJECTION_ENABLED"] = "true"
        os.environ["GRAPH_ENTITY_EXTRACTION_ENABLED"] = "true"
        os.environ["GRAPH_ENTITY_EXTRACTION_MIN_CONFIDENCE"] = "0.7"
        self._enable_fake_embeddings()

        unrelated = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-graph-vector-unrelated",
                    "scope": "user",
                    "content": "The user likes matcha during afternoon planning.",
                    "summary": "Planning beverage preference.",
                    "metadata": {
                        "entity_hints": [
                            {"name": "Planning", "type": "concept", "confidence": 0.95},
                        ],
                    },
                }
            },
        )
        self.assertEqual(unrelated.status_code, 200)
        relevant = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-graph-vector-relevant",
                    "scope": "user",
                    "content": "Invoices must be reconciled before finance reports are sent.",
                    "summary": "Finance invoice reconciliation rule.",
                    "metadata": {
                        "entity_hints": [
                            {"name": "Finance", "type": "organization", "confidence": 0.95},
                        ],
                    },
                }
            },
        )
        self.assertEqual(relevant.status_code, 200)

        response = self.client.get(
            "/memories",
            headers=self.user_1_headers,
            params={"q": "refund chargeback", "limit": 2},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["id"], "memory-graph-vector-relevant")
        projection_events = asyncio.run(self.context.graph_projection_event_repo.list_events())
        self.assertTrue(any(event.event_type == "memory.entities.extracted" for event in projection_events))
        self.assertFalse(any("embedding" in event.payload for event in projection_events))

    def test_memory_embedding_backfill_updates_existing_records(self) -> None:
        created = self.client.post(
            "/memories",
            headers=self.user_1_headers,
            json={
                "memory": {
                    "id": "memory-needs-embedding",
                    "scope": "user",
                    "content": "Billing invoices require finance approval.",
                }
            },
        )
        self.assertEqual(created.status_code, 200)
        self.assertIsNone(created.json()["embedding"])
        self._enable_fake_embeddings()

        response = self.client.post(
            "/memories/embeddings/backfill",
            headers=self.user_1_headers,
            json={"limit": 10},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["updated"], 1)

        fetched = self.client.get("/memories/memory-needs-embedding", headers=self.user_1_headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["embedding_model_profile_id"], "profile-embedding")


if __name__ == "__main__":
    unittest.main()
