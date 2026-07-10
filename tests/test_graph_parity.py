from __future__ import annotations

import unittest

from app.db.repositories import InMemoryGraphProjectionEventRepository
from app.domain import GraphProjectionEvent
from app.graph.neo4j_projection import Neo4jProjectionConfig
from app.graph.parity import Neo4jGraphParityChecker


class FakeCountResult:
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


class FakeParitySession:
    def __init__(self, driver: "FakeParityDriver"):
        self.driver = driver

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, exc, _tb):
        return None

    async def run(self, cypher: str, **params):
        self.driver.calls.append({"cypher": cypher, "params": params})
        for key, value in self.driver.counts.items():
            if f"`{key}`" in cypher:
                return FakeCountResult(value)
        return FakeCountResult(0)


class FakeParityDriver:
    def __init__(self, counts: dict[str, int]):
        self.counts = counts
        self.calls: list[dict] = []
        self.session_kwargs: list[dict] = []
        self.closed = False

    def session(self, **kwargs):
        self.session_kwargs.append(kwargs)
        return FakeParitySession(self)

    async def close(self):
        self.closed = True


class GraphParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_parity_matches_projected_active_outbox_counts(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        workflow = await repo.append(
            GraphProjectionEvent(
                event_type="workflow.created",
                aggregate_type="workflow",
                aggregate_id="workflow-1",
                payload={"workflow_id": "workflow-1"},
            )
        )
        memory = await repo.append(
            GraphProjectionEvent(
                event_type="memory.created",
                aggregate_type="memory",
                aggregate_id="memory-1",
                payload={
                    "memory_id": "memory-1",
                    "memory_type": "context_pack",
                    "metadata": {
                        "decisions": [{"id": "decision-1", "summary": "Use graph projection."}],
                        "next_actions": [{"id": "next-1", "summary": "Backfill context packs."}],
                    },
                },
            )
        )
        link = await repo.append(
            GraphProjectionEvent(
                event_type="workflow_memory_link.created",
                aggregate_type="workflow_memory_link",
                aggregate_id="link-1",
                payload={"workflow_id": "workflow-1"},
            )
        )
        document = await repo.append(
            GraphProjectionEvent(
                event_type="document_memory_collection.created",
                aggregate_type="document_memory_collection",
                aggregate_id="doc-1",
                payload={"document_id": "doc-1", "memory_ids": ["memory-1"]},
            )
        )
        await repo.mark_projected(workflow.event_id)
        await repo.mark_projected(memory.event_id)
        await repo.mark_projected(link.event_id)
        await repo.mark_projected(document.event_id)
        driver = FakeParityDriver(
            {
                "Workflow": 1,
                "Memory": 1,
                "ContextPack": 1,
                "Decision": 1,
                "NextAction": 1,
                "Document": 1,
                "DocumentChunk": 1,
                "HAS_CONTEXT_PACK": 1,
                "LINKS_MEMORY": 1,
                "MENTIONS": 2,
                "SUMMARIZES": 2,
                "HAS_CHUNK": 1,
                "PART_OF_DOCUMENT": 2,
                "SOURCE_DOCUMENT": 1,
            }
        )
        checker = Neo4jGraphParityChecker(driver, config=Neo4jProjectionConfig(database="neo4j"))

        result = await checker.check(repo)

        self.assertTrue(result.ok)
        self.assertEqual(result.checked_events, 4)
        self.assertEqual(driver.session_kwargs[0], {"database": "neo4j"})
        self.assertEqual(result.node_counts_by_type["Workflow"], 1)
        self.assertEqual(result.edge_counts_by_type["LINKS_MEMORY"], 1)
        items = {(item.kind, item.name): item.to_dict() for item in result.items}
        self.assertEqual(items[("node", "Workflow")]["delta"], 0)
        self.assertEqual(items[("node", "Memory")]["delta"], 0)
        self.assertEqual(items[("node", "ContextPack")]["delta"], 0)
        self.assertEqual(items[("node", "DocumentChunk")]["delta"], 0)
        self.assertEqual(items[("relationship", "HAS_CONTEXT_PACK")]["delta"], 0)
        self.assertEqual(items[("relationship", "LINKS_MEMORY")]["delta"], 0)
        self.assertEqual(items[("relationship", "SUMMARIZES")]["delta"], 0)
        self.assertEqual(items[("relationship", "SOURCE_DOCUMENT")]["delta"], 0)

    async def test_parity_reports_mismatch_and_pending_events(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        projected = await repo.append(
            GraphProjectionEvent(
                event_type="memory.created",
                aggregate_type="memory",
                aggregate_id="memory-1",
                payload={"memory_id": "memory-1"},
            )
        )
        await repo.mark_projected(projected.event_id)
        await repo.append(
            GraphProjectionEvent(
                event_type="memory.created",
                aggregate_type="memory",
                aggregate_id="memory-2",
                payload={"memory_id": "memory-2"},
            )
        )
        driver = FakeParityDriver({"Memory": 2})
        checker = Neo4jGraphParityChecker(driver)

        result = await checker.check(repo)

        self.assertFalse(result.ok)
        memory_item = next(item for item in result.items if item.kind == "node" and item.name == "Memory")
        self.assertEqual(memory_item.expected, 1)
        self.assertEqual(memory_item.actual, 2)
        self.assertIn("Outbox has pending projection events.", result.errors)

    async def test_parity_counts_approved_source_intelligence_graph_hints(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        event = await repo.append(
            GraphProjectionEvent(
                event_type="memory.source_intelligence.graph_hints.approved",
                aggregate_type="memory",
                aggregate_id="memory-source-intelligence",
                payload={
                    "memory_id": "memory-source-intelligence",
                    "document_id": "doc-release",
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
        await repo.mark_projected(event.event_id)
        driver = FakeParityDriver(
            {
                "Memory": 1,
                "Document": 1,
                "Entity": 3,
                "Workflow": 1,
                "Artifact": 1,
                "Person": 1,
                "MENTIONS": 6,
                "SOURCE_DOCUMENT": 1,
                "PRODUCES": 1,
                "REVIEWS": 1,
            }
        )
        checker = Neo4jGraphParityChecker(driver)

        result = await checker.check(repo)

        self.assertTrue(result.ok)
        items = {(item.kind, item.name): item.to_dict() for item in result.items}
        self.assertEqual(items[("node", "Entity")]["expected"], 3)
        self.assertEqual(items[("node", "Workflow")]["expected"], 1)
        self.assertEqual(items[("node", "Person")]["expected"], 1)
        self.assertEqual(items[("relationship", "PRODUCES")]["expected"], 1)
        self.assertEqual(items[("relationship", "REVIEWS")]["expected"], 1)
        self.assertEqual(items[("relationship", "MENTIONS")]["expected"], 6)

    async def test_parity_counts_persona_factory_projection(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        event = await repo.append(
            GraphProjectionEvent(
                event_type="persona.factory.item.approved",
                aggregate_type="persona",
                aggregate_id="persona-1",
                payload={
                    "persona_id": "persona-1",
                    "run_id": "run-1",
                    "item_id": "item-1",
                    "source_memory_id": "memory-source-1",
                    "tools": [{"id": "jira", "name": "Jira"}],
                    "workflows": [{"id": "workflow-audit-review", "name": "Audit Review"}],
                    "artifacts": [{"id": "artifact-mlp", "name": "MLP Observation"}],
                },
            )
        )
        await repo.mark_projected(event.event_id)
        driver = FakeParityDriver(
            {
                "Persona": 1,
                "DistillationRun": 1,
                "DistillationItem": 1,
                "SourceMemory": 1,
                "Tool": 1,
                "Workflow": 1,
                "Artifact": 1,
                "PERSONA_HAS_DISTILLATION_RUN": 1,
                "RUN_EXTRACTED_ITEM": 1,
                "ITEM_DERIVED_FROM_MEMORY": 1,
                "PERSONA_USES_TOOL": 1,
                "PERSONA_FOLLOWS_WORKFLOW": 1,
                "PERSONA_PRODUCES_ARTIFACT": 1,
            }
        )
        checker = Neo4jGraphParityChecker(driver)

        result = await checker.check(repo)

        self.assertTrue(result.ok)
        items = {(item.kind, item.name): item.to_dict() for item in result.items}
        self.assertEqual(items[("node", "Persona")]["expected"], 1)
        self.assertEqual(items[("node", "DistillationItem")]["expected"], 1)
        self.assertEqual(items[("relationship", "RUN_EXTRACTED_ITEM")]["expected"], 1)
        self.assertEqual(items[("relationship", "ITEM_DERIVED_FROM_MEMORY")]["expected"], 1)

    async def test_parity_uses_projection_container_source_id_rules(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        event = await repo.append(
            GraphProjectionEvent(
                event_type="execution.started",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "payload": {"containerName": "agency-run-1"},
                },
            )
        )
        await repo.mark_projected(event.event_id)
        driver = FakeParityDriver(
            {
                "Workflow": 1,
                "WorkflowRun": 1,
                "RuntimeContainer": 1,
                "HAS_RUN": 1,
                "STARTED": 1,
                "CREATED_CONTAINER": 1,
            }
        )
        checker = Neo4jGraphParityChecker(driver)

        result = await checker.check(repo)

        self.assertTrue(result.ok)
        items = {(item.kind, item.name): item.to_dict() for item in result.items}
        self.assertEqual(items[("node", "RuntimeContainer")]["expected"], 1)
        self.assertEqual(items[("relationship", "CREATED_CONTAINER")]["expected"], 1)

    async def test_parity_counts_container_metadata_on_execution_detail_events(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        event = await repo.append(
            GraphProjectionEvent(
                event_type="monitor.finding.created",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="finding-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "sequence": 10,
                    "container_id": "container-1",
                    "container_name": "agency-run-1",
                    "payload": {"category": "failed_execution"},
                },
            )
        )
        await repo.mark_projected(event.event_id)
        driver = FakeParityDriver(
            {
                "WorkflowRun": 1,
                "ExecutionEvent": 1,
                "MonitorFinding": 1,
                "RuntimeContainer": 1,
                "EMITTED_EVENT": 1,
                "CREATED_CONTAINER": 1,
                "RAISED_FINDING": 2,
            }
        )
        checker = Neo4jGraphParityChecker(driver)

        result = await checker.check(repo)

        self.assertTrue(result.ok)
        items = {(item.kind, item.name): item.to_dict() for item in result.items}
        self.assertEqual(items[("node", "RuntimeContainer")]["expected"], 1)
        self.assertEqual(items[("relationship", "CREATED_CONTAINER")]["expected"], 1)

    async def test_parity_deactivates_workflow_scoped_relationships_on_workflow_delete(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        workflow = await repo.append(
            GraphProjectionEvent(
                event_type="workflow.created",
                aggregate_type="workflow",
                aggregate_id="workflow-1",
                payload={
                    "workflow_id": "workflow-1",
                    "revision": 1,
                    "agents": [{"id": "agent-1", "tool_ids": ["tool-1"]}],
                    "tasks": [{"id": "task-1", "agent_id": "agent-1"}],
                    "tools": [{"id": "tool-1"}],
                },
            )
        )
        execution = await repo.append(
            GraphProjectionEvent(
                event_type="execution.started",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                payload={"execution_id": "run-1", "workflow_id": "workflow-1"},
            )
        )
        deleted = await repo.append(
            GraphProjectionEvent(
                event_type="workflow.deleted",
                aggregate_type="workflow",
                aggregate_id="workflow-1",
                payload={"workflow_id": "workflow-1"},
            )
        )
        for event in (workflow, execution, deleted):
            await repo.mark_projected(event.event_id)
        driver = FakeParityDriver({"WorkflowRun": 1, "WorkflowVersion": 1, "Agent": 1, "Task": 1, "Tool": 1, "HAS_VERSION": 1})
        checker = Neo4jGraphParityChecker(driver)

        result = await checker.check(repo)

        self.assertTrue(result.ok)
        items = {(item.kind, item.name): item.to_dict() for item in result.items}
        self.assertEqual(items[("node", "Workflow")]["expected"], 0)
        self.assertEqual(items[("relationship", "HAS_RUN")]["expected"], 0)
        self.assertEqual(items[("relationship", "STARTED")]["expected"], 0)
        self.assertEqual(items[("relationship", "DEFINES_AGENT")]["expected"], 0)
        self.assertEqual(items[("relationship", "HAS_VERSION")]["expected"], 1)

    async def test_parity_counts_workflow_topology_model_and_observability_projection(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        workflow = await repo.append(
            GraphProjectionEvent(
                event_type="workflow.updated",
                aggregate_type="workflow",
                aggregate_id="workflow-1",
                payload={
                    "workflow_id": "workflow-1",
                    "revision": 2,
                    "version": "1.0.0",
                    "agents": [
                        {
                            "id": "agent-1",
                            "tool_ids": ["tool-1"],
                            "handoff_agent_ids": ["agent-2"],
                            "model_profile_id": "model-profile-1",
                        }
                    ],
                    "tasks": [
                        {
                            "id": "task-1",
                            "agent_id": "agent-1",
                            "tool_ids": ["tool-1"],
                            "depends_on_task_ids": ["task-0"],
                        }
                    ],
                    "tools": [{"id": "tool-1"}],
                },
            )
        )
        model_request = await repo.append(
            GraphProjectionEvent(
                event_type="llm.request.created",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="event-llm-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "sequence": 1,
                    "model_request_id": "model-request-1",
                    "payload": {"provider": "openai", "model": "gpt-4.1"},
                },
            )
        )
        budget = await repo.append(
            GraphProjectionEvent(
                event_type="token.budget.exceeded",
                aggregate_type="workflow_run",
                aggregate_id="run-1",
                source_event_id="event-budget-1",
                payload={
                    "execution_id": "run-1",
                    "workflow_id": "workflow-1",
                    "sequence": 2,
                    "payload": {"budget": {"scope": "run", "status": "exceeded"}},
                },
            )
        )
        for event in (workflow, model_request, budget):
            await repo.mark_projected(event.event_id)
        driver = FakeParityDriver(
            {
                "Workflow": 1,
                "Agent": 2,
                "Task": 2,
                "Tool": 1,
                "Model": 2,
                "WorkflowVersion": 1,
                "WorkflowRun": 1,
                "ExecutionEvent": 2,
                "ModelRequest": 1,
                "ModelProvider": 1,
                "TokenBudget": 1,
                "DEFINES_AGENT": 1,
                "DEFINES_TASK": 1,
                "DEFINES_TOOL": 1,
                "ASSIGNED_TO": 1,
                "CAN_USE": 1,
                "CAN_HANDOFF_TO": 1,
                "DEPENDS_ON": 1,
                "USES_TOOL": 1,
                "USED_MODEL": 3,
                "USES_MODEL_PROFILE": 1,
                "USED_PROVIDER": 1,
                "EMITTED_EVENT": 2,
                "FOLLOWED_BY": 1,
                "HAS_VERSION": 1,
                "OCCURRED_IN": 1,
                "HAS_BUDGET_SIGNAL": 2,
            }
        )
        checker = Neo4jGraphParityChecker(driver)

        result = await checker.check(repo)

        self.assertTrue(result.ok)
        items = {(item.kind, item.name): item.to_dict() for item in result.items}
        self.assertEqual(items[("node", "TokenBudget")]["expected"], 1)
        self.assertEqual(items[("node", "ModelProvider")]["expected"], 1)
        self.assertEqual(items[("node", "WorkflowVersion")]["expected"], 1)
        self.assertEqual(items[("relationship", "HAS_BUDGET_SIGNAL")]["expected"], 2)
        self.assertEqual(items[("relationship", "HAS_VERSION")]["expected"], 1)
        self.assertEqual(items[("relationship", "USED_MODEL")]["expected"], 3)
        self.assertEqual(items[("relationship", "FOLLOWED_BY")]["expected"], 1)

    async def test_parity_counts_step_run_aggregate_detail_events_by_event_type(self) -> None:
        repo = InMemoryGraphProjectionEventRepository()
        event = await repo.append(
            GraphProjectionEvent(
                event_type="context.health.recorded",
                aggregate_type="step_run",
                aggregate_id="step-run-1",
                source_event_id="event-context-health-1",
                payload={
                    "execution_id": "run-1",
                    "task_id": "task-1",
                    "agent_id": "agent-1",
                    "sequence": 4,
                    "payload": {"status": "ok"},
                },
            )
        )
        await repo.mark_projected(event.event_id)
        driver = FakeParityDriver(
            {
                "WorkflowRun": 1,
                "ExecutionEvent": 1,
                "ContextHealth": 1,
                "Task": 1,
                "Agent": 1,
                "EMITTED_EVENT": 2,
                "OCCURRED_IN": 1,
                "PARTICIPATED_IN": 1,
                "HAS_CONTEXT_HEALTH": 1,
                "RECORDED_CONTEXT_HEALTH": 1,
            }
        )
        checker = Neo4jGraphParityChecker(driver)

        result = await checker.check(repo)

        self.assertTrue(result.ok)
        items = {(item.kind, item.name): item.to_dict() for item in result.items}
        self.assertEqual(items[("node", "ContextHealth")]["expected"], 1)
        self.assertEqual(items[("node", "StepRun")]["expected"], 0)
        self.assertEqual(items[("relationship", "RECORDED_CONTEXT_HEALTH")]["expected"], 1)


if __name__ == "__main__":
    unittest.main()
