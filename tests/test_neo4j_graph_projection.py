from __future__ import annotations

import os
import unittest

from app.db.repositories import InMemoryGraphProjectionEventRepository
from app.domain import GraphProjectionEvent
from app.graph.neo4j_projection import Neo4jGraphProjector, Neo4jProjectionConfig, create_neo4j_driver
from app.graph.rebuild import Neo4jGraphRebuilder
from app.core.config import get_settings, reset_settings_cache


class FakeNeo4jSession:
    def __init__(self, calls: list[dict]):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, exc, _tb):
        return None

    async def run(self, cypher: str, **params):
        self.calls.append({"cypher": cypher, "params": params})


class FakeNeo4jDriver:
    def __init__(self):
        self.calls: list[dict] = []
        self.session_kwargs: list[dict] = []
        self.closed = False

    def session(self, **kwargs):
        self.session_kwargs.append(kwargs)
        return FakeNeo4jSession(self.calls)

    async def close(self):
        self.closed = True


class Neo4jGraphProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_schema_runs_constraints(self) -> None:
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        await projector.ensure_schema()

        self.assertGreaterEqual(len(driver.calls), 1)
        self.assertTrue(all("CREATE CONSTRAINT" in call["cypher"] for call in driver.calls))

    async def test_projects_memory_and_document_events(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        memory_event = await repo.append(
            GraphProjectionEvent(
                event_type="memory.created",
                aggregate_type="memory",
                aggregate_id="memory-1",
                payload={
                    "memory_id": "memory-1",
                    "scope": "user",
                    "summary": "Useful memory.",
                    "tags": ["document", "roadmap"],
                    "sensitive": False,
                    "created_by_user_id": "user-1",
                    "workflow_id": "workflow-1",
                    "agent_id": "agent-1",
                    "conversation_id": "conversation-1",
                    "source": "manual",
                    "memory_type": "fact",
                    "status": "active",
                    "importance": 80,
                    "archived_window_start": "2026-05-23T00:00:00+00:00",
                    "archived_window_end": "2026-05-23T23:59:59+00:00",
                    "source_conversation_id": "conversation-source-1",
                    "source_execution_id": "execution-1",
                    "supersedes_memory_id": "memory-0",
                    "metadata": {
                        "document_id": "doc-1",
                        "filename": "roadmap.md",
                        "content_type": "text/markdown",
                        "content_sha256": "abc",
                        "chunk_index": 0,
                        "chunk_count": 1,
                        "start_char": 0,
                        "end_char": 120,
                        "semantic_hint": "Document upload roadmap.md",
                        "mode": "technical",
                        "source_range": "full",
                        "source_message_start_id": "message-1",
                        "source_message_end_id": "message-2",
                        "source_message_start_at": "2026-05-23T10:00:00+00:00",
                        "source_message_end_at": "2026-05-23T11:00:00+00:00",
                        "source_message_count": 2,
                    },
                    "updated_at": "2026-05-24T00:00:00+00:00",
                },
            )
        )
        document_event = await repo.append(
            GraphProjectionEvent(
                event_type="document_memory_collection.created",
                aggregate_type="document_memory_collection",
                aggregate_id="doc-1",
                payload={
                    "document_id": "doc-1",
                    "scope": "workflow",
                    "created_by_user_id": "user-1",
                    "workflow_id": "workflow-1",
                    "agent_id": "agent-1",
                    "filename": "roadmap.md",
                    "content_type": "text/markdown",
                    "content_sha256": "abc",
                    "chunk_count": 3,
                    "projected_chunk_count": 1,
                    "omitted_chunk_count": 2,
                    "projection_capped": True,
                    "memory_ids": ["memory-1"],
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 2)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.checkpoint_event_id, document_event.event_id)
        self.assertEqual(await repo.list_events(status="pending"), [])
        calls = "\n".join(call["cypher"] for call in driver.calls)
        self.assertIn("MERGE (m:Memory", calls)
        self.assertIn("MERGE (d:Document", calls)
        self.assertIn("DocumentChunk", calls)
        self.assertIn("HAS_CHUNK", calls)
        self.assertIn("SOURCE_DOCUMENT", calls)
        self.assertIn("CREATED_MEMORY", calls)
        self.assertIn("OWNS_DOCUMENT", calls)
        self.assertIn("AVAILABLE_TO", calls)
        self.assertIn("SOURCE_CONVERSATION", calls)
        self.assertIn("SOURCE_EXECUTION", calls)
        self.assertIn("SUPERSEDES", calls)
        memory_params = driver.calls[0]["params"]
        self.assertEqual(memory_params["memory_id"], memory_event.aggregate_id)
        self.assertEqual(memory_params["created_by_user_id"], "user-1")
        self.assertEqual(memory_params["workflow_id"], "workflow-1")
        self.assertEqual(memory_params["source_execution_id"], "execution-1")
        self.assertTrue(memory_params["missing_embedding"])
        self.assertEqual(memory_params["document_id"], "doc-1")
        self.assertEqual(memory_params["chunk_index"], 0)
        self.assertEqual(memory_params["start_char"], 0)
        self.assertEqual(memory_params["archived_window_start"], "2026-05-23T00:00:00+00:00")
        self.assertEqual(memory_params["started_at"], "2026-05-23T00:00:00+00:00")
        self.assertEqual(memory_params["ended_at"], "2026-05-23T23:59:59+00:00")
        self.assertEqual(memory_params["mode"], "technical")
        self.assertEqual(memory_params["source_message_start_id"], "message-1")
        self.assertEqual(memory_params["source_message_count"], 2)
        self.assertIn("tenant_id", memory_params)
        self.assertIsNone(memory_params["tenant_id"])
        self.assertNotIn("content", memory_params)
        self.assertNotIn("embedding", memory_params)
        document_params = driver.calls[1]["params"]
        self.assertEqual(document_params["scope"], "workflow")
        self.assertEqual(document_params["created_by_user_id"], "user-1")
        self.assertEqual(document_params["workflow_id"], "workflow-1")
        self.assertEqual(document_params["projected_chunk_count"], 1)
        self.assertEqual(document_params["omitted_chunk_count"], 2)
        self.assertTrue(document_params["projection_capped"])
        self.assertEqual(document_params["memory_ids"], ["memory-1"])
        self.assertEqual(
            document_params["chunks"],
            [{"id": "doc-1:chunk:0", "memory_id": "memory-1", "chunk_index": 0}],
        )

    async def test_memory_projection_schema_includes_memory_provenance_labels(self) -> None:
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        await projector.ensure_schema()

        calls = "\n".join(call["cypher"] for call in driver.calls)
        self.assertIn("agency_user_id", calls)
        self.assertIn("agency_conversation_id", calls)
        self.assertIn("agency_entity_id", calls)
        self.assertIn("agency_context_pack_id", calls)
        self.assertIn("agency_persona_id", calls)
        self.assertIn("agency_distillation_item_id", calls)
        self.assertIn("agency_person_id", calls)
        self.assertIn("agency_knowledge_id", calls)
        self.assertIn("agency_organization_id", calls)
        self.assertIn("agency_event_id", calls)
        self.assertIn("agency_device_id", calls)
        self.assertIn("agency_device_event_id", calls)
        self.assertIn("agency_device_command_id", calls)
        self.assertIn("agency_room_id", calls)
        self.assertIn("agency_adapter_id", calls)
        self.assertIn("agency_location_id", calls)

    async def test_projects_physical_device_event_and_command_graph(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="physical.device.state.updated",
                aggregate_type="physical_device",
                aggregate_id="device-light-1",
                payload={
                    "device_id": "device-light-1",
                    "name": "Living Room Main",
                    "type": "iot_actuator",
                    "vendor": "Aqara",
                    "model": "Light",
                    "location_id": "home",
                    "room": "Living Room",
                    "capabilities": ["turn_on_off", "set_brightness"],
                    "status": "online",
                    "metadata_keys": ["source_entity_id"],
                    "last_seen_at": "2026-06-25T00:00:00+00:00",
                    "state": {
                        "online": True,
                        "battery_level": 90,
                        "network_status": "lan",
                        "current_activity": "on",
                        "sensor_keys": ["brightness"],
                        "last_telemetry_at": "2026-06-25T00:00:00+00:00",
                    },
                },
            )
        )
        await repo.append(
            GraphProjectionEvent(
                event_type="physical.device.command.sent",
                aggregate_type="physical_device_command",
                aggregate_id="command-1",
                payload={
                    "command_id": "command-1",
                    "device_id": "device-light-1",
                    "command_type": "set_brightness",
                    "payload_keys": ["brightness"],
                    "context_memory_ids": ["memory-pref-1"],
                    "priority": "normal",
                    "requested_by": "agent:operator",
                    "status": "sent",
                    "created_at": "2026-06-25T00:00:01+00:00",
                },
            )
        )
        await repo.append(
            GraphProjectionEvent(
                event_type="physical.device.event.ingested",
                aggregate_type="physical_device_event",
                aggregate_id="event-1",
                payload={
                    "event_id": "event-1",
                    "device_id": "device-light-1",
                    "event_type": "home.light.changed",
                    "source": "home_assistant",
                    "payload_keys": ["state"],
                    "correlation_id": "command-1",
                    "timestamp": "2026-06-25T00:00:02+00:00",
                },
            )
        )
        await repo.append(
            GraphProjectionEvent(
                event_type="physical.device.workflow_triggered",
                aggregate_type="physical_device_event",
                aggregate_id="event-1",
                payload={
                    "event_id": "event-1",
                    "device_id": "device-light-1",
                    "device_event_type": "home.light.changed",
                    "trigger_id": "trigger-1",
                    "workflow_id": "workflow-evening",
                    "execution_id": "execution-evening-1",
                    "started": True,
                    "status": "queued",
                    "reason": None,
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 4)
        calls = "\n".join(call["cypher"] for call in driver.calls)
        self.assertIn("MERGE (d:Device", calls)
        self.assertIn("MERGE (r:Room", calls)
        self.assertIn("LOCATED_IN", calls)
        self.assertIn("MERGE (a:Adapter", calls)
        self.assertIn("MERGE (l:Location", calls)
        self.assertIn("MANAGES_DEVICE", calls)
        self.assertIn("LOCATED_AT", calls)
        self.assertIn("PRODUCED_DEVICE_EVENT", calls)
        self.assertIn("MERGE (c:DeviceCommand", calls)
        self.assertIn("TARGETS_DEVICE", calls)
        self.assertIn("REQUESTED_DEVICE_COMMAND", calls)
        self.assertIn("INFLUENCED_DEVICE_COMMAND", calls)
        self.assertIn("MERGE (e:DeviceEvent", calls)
        self.assertIn("EMITTED_DEVICE_EVENT", calls)
        self.assertIn("CORRELATES_WITH_COMMAND", calls)
        self.assertIn("TRIGGERED_WORKFLOW", calls)
        self.assertIn("STARTED_WORKFLOW_RUN", calls)
        self.assertEqual(driver.calls[0]["params"]["room_id"], "room:home:living-room")
        self.assertEqual(driver.calls[0]["params"]["adapter_id"], "adapter:aqara")
        self.assertEqual(driver.calls[0]["params"]["location_id"], "home")
        self.assertEqual(driver.calls[1]["params"]["command_id"], "command-1")
        self.assertEqual(driver.calls[1]["params"]["context_memory_ids"], ["memory-pref-1"])
        self.assertEqual(driver.calls[2]["params"]["event_id"], "event-1")
        self.assertEqual(driver.calls[2]["params"]["adapter_id"], "adapter:home-assistant")
        self.assertEqual(driver.calls[3]["params"]["workflow_id"], "workflow-evening")
        self.assertEqual(driver.calls[3]["params"]["execution_id"], "execution-evening-1")

    async def test_projects_persona_factory_lifecycle_graph(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="persona.factory.version.published",
                aggregate_type="persona",
                aggregate_id="persona-1",
                payload={
                    "persona_id": "persona-1",
                    "persona_slug": "audit-manager",
                    "persona_name": "Audit Manager",
                    "persona_status": "published",
                    "run_id": "run-1",
                    "persona_version_id": "version-1",
                    "version": "1.0.0",
                    "agent_id": "agent-persona-1",
                    "memory_ids": ["memory-persona-1"],
                    "source_memory_ids": ["memory-source-1"],
                    "tools": [{"id": "jira", "name": "Jira"}],
                    "workflows": [{"id": "workflow-audit-review", "name": "Audit Review"}],
                    "artifacts": [{"id": "artifact-mlp", "name": "MLP Observation"}],
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        cypher = driver.calls[0]["cypher"]
        self.assertIn("MERGE (p:Persona", cypher)
        self.assertIn("PersonaVersion", cypher)
        self.assertIn("DistillationRun", cypher)
        self.assertIn("SourceMemory", cypher)
        self.assertIn("PERSONA_HAS_VERSION", cypher)
        self.assertIn("RUN_USED_SOURCE_MEMORY", cypher)
        self.assertIn("PERSONA_USES_TOOL", cypher)
        self.assertIn("PERSONA_FOLLOWS_WORKFLOW", cypher)
        self.assertIn("PERSONA_PRODUCES_ARTIFACT", cypher)
        params = driver.calls[0]["params"]
        self.assertEqual(params["persona_id"], "persona-1")
        self.assertEqual(params["persona_slug"], "audit-manager")
        self.assertEqual(params["persona_version_id"], "version-1")
        self.assertEqual(params["source_memory_ids"], ["memory-source-1"])
        self.assertEqual(params["tools"][0]["id"], "jira")

    async def test_context_pack_memory_projects_structured_summary_nodes(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="memory.created",
                aggregate_type="memory",
                aggregate_id="context-pack-1",
                payload={
                    "memory_id": "context-pack-1",
                    "scope": "workflow",
                    "summary": "Handoff context.",
                    "memory_type": "context_pack",
                    "workflow_id": "workflow-1",
                    "source_execution_id": "run-1",
                    "metadata": {
                        "mode": "handoff",
                        "graph_context_source": "runtime_context_compaction",
                        "decisions": [{"id": "decision-1", "summary": "Keep projection idempotent."}],
                        "constraints": ["Do not project raw memory content."],
                        "open_questions": [{"id": "question-1", "text": "Should users tune graph depth?"}],
                        "next_actions": [{"id": "next-1", "title": "Backfill compact packs."}],
                    },
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        cypher = driver.calls[0]["cypher"]
        self.assertIn("ContextPack", cypher)
        self.assertIn("Decision", cypher)
        self.assertIn("Constraint", cypher)
        self.assertIn("OpenQuestion", cypher)
        self.assertIn("NextAction", cypher)
        self.assertIn("HAS_CONTEXT_PACK", cypher)
        self.assertIn("SUMMARIZES", cypher)
        params = driver.calls[0]["params"]
        self.assertEqual(params["context_pack_id"], "context-pack-1")
        self.assertEqual(params["context_pack_decisions"][0]["id"], "decision-1")
        self.assertEqual(params["context_pack_constraints"][0]["summary"], "Do not project raw memory content.")
        self.assertNotIn("content", params)

    async def test_projects_memory_entity_mentions(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="memory.entities.extracted",
                aggregate_type="memory",
                aggregate_id="memory-1",
                payload={
                    "memory_id": "memory-1",
                    "document_id": "doc-1",
                    "entities": [
                        {
                            "id": "entity:organization:acme-corp",
                            "name": "Acme Corp",
                            "normalized_name": "Acme Corp",
                            "entity_type": "organization",
                            "confidence": 0.95,
                            "source_fields": ["metadata.entity_hints"],
                            "extractor_version": "deterministic-memory-entity-v1",
                        }
                    ],
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertIn("MERGE (e:Entity", driver.calls[0]["cypher"])
        self.assertIn("MERGE (d:Document", driver.calls[0]["cypher"])
        self.assertIn("MENTIONS", driver.calls[0]["cypher"])
        self.assertEqual(driver.calls[0]["params"]["memory_id"], "memory-1")
        self.assertEqual(driver.calls[0]["params"]["document_id"], "doc-1")
        self.assertEqual(driver.calls[0]["params"]["entities"][0]["id"], "entity:organization:acme-corp")
        self.assertNotIn("content", driver.calls[0]["params"]["entities"][0])
        self.assertNotIn("embedding", driver.calls[0]["params"]["entities"][0])

    async def test_projects_approved_source_intelligence_graph_hints(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="memory.source_intelligence.graph_hints.approved",
                aggregate_type="memory",
                aggregate_id="memory-source-intelligence",
                payload={
                    "memory_id": "memory-source-intelligence",
                    "document_id": "doc-release",
                    "filename": "release-sop.md",
                    "chunk_index": 0,
                    "persona_id": "persona-release",
                    "run_id": "run-release",
                    "distillation_item_id": "item-release",
                    "item_type": "decision_pattern",
                    "memory_layer": "procedural",
                    "graph_hint_source": "persona_llm_distillation",
                    "review": {
                        "reviewed_at": "2026-05-31T00:00:00+00:00",
                        "reviewed_by_user_id": "user-1",
                    },
                    "entities": [
                        {"label": "Workflow", "name": "Release Workflow", "confidence": 0.92},
                        {"label": "Artifact", "name": "Approval Record", "confidence": 0.88},
                        {"label": "Person", "name": "Release Manager", "confidence": 0.8},
                    ],
                    "relationships": [
                        {
                            "source_name": "Release Workflow",
                            "relationship_type": "PRODUCES",
                            "target_name": "Approval Record",
                            "confidence": 0.9,
                        },
                        {
                            "source_name": "Release Manager",
                            "relationship_type": "REVIEWS",
                            "target_name": "Approval Record",
                            "confidence": 0.84,
                        },
                    ],
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        cypher = driver.calls[0]["cypher"]
        self.assertIn("source_intelligence_graph_hints_review_status", cypher)
        self.assertIn("MERGE (hint:Entity", cypher)
        self.assertIn("SET hint:Workflow", cypher)
        self.assertIn("SET hint:Artifact", cypher)
        self.assertIn("SET hint:Person", cypher)
        self.assertIn("edge:PRODUCES", cypher)
        self.assertIn("edge:REVIEWS", cypher)
        self.assertIn("MENTIONS", cypher)
        self.assertIn("DistillationItem", cypher)
        self.assertIn("ITEM_DERIVED_FROM_MEMORY", cypher)
        self.assertIn("personaMentions", cypher)
        self.assertIn("itemMentions", cypher)
        params = driver.calls[0]["params"]
        self.assertEqual(params["memory_id"], "memory-source-intelligence")
        self.assertEqual(params["document_id"], "doc-release")
        self.assertEqual(params["persona_id"], "persona-release")
        self.assertEqual(params["distillation_item_id"], "item-release")
        self.assertEqual(params["graph_hint_source"], "persona_llm_distillation")
        self.assertEqual(params["reviewed_by_user_id"], "user-1")
        self.assertEqual(len(params["entities"]), 3)
        self.assertEqual(params["entities"][0]["id"].split(":")[:2], ["source-intelligence", "workflow"])
        self.assertEqual(len(params["produces_relationships"]), 1)
        self.assertEqual(len(params["reviews_relationships"]), 1)
        self.assertEqual(params["relates_to_relationships"], [])
        self.assertNotIn("content", params["entities"][0])
        self.assertNotIn("embedding", params["entities"][0])

    async def test_projects_workflow_memory_link(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="workflow_memory_link.created",
                aggregate_type="workflow_memory_link",
                aggregate_id="link-1",
                payload={
                    "workflow_id": "workflow-1",
                    "link": {
                        "id": "link-1",
                        "targetType": "task",
                        "targetId": "task-1",
                        "refType": "memory",
                        "refId": "memory-1",
                        "memoryIds": ["memory-1"],
                        "accessMode": "read",
                        "label": "Task memory",
                    },
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertIn("LINKS_MEMORY", driver.calls[0]["cypher"])
        self.assertIn("HAS_MEMORY_LINK", driver.calls[0]["cypher"])
        self.assertIn("DEFINES_TASK", driver.calls[0]["cypher"])
        self.assertEqual(driver.calls[0]["params"]["workflow_id"], "workflow-1")
        self.assertEqual(driver.calls[0]["params"]["target_type"], "task")
        self.assertEqual(driver.calls[0]["params"]["target_id"], "task-1")
        self.assertEqual(driver.calls[0]["params"]["memory_ids"], ["memory-1"])

    async def test_workflow_memory_link_deletion_marks_target_memory_links_deleted(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="workflow_memory_link.deleted",
                aggregate_type="workflow_memory_link",
                aggregate_id="link-1",
                payload={
                    "workflow_id": "workflow-1",
                    "link": {
                        "id": "link-1",
                        "targetType": "agent",
                        "targetId": "agent-1",
                        "refType": "memory",
                        "refId": "memory-1",
                        "memoryIds": ["memory-1"],
                    },
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertIn("LINKS_MEMORY", driver.calls[0]["cypher"])
        self.assertIn("HAS_MEMORY_LINK", driver.calls[0]["cypher"])
        self.assertEqual(driver.calls[0]["params"]["workflow_id"], "workflow-1")
        self.assertEqual(driver.calls[0]["params"]["link_id"], "link-1")

    async def test_task_scoped_execution_failure_projects_step_run(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="execution.failed",
                aggregate_type="step_run",
                aggregate_id="run-1:task-a",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "task_id": "task-a",
                    "agent_id": "agent-1",
                    "status": "failed",
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        self.assertIn("MERGE (s:StepRun", driver.calls[0]["cypher"])
        self.assertIn("HAS_STEP_RUN", driver.calls[0]["cypher"])
        self.assertEqual(driver.calls[0]["params"]["step_run_id"], "run-1:task-a")
        self.assertEqual(driver.calls[0]["params"]["execution_id"], "run-1")
        self.assertEqual(driver.calls[0]["params"]["task_id"], "task-a")

    async def test_execution_failure_projects_operational_context(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="execution.failed",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="event-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "workflow_version_id": "workflow-v1",
                    "runtime_adapter_id": "native",
                    "runtime_revision_id": "revision-1",
                    "runtime_fingerprint": "fingerprint-1",
                    "trigger_type": "schedule",
                    "trigger_payload": {"schedule_id": "schedule-1"},
                    "container_id": "container-1",
                    "container_name": "agency-run-1",
                    "container_image": "agency-worker:test",
                    "container_status": "exited",
                    "container_exit_code": 1,
                    "execution_error": "Worker failed",
                    "status": "failed",
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        cypher = driver.calls[0]["cypher"]
        self.assertIn("STARTED", cypher)
        self.assertIn("TRIGGERED", cypher)
        self.assertIn("RuntimeRevision", cypher)
        self.assertIn("RuntimeContainer", cypher)
        self.assertIn("WorkflowVersion", cypher)
        self.assertIn("USED_WORKFLOW_VERSION", cypher)
        self.assertIn("FAILED_WITH", cypher)
        self.assertIn("hasRun.deleted = false", cypher)
        self.assertIn("started.deleted = false", cypher)
        params = driver.calls[0]["params"]
        self.assertEqual(params["workflow_version_id"], "workflow-v1")
        self.assertEqual(params["runtime_revision_id"], "revision-1")
        self.assertEqual(params["container_source_id"], "container-1")
        self.assertEqual(params["schedule_id"], "schedule-1")
        self.assertEqual(params["error_message"], "Worker failed")

    async def test_tool_call_event_projects_execution_event_and_tool_call(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="tool.call.failed",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="event-tool-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "sequence": 7,
                    "status": "failed",
                    "tool_call_id": "tool-call-1",
                    "payload": {"tool_name": "read_file", "error": "missing file", "content": "must not be projected"},
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        cypher = driver.calls[0]["cypher"]
        self.assertIn("ExecutionEvent", cypher)
        self.assertIn("ToolCall", cypher)
        self.assertIn("EMITTED_EVENT", cypher)
        self.assertIn("CALLED_TOOL", cypher)
        params = driver.calls[0]["params"]
        self.assertEqual(params["event_id"], "event-tool-1")
        self.assertEqual(params["tool_call_id"], "tool-call-1")
        self.assertEqual(params["tool_name"], "read_file")
        self.assertEqual(params["error_message"], "missing file")
        self.assertIn("content", params["payload_keys"])
        self.assertNotIn("content", cypher)

    async def test_execution_events_link_followed_by_sequence(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="tool.call.started",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="event-tool-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "sequence": 1,
                    "tool_call_id": "tool-call-1",
                    "payload": {"tool_name": "read_file"},
                },
            )
        )
        await repo.append(
            GraphProjectionEvent(
                event_type="tool.call.completed",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="event-tool-2",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "sequence": 2,
                    "tool_call_id": "tool-call-1",
                    "payload": {"tool_name": "read_file"},
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 2)
        self.assertEqual(result.failed, 0)
        self.assertEqual(len(driver.session_kwargs), 1)
        self.assertIn("FOLLOWED_BY", driver.calls[1]["cypher"])
        self.assertIn("nextExecutionEvent", driver.calls[1]["cypher"])
        self.assertEqual(driver.calls[0]["params"]["previous_sequence"], None)
        self.assertEqual(driver.calls[0]["params"]["next_sequence"], 2)
        self.assertEqual(driver.calls[1]["params"]["previous_sequence"], 1)
        self.assertEqual(driver.calls[1]["params"]["next_sequence"], 3)
        self.assertEqual(driver.calls[1]["params"]["sequence"], 2)
        self.assertEqual(driver.calls[1]["params"]["execution_id"], "run-1")

    async def test_workflow_definition_projects_agents_tasks_tools_and_dependencies(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="workflow.updated",
                aggregate_type="workflow",
                aggregate_id="workflow-1",
                tenant_id="tenant-1",
                user_id="user-1",
                payload={
                    "workflow_id": "workflow-1",
                    "name": "Graph Memory Workflow",
                    "description": "Builds graph context.",
                    "entrypoint": "task-1",
                    "revision": 2,
                    "version": "1.0.0",
                    "labels": ["graph-memory"],
                    "is_published": True,
                    "workspace_id": "workspace-1",
                    "default_runtime_adapter_id": "native",
                    "allowed_runtime_adapter_ids": ["native"],
                    "agents": [
                        {
                            "id": "agent-1",
                            "name": "researcher",
                            "display_name": "Researcher",
                            "role": "research",
                            "model_profile_id": "model-1",
                            "tool_ids": ["tool-1"],
                            "handoff_agent_ids": ["agent-2"],
                            "memory_enabled": True,
                            "memory_scope": "workflow",
                            "memory_strategy": "graph",
                        }
                    ],
                    "tasks": [
                        {
                            "id": "task-1",
                            "name": "Collect context",
                            "agent_id": "agent-1",
                            "tool_ids": ["tool-1"],
                            "depends_on_task_ids": ["task-0"],
                            "human_approval_required": False,
                            "has_input_schema": True,
                            "has_output_schema": False,
                        }
                    ],
                    "tools": [
                        {
                            "id": "tool-1",
                            "name": "graph_search",
                            "display_name": "Graph Search",
                            "tool_type": "python_function",
                            "requires_approval": False,
                            "sandbox_required": False,
                            "allow_shell": False,
                            "allow_browser": False,
                            "allow_filesystem": False,
                            "allow_network": False,
                            "read_only": False,
                            "read_only_sql": True,
                            "dangerous": False,
                            "has_input_schema": True,
                            "has_output_schema": False,
                        }
                    ],
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        cypher = driver.calls[0]["cypher"]
        self.assertIn("DEFINES_AGENT", cypher)
        self.assertIn("DEFINES_TASK", cypher)
        self.assertIn("DEFINES_TOOL", cypher)
        self.assertIn("ASSIGNED_TO", cypher)
        self.assertIn("USES_TOOL", cypher)
        self.assertIn("CAN_USE", cypher)
        self.assertIn("CAN_HANDOFF_TO", cypher)
        self.assertIn("USED_MODEL", cypher)
        self.assertIn("USES_MODEL_PROFILE", cypher)
        self.assertIn("DEPENDS_ON", cypher)
        self.assertIn("WorkflowVersion", cypher)
        self.assertIn("HAS_VERSION", cypher)
        self.assertIn("oldScoped", cypher)
        params = driver.calls[0]["params"]
        self.assertEqual(params["workflow_id"], "workflow-1")
        self.assertEqual(params["workflow_version_id"], "workflow-1:v2")
        self.assertEqual(params["created_by_user_id"], "user-1")
        self.assertEqual(params["workspace_id"], "workspace-1")
        self.assertEqual(params["tenant_id"], "tenant-1")
        self.assertEqual(params["agents"][0]["id"], "agent-1")
        self.assertEqual(params["tasks"][0]["depends_on_task_ids"], ["task-0"])
        self.assertEqual(params["tools"][0]["id"], "tool-1")

    async def test_workflow_deletion_marks_topology_relationships_deleted(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="workflow.deleted",
                aggregate_type="workflow",
                aggregate_id="workflow-1",
                payload={"workflow_id": "workflow-1"},
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        cypher = driver.calls[0]["cypher"]
        self.assertIn("DEFINES_AGENT", cypher)
        self.assertIn("DEFINES_TASK", cypher)
        self.assertIn("DEFINES_TOOL", cypher)
        self.assertIn("CAN_USE", cypher)
        self.assertIn("ASSIGNED_TO", cypher)
        self.assertIn("scopedRel.workflow_id = $workflow_id", cypher)
        self.assertIn("definitionRel.deleted = true", cypher)
        self.assertIn("scopedRel.deleted = true", cypher)
        self.assertEqual(driver.calls[0]["params"]["workflow_id"], "workflow-1")

    async def test_execution_deletion_marks_retained_run_graph_deleted(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="execution.deleted",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                payload={"execution_id": "run-1", "workflow_id": "workflow-1"},
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        cypher = driver.calls[0]["cypher"]
        self.assertIn("WorkflowRun", cypher)
        self.assertIn("HAS_STEP_RUN", cypher)
        self.assertIn("EMITTED_EVENT", cypher)
        self.assertIn("deleted_at", cypher)
        self.assertEqual(driver.calls[0]["params"]["execution_id"], "run-1")
        self.assertIn("HAS_RUN", driver.calls[0]["params"]["run_relationship_types"])

    async def test_memory_deletion_marks_connected_relationships_deleted(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="memory.deleted",
                aggregate_type="memory",
                aggregate_id="memory-1",
                payload={"memory_id": "memory-1"},
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        cypher = driver.calls[0]["cypher"]
        self.assertIn("OPTIONAL MATCH (m)-[outRel]->()", cypher)
        self.assertIn("OPTIONAL MATCH ()-[inRel]->(m)", cypher)
        self.assertIn("outRel.deleted = true", cypher)
        self.assertIn("inRel.deleted = true", cypher)

    async def test_document_deletion_marks_chunks_and_memory_relationships_deleted(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="document_memory_collection.deleted",
                aggregate_type="document_memory_collection",
                aggregate_id="doc-1",
                payload={"document_id": "doc-1", "memory_ids": ["memory-1"]},
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        cypher = driver.calls[0]["cypher"]
        self.assertIn("docOutRel.deleted = true", cypher)
        self.assertIn("docInRel.deleted = true", cypher)
        self.assertIn("memoryRel.deleted = true", cypher)
        self.assertEqual(driver.calls[0]["params"]["memory_ids"], ["memory-1"])

    async def test_model_request_event_projects_model_and_provider_lineage(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="llm.request.created",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="event-llm-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "model_request_id": "model-request-1",
                    "payload": {
                        "provider": "openai",
                        "model": "gpt-4.1",
                        "messages": "must not be projected",
                    },
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        cypher = driver.calls[0]["cypher"]
        self.assertIn("ModelRequest", cypher)
        self.assertIn("ModelProvider", cypher)
        self.assertIn("USED_MODEL", cypher)
        self.assertIn("USED_PROVIDER", cypher)
        params = driver.calls[0]["params"]
        self.assertEqual(params["model_request_id"], "model-request-1")
        self.assertEqual(params["model_provider"], "openai")
        self.assertEqual(params["model_name"], "gpt-4.1")
        self.assertEqual(params["model_id"], "openai:gpt-4.1")
        self.assertIn("messages", params["payload_keys"])
        self.assertNotIn("messages", cypher)

    async def test_observability_events_project_context_budget_usage_and_compaction(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        await repo.append(
            GraphProjectionEvent(
                event_type="context.health.recorded",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="event-context-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "agent_id": "agent-1",
                    "task_id": "task-1",
                    "model_request_id": "model-request-1",
                    "payload": {
                        "status": "critical",
                        "estimated_prompt_tokens": 9000,
                        "reserved_completion_tokens": 1000,
                        "estimated_total_context_tokens": 10000,
                        "context_window": 10000,
                        "usage_ratio": 1.0,
                    },
                    "metrics": {"context_status": "critical", "context_usage_ratio": 1.0},
                },
            )
        )
        await repo.append(
            GraphProjectionEvent(
                event_type="token.usage.recorded",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="event-usage-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "model_request_id": "model-request-1",
                    "payload": {
                        "usage": {
                            "provider": "openai",
                            "model": "gpt-4.1",
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                            "estimated_cost": 0.01,
                            "currency": "USD",
                        }
                    },
                },
            )
        )
        await repo.append(
            GraphProjectionEvent(
                event_type="token.budget.exceeded",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="event-budget-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "payload": {
                        "budget": {
                            "scope": "run",
                            "status": "exceeded",
                            "action": "compact_context",
                            "used_tokens": 1200,
                            "budget_tokens": 1000,
                            "usage_ratio": 1.2,
                        }
                    },
                },
            )
        )
        await repo.append(
            GraphProjectionEvent(
                event_type="context.compaction.completed",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="event-compaction-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "payload": {
                        "reason": "budget_exceeded",
                        "record": {
                            "compacted": True,
                            "memory_id": "memory-context-pack-1",
                            "source_model_request_id": "model-request-1",
                            "estimated_tokens_saved": 500,
                        },
                        "context_health_before": {"status": "critical", "usage_ratio": 1.2},
                        "context_health_after": {"status": "normal", "usage_ratio": 0.4},
                    },
                },
            )
        )
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        result = await projector.project_pending(repo, limit=10)

        self.assertEqual(result.processed, 4)
        self.assertEqual(result.failed, 0)
        calls = "\n".join(call["cypher"] for call in driver.calls)
        self.assertIn("ContextHealth", calls)
        self.assertIn("TokenUsage", calls)
        self.assertIn("TokenBudget", calls)
        self.assertIn("ContextCompaction", calls)
        self.assertIn("HAS_CONTEXT_HEALTH", calls)
        self.assertIn("RECORDED_USAGE", calls)
        self.assertIn("HAS_BUDGET_SIGNAL", calls)
        self.assertIn("HAS_COMPACTION", calls)
        self.assertIn("context_health_status", calls)
        self.assertIn("last_token_usage_total_tokens", calls)
        self.assertIn("last_estimated_cost", calls)
        self.assertIn("token_budget_status", calls)
        self.assertIn("context_compaction_status", calls)
        context_params = driver.calls[0]["params"]
        self.assertEqual(context_params["context_health_id"], "context_health:event-context-1")
        self.assertEqual(context_params["context_health_status"], "critical")
        usage_params = driver.calls[1]["params"]
        self.assertEqual(usage_params["token_usage_id"], "token_usage:event-usage-1")
        self.assertEqual(usage_params["token_usage_total_tokens"], 15)
        budget_params = driver.calls[2]["params"]
        self.assertEqual(budget_params["token_budget_id"], "token_budget:event-budget-1")
        self.assertEqual(budget_params["token_budget_action"], "compact_context")
        compaction_params = driver.calls[3]["params"]
        self.assertEqual(compaction_params["context_compaction_id"], "context_compaction:event-compaction-1")
        self.assertEqual(compaction_params["context_compaction_memory_id"], "memory-context-pack-1")

    async def test_clear_projection_deletes_projected_labels_only(self) -> None:
        driver = FakeNeo4jDriver()
        projector = Neo4jGraphProjector(driver)

        await projector.clear_projection(labels=["Workflow", "Memory"])

        self.assertIn("DETACH DELETE", driver.calls[0]["cypher"])
        self.assertEqual(driver.calls[0]["params"]["labels"], ["Workflow", "Memory"])

    async def test_rebuilder_dry_run_does_not_mutate_events_or_driver(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        event = await repo.append(
            GraphProjectionEvent(
                event_type="memory.created",
                aggregate_type="memory",
                aggregate_id="memory-rebuild",
                payload={"memory_id": "memory-rebuild"},
            )
        )
        driver = FakeNeo4jDriver()
        rebuilder = Neo4jGraphRebuilder(repo, Neo4jGraphProjector(driver))

        result = await rebuilder.rebuild(dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertEqual(result.reset_events, 1)
        self.assertEqual(driver.calls, [])
        self.assertEqual((await repo.list_events())[0].event_id, event.event_id)
        self.assertEqual((await repo.list_events())[0].status, "pending")

    async def test_rebuilder_clears_resets_and_replays_events(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        event = await repo.append(
            GraphProjectionEvent(
                event_type="memory.created",
                aggregate_type="memory",
                aggregate_id="memory-rebuild",
                payload={
                    "memory_id": "memory-rebuild",
                    "scope": "user",
                    "summary": "Rebuild memory.",
                    "sensitive": False,
                    "source": "manual",
                    "memory_type": "fact",
                    "status": "active",
                },
            )
        )
        await repo.mark_projected(event.event_id)
        driver = FakeNeo4jDriver()
        rebuilder = Neo4jGraphRebuilder(repo, Neo4jGraphProjector(driver), batch_size=1)

        result = await rebuilder.rebuild(clear=True)

        self.assertTrue(result.cleared)
        self.assertEqual(result.reset_events, 1)
        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        calls = "\n".join(call["cypher"] for call in driver.calls)
        self.assertIn("CREATE CONSTRAINT", calls)
        self.assertIn("DETACH DELETE", calls)
        self.assertIn("MERGE (m:Memory", calls)
        self.assertEqual(await repo.list_events(status="pending"), [])


@unittest.skipUnless(os.getenv("NEO4J_LIVE_TESTS") == "1", "Set NEO4J_LIVE_TESTS=1 to run live Neo4j smoke tests")
class Neo4jGraphProjectionLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_schema_and_projection_smoke(self) -> None:
        reset_settings_cache()
        settings = get_settings()
        driver = create_neo4j_driver(settings)
        projector = Neo4jGraphProjector(driver, config=Neo4jProjectionConfig(database=settings.neo4j_database))
        repo = InMemoryGraphProjectionEventRepository()
        try:
            await projector.ensure_schema()
            await repo.append(
                GraphProjectionEvent(
                    event_type="memory.created",
                    aggregate_type="memory",
                    aggregate_id="live-memory-1",
                    payload={
                        "memory_id": "live-memory-1",
                        "scope": "user",
                        "summary": "Live Neo4j smoke memory.",
                        "sensitive": False,
                        "source": "live-test",
                        "memory_type": "fact",
                        "status": "active",
                        "updated_at": "2026-05-24T00:00:00+00:00",
                    },
                )
            )
            result = await projector.project_pending(repo, limit=10)
            self.assertEqual(result.processed, 1)
            self.assertEqual(result.failed, 0)
        finally:
            await projector.close()
            reset_settings_cache()


if __name__ == "__main__":
    unittest.main()
