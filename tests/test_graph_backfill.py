from __future__ import annotations

import unittest

from app.api.context import create_test_api_context
from app.domain import (
    AgentDefinition,
    Execution,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    MemoryRecord,
    MemoryScope,
    MemoryType,
    TaskDefinition,
    WorkflowDefinition,
)
from app.graph.backfill import GraphProjectionBackfillService


class GraphProjectionBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_backfill_enqueues_workflows_executions_memories_documents_and_links(self) -> None:
        context = create_test_api_context()
        await context.workflow_repo.create(
            WorkflowDefinition(
                id="workflow-1",
                name="Backfill Workflow",
                entrypoint="task-1",
                metadata={
                    "memory_links": [
                        {
                            "id": "link-1",
                            "target_type": "workflow",
                            "ref_type": "memory",
                            "ref_id": "memory-1",
                            "memory_ids": ["memory-1"],
                            "access_mode": "read",
                        }
                    ]
                },
                agent_definitions=[AgentDefinition(id="agent-1", name="agent", model_profile_id="model-profile-1")],
                task_definitions=[
                    TaskDefinition(
                        id="task-1",
                        name="Task",
                        description="Task description",
                        agent_id="agent-1",
                    )
                ],
            )
        )
        await context.execution_store.save_execution(
            Execution(
                id="execution-1",
                workflow_id="workflow-1",
                runtime_adapter="native",
                status=ExecutionStatus.RUNNING,
                input_json={},
            )
        )
        await context.execution_store.save_event(
            ExecutionEvent(
                id="event-1",
                execution_id="execution-1",
                workflow_id="workflow-1",
                event_type=ExecutionEventType.LLM_REQUEST_CREATED,
                model_request_id="model-request-1",
                payload_json={"provider": "openai", "model": "gpt-4.1"},
            )
        )
        await context.memory_repo.create(
            MemoryRecord(
                id="memory-1",
                scope=MemoryScope.WORKFLOW,
                workflow_id="workflow-1",
                content="Document chunk about Graph Backfill.",
                summary="Graph backfill chunk",
                source="document_upload",
                memory_type=MemoryType.ARCHIVE,
                metadata={
                    "document_id": "document-1",
                    "filename": "graph.md",
                    "content_type": "text/markdown",
                    "chunk_index": 0,
                    "chunk_count": 1,
                    "entity_hints": [{"name": "Graph Backfill", "type": "concept", "confidence": 0.9}],
                },
            )
        )

        result = await GraphProjectionBackfillService(context).backfill()

        self.assertEqual(result.errors, [])
        self.assertGreaterEqual(result.enqueued, 6)
        events = await context.graph_projection_event_repo.list_events(limit=50)
        event_types = {event.event_type for event in events}
        self.assertIn("workflow.updated", event_types)
        self.assertIn("workflow_memory_link.updated", event_types)
        self.assertIn("llm.request.created", event_types)
        self.assertIn("memory.updated", event_types)
        self.assertIn("memory.entities.extracted", event_types)
        self.assertIn("document_memory_collection.created", event_types)
        workflow_event = next(event for event in events if event.event_type == "workflow.updated")
        self.assertEqual(workflow_event.source, "graph_backfill_workflows")
        self.assertEqual(workflow_event.payload["agents"][0]["model_profile_id"], "model-profile-1")
        self.assertNotIn("instructions", workflow_event.payload["agents"][0])
        memory_event = next(event for event in events if event.event_type == "memory.updated")
        self.assertNotIn("content", memory_event.payload)
        document_event = next(event for event in events if event.event_type == "document_memory_collection.created")
        self.assertEqual(document_event.payload["memory_ids"], ["memory-1"])

    async def test_backfill_is_idempotent_for_deterministic_source_events(self) -> None:
        context = create_test_api_context()
        await context.workflow_repo.create(
            WorkflowDefinition(id="workflow-1", name="Backfill Workflow", entrypoint="task-1")
        )
        service = GraphProjectionBackfillService(context)

        first = await service.backfill(domains=["workflows"])
        second = await service.backfill(domains=["workflows"])

        self.assertEqual(first.enqueued, 1)
        self.assertEqual(second.enqueued, 1)
        events = await context.graph_projection_event_repo.list_events(limit=20)
        self.assertEqual(len([event for event in events if event.event_type == "workflow.updated"]), 1)

    async def test_backfill_enqueues_approved_source_intelligence_graph_hints_idempotently(self) -> None:
        context = create_test_api_context()
        await context.memory_repo.create(
            MemoryRecord(
                id="memory-source-intelligence-backfill",
                scope=MemoryScope.USER,
                created_by_user_id="user-1",
                content="Release workflow produces an approval record.",
                summary="Release approval workflow",
                memory_type=MemoryType.ARCHIVE,
                metadata={
                    "document_id": "doc-release",
                    "filename": "release-sop.md",
                    "chunk_index": 0,
                    "source_intelligence": {
                        "review_status": "approved",
                        "classification": {"label": "workflow"},
                    },
                    "graph_hints": {
                        "review_status": "approved",
                        "entities": [
                            {"label": "Workflow", "name": "Release Workflow", "confidence": 0.9},
                            {"label": "Artifact", "name": "Approval Record", "confidence": 0.85},
                        ],
                        "relationships": [
                            {
                                "source_name": "Release Workflow",
                                "relationship_type": "PRODUCES",
                                "target_name": "Approval Record",
                                "confidence": 0.82,
                            }
                        ],
                    },
                },
            )
        )
        service = GraphProjectionBackfillService(context)

        first = await service.backfill(domains=["graph-hints"])
        second = await service.backfill(domains=["graph-hints"])

        self.assertEqual(first.domains["source_intelligence_graph_hints"]["enqueued"], 1)
        self.assertEqual(second.domains["source_intelligence_graph_hints"]["enqueued"], 1)
        events = await context.graph_projection_event_repo.list_events(limit=20)
        graph_hint_events = [
            event
            for event in events
            if event.event_type == "memory.source_intelligence.graph_hints.approved"
        ]
        self.assertEqual(len(graph_hint_events), 1)
        self.assertEqual(graph_hint_events[0].source, "source_intelligence")
        self.assertTrue(graph_hint_events[0].source_event_id.startswith("memory-graph-hints:"))
        self.assertEqual(graph_hint_events[0].payload["document_id"], "doc-release")
        self.assertEqual(graph_hint_events[0].payload["relationships"][0]["relationship_type"], "PRODUCES")


if __name__ == "__main__":
    unittest.main()
