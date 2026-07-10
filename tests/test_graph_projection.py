from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import reset_settings_cache
from app.db.models import Base
from app.db.repositories import SQLGraphProjectionEventRepository
from app.db.repositories.domain_sql import SQLWorkflowRepository
from app.db.session import get_async_engine, get_session_maker, reset_session_state
from app.domain import (
    AgentDefinition,
    Execution,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    GraphProjectionEvent,
    MemorySettings,
    TaskDefinition,
    ToolDefinition,
    ToolImplementationReference,
    ToolType,
    WorkflowDefinition,
)
from app.graph.projection import GraphProjectionWorker
from app.runtime.native.state import SQLExecutionStore


class FailingProjectionRepository:
    async def append(self, event: GraphProjectionEvent) -> GraphProjectionEvent:
        raise RuntimeError("projection unavailable")


class GraphProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "graph_projection.db"
        self.env_patch = patch.dict(
            os.environ,
            {
                "APP_ENV": "test",
                "DATABASE_URL": f"sqlite+aiosqlite:///{self.db_path}",
            },
            clear=False,
        )
        self.env_patch.start()
        reset_settings_cache()
        reset_session_state()

    async def asyncSetUp(self) -> None:
        engine = get_async_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = get_session_maker()

    async def asyncTearDown(self) -> None:
        engine = get_async_engine(optional=True)
        if engine is not None:
            await engine.dispose()
        reset_session_state()
        reset_settings_cache()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    async def test_repository_appends_lists_and_replays_projection_events(self) -> None:
        repo = SQLGraphProjectionEventRepository(self.session_factory)
        event = await repo.append(
            GraphProjectionEvent(
                event_type="workflow.created",
                aggregate_type="workflow",
                aggregate_id="workflow-1",
                user_id="user-1",
                payload={"workflow_id": "workflow-1"},
            )
        )

        pending = await repo.list_events(status="pending")
        self.assertEqual([item.event_id for item in pending], [event.event_id])

        worker = GraphProjectionWorker(repo)
        result = await worker.run_once()
        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.checkpoint_event_id, event.event_id)
        self.assertEqual(await repo.list_events(status="pending"), [])

        replay_result = await worker.replay(run=True)
        self.assertEqual(replay_result.processed, 1)

    async def test_projection_status_summary_includes_health_metadata(self) -> None:
        repo = SQLGraphProjectionEventRepository(self.session_factory)
        execution_event = await repo.append(
            GraphProjectionEvent(
                event_type="execution.started",
                aggregate_type="workflow_run",
                aggregate_id="execution-1",
                payload={"execution_id": "execution-1", "workflow_id": "workflow-1"},
            )
        )
        memory_event = await repo.append(
            GraphProjectionEvent(
                event_type="memory.created",
                aggregate_type="memory",
                aggregate_id="memory-1",
                payload={"memory_id": "memory-1"},
            )
        )
        failed_event = await repo.append(
            GraphProjectionEvent(
                event_type="tool.call.failed",
                aggregate_type="workflow_run",
                aggregate_id="execution-2",
                payload={"execution_id": "execution-2"},
            )
        )
        await repo.mark_projected(execution_event.event_id)
        await repo.mark_projected(memory_event.event_id)
        await repo.mark_failed(failed_event.event_id, "projection failed")

        summary = await repo.status_summary()

        self.assertEqual(summary["health_status"], "degraded")
        self.assertEqual(summary["total_count"], 3)
        self.assertEqual(summary["projected_count"], 2)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["last_error"], "projection failed")
        self.assertIsNotNone(summary["latest_event_at"])
        self.assertIsNotNone(summary["last_projected_event_at"])
        self.assertIsNotNone(summary["last_projected_execution_event_at"])
        self.assertIsNotNone(summary["last_projected_memory_event_at"])
        self.assertIsNotNone(summary["projection_lag_seconds"])

    async def test_workflow_repository_projects_definition_topology_without_prompt_content(self) -> None:
        projection_repo = SQLGraphProjectionEventRepository(self.session_factory)
        workflow_repo = SQLWorkflowRepository(self.session_factory, graph_projection_event_repo=projection_repo)

        await workflow_repo.create(
            WorkflowDefinition(
                id="workflow-1",
                name="Graph Memory Workflow",
                description="Builds graph context.",
                entrypoint="task-1",
                agent_definitions=[
                    AgentDefinition(
                        id="agent-1",
                        name="researcher",
                        description="Research agent",
                        instructions="private agent prompt",
                        backstory="private backstory",
                        role="research",
                        model_profile_id="model-profile-1",
                        tool_ids=["tool-1"],
                        handoff_agent_ids=["agent-2"],
                        memory=MemorySettings(enabled=True, scope="workflow", strategy="graph"),
                    ),
                    AgentDefinition(id="agent-2", name="reviewer"),
                ],
                task_definitions=[
                    TaskDefinition(
                        id="task-1",
                        name="Collect context",
                        description="Collect relevant graph context.",
                        instructions="private task instructions",
                        expected_output="private expected output",
                        agent_id="agent-1",
                        tool_ids=["tool-1"],
                    ),
                    TaskDefinition(
                        id="task-2",
                        name="Review context",
                        description="Review graph context.",
                        agent_id="agent-2",
                        depends_on_task_ids=["task-1"],
                        human_approval_required=True,
                    ),
                ],
                tool_definitions=[
                    ToolDefinition(
                        id="tool-1",
                        name="graph_search",
                        description="Query graph context.",
                        tool_type=ToolType.PYTHON_FUNCTION,
                        input_schema={"type": "object"},
                        implementation=ToolImplementationReference(
                            implementation_type="python",
                            target="app.tools.graph",
                            config={"private": "not projected"},
                        ),
                    )
                ],
            )
        )

        events = await projection_repo.list_events(status="pending")
        self.assertEqual(len(events), 1)
        payload = events[0].payload
        self.assertEqual(payload["workflow_id"], "workflow-1")
        self.assertEqual(payload["entrypoint"], "task-1")
        self.assertEqual(payload["agents"][0]["id"], "agent-1")
        self.assertEqual(payload["agents"][0]["tool_ids"], ["tool-1"])
        self.assertEqual(payload["agents"][0]["handoff_agent_ids"], ["agent-2"])
        self.assertTrue(payload["agents"][0]["memory_enabled"])
        self.assertEqual(payload["tasks"][1]["depends_on_task_ids"], ["task-1"])
        self.assertTrue(payload["tasks"][1]["human_approval_required"])
        self.assertEqual(payload["tools"][0]["id"], "tool-1")
        self.assertNotIn("instructions", payload["agents"][0])
        self.assertNotIn("backstory", payload["agents"][0])
        self.assertNotIn("instructions", payload["tasks"][0])
        self.assertNotIn("expected_output", payload["tasks"][0])
        self.assertNotIn("implementation", payload["tools"][0])

    async def test_sql_execution_store_projects_selected_execution_events(self) -> None:
        projection_repo = SQLGraphProjectionEventRepository(self.session_factory)
        store = SQLExecutionStore(self.session_factory, graph_projection_event_repo=projection_repo)
        await store.save_execution(
            Execution(
                id="execution-1",
                workflow_id="workflow-1",
                runtime_adapter="native",
                runtime_revision_id="revision-1",
                runtime_fingerprint="fingerprint-1",
                status=ExecutionStatus.RUNNING,
                trigger_type="schedule",
                trigger_payload={"schedule_id": "schedule-1"},
                input_json={},
                container_id="container-1",
                container_name="agency-execution-1",
            )
        )

        saved = await store.save_event(
            ExecutionEvent(
                execution_id="execution-1",
                workflow_id="workflow-1",
                event_type=ExecutionEventType.EXECUTION_STARTED,
                payload_json={"reason": "test"},
            )
        )

        events = await projection_repo.list_events(status="pending")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source, "execution_events")
        self.assertEqual(events[0].source_event_id, saved.id)
        self.assertEqual(events[0].event_type, "execution.started")
        self.assertEqual(events[0].aggregate_type, "workflow_run")
        self.assertEqual(events[0].aggregate_id, "execution-1")
        self.assertEqual(events[0].payload["runtime_revision_id"], "revision-1")
        self.assertEqual(events[0].payload["runtime_fingerprint"], "fingerprint-1")
        self.assertEqual(events[0].payload["trigger_payload"], {"schedule_id": "schedule-1"})
        self.assertEqual(events[0].payload["container_id"], "container-1")
        self.assertEqual(events[0].payload["container_name"], "agency-execution-1")

    async def test_sql_execution_store_projects_execution_deletion_for_retention_cleanup(self) -> None:
        projection_repo = SQLGraphProjectionEventRepository(self.session_factory)
        store = SQLExecutionStore(self.session_factory, graph_projection_event_repo=projection_repo)
        await store.save_execution(
            Execution(
                id="execution-delete-1",
                workflow_id="workflow-1",
                runtime_adapter="native",
                runtime_revision_id="revision-1",
                runtime_fingerprint="fingerprint-1",
                status=ExecutionStatus.COMPLETED,
                input_json={},
                container_id="container-1",
                container_name="agency-execution-delete-1",
            )
        )

        deleted = await store.delete_execution("execution-delete-1")

        self.assertTrue(deleted)
        self.assertIsNone(await store.get_execution("execution-delete-1"))
        events = await projection_repo.list_events(status="pending")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "execution.deleted")
        self.assertEqual(events[0].aggregate_type, "workflow_run")
        self.assertEqual(events[0].aggregate_id, "execution-delete-1")
        self.assertEqual(events[0].source, "execution_retention")
        self.assertEqual(events[0].source_event_id, "execution:execution-delete-1:deleted")
        self.assertEqual(events[0].payload["execution_id"], "execution-delete-1")
        self.assertEqual(events[0].payload["workflow_id"], "workflow-1")
        self.assertEqual(events[0].payload["status"], "deleted")
        self.assertEqual(events[0].payload["runtime_revision_id"], "revision-1")
        self.assertEqual(events[0].payload["container_name"], "agency-execution-delete-1")

    async def test_sql_execution_store_projects_observability_execution_events(self) -> None:
        projection_repo = SQLGraphProjectionEventRepository(self.session_factory)
        store = SQLExecutionStore(self.session_factory, graph_projection_event_repo=projection_repo)
        await store.save_execution(
            Execution(
                id="execution-observability-1",
                workflow_id="workflow-1",
                runtime_adapter="native",
                status=ExecutionStatus.RUNNING,
                input_json={},
            )
        )

        saved = await store.save_event(
            ExecutionEvent(
                execution_id="execution-observability-1",
                workflow_id="workflow-1",
                event_type=ExecutionEventType.TOKEN_BUDGET_WARNING,
                agent_id="agent-1",
                task_id="task-1",
                model_request_id="model-request-1",
                payload_json={
                    "budget": {
                        "scope": "run",
                        "used_tokens": 800,
                        "budget_tokens": 1000,
                        "usage_ratio": 0.8,
                        "status": "warning",
                        "action": "warn_only",
                    }
                },
                metrics={"used_tokens": 800, "budget_tokens": 1000, "usage_ratio": 0.8},
            )
        )

        events = await projection_repo.list_events(status="pending")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source_event_id, saved.id)
        self.assertEqual(events[0].event_type, "token.budget.warning")
        self.assertEqual(events[0].aggregate_type, "step_run")
        self.assertEqual(events[0].aggregate_id, "execution-observability-1:task-1")
        self.assertEqual(events[0].payload["agent_id"], "agent-1")
        self.assertEqual(events[0].payload["task_id"], "task-1")
        self.assertEqual(events[0].payload["model_request_id"], "model-request-1")
        self.assertEqual(events[0].payload["payload"]["budget"]["usage_ratio"], 0.8)
        self.assertEqual(events[0].payload["metrics"]["used_tokens"], 800)

    async def test_execution_event_persistence_survives_projection_append_failure(self) -> None:
        store = SQLExecutionStore(self.session_factory, graph_projection_event_repo=FailingProjectionRepository())
        await store.save_execution(
            Execution(
                id="execution-2",
                workflow_id="workflow-1",
                runtime_adapter="native",
                status=ExecutionStatus.RUNNING,
                input_json={},
            )
        )

        saved = await store.save_event(
            ExecutionEvent(
                execution_id="execution-2",
                workflow_id="workflow-1",
                event_type=ExecutionEventType.EXECUTION_STARTED,
            )
        )

        events = await store.list_events("execution-2")
        self.assertEqual([event.id for event in events], [saved.id])
