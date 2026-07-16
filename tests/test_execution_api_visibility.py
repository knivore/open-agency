from __future__ import annotations

import asyncio
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.routes.executions import create_executions_router
from app.core.time import utc_now
from app.domain import (
    Execution,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    RuntimeRevision,
    RuntimeRevisionStatus,
    TaskDefinition,
    UserDefinition,
    WorkflowDefinition,
    WorkflowEdgeDefinition,
    WorkflowNodeDefinition,
)
from app.runtime.containers import RuntimeContainerState
from app.runtime.reconcile import ReconciliationAction, ReconciliationReport


class ExecutionApiVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.context = create_test_api_context()
        app = FastAPI()
        app.include_router(create_executions_router(cls.context))
        cls.client = TestClient(app)
        cls.client.headers.update(
            {
                "x-agency-user-id": "user-executions",
                "x-agency-user-email": "executions@example.com",
            }
        )
        asyncio.run(
            cls.context.user_repo.create(
                UserDefinition(
                    id="user-executions",
                    email="executions@example.com",
                    display_name="Executions User",
                    roles=["admin"],
                )
            )
        )

        revision = RuntimeRevision(
            id="runtime-rev-1",
            fingerprint="fp-1",
            build_status=RuntimeRevisionStatus.READY,
            image_name="agency-runtime",
            image_tag="rev-1",
        )
        asyncio.run(cls.context.runtime_revision_repo.create(revision))

        old_execution = Execution(
            id="execution-old",
            workflow_id="workflow-1",
            runtime_adapter_id="native",
            runtime_revision_id="runtime-rev-0",
            runtime_fingerprint="fp-0",
            status="running",
            container_id="container-old",
            container_name="agency-execution-old",
            container_image="agency-runtime:rev-0",
            container_status="running",
            created_by="user-executions",
        )
        current_execution = Execution(
            id="execution-current",
            workflow_id="workflow-1",
            runtime_adapter_id="native",
            runtime_revision_id="runtime-rev-1",
            runtime_fingerprint="fp-1",
            status="running",
            container_id="container-current",
            container_name="agency-execution-current",
            container_image="agency-runtime:rev-1",
            container_status="running",
            replacement_of_execution_id="execution-old",
            restart_reason="runtime_revision_superseded",
            created_by="user-executions",
        )
        next_execution = Execution(
            id="execution-next",
            workflow_id="workflow-1",
            runtime_adapter_id="native",
            runtime_revision_id="runtime-rev-1",
            runtime_fingerprint="fp-1",
            status="created",
            replacement_of_execution_id="execution-current",
            restart_reason="manual_retry",
            created_by="user-executions",
        )
        asyncio.run(cls.context.execution_store.save_execution(old_execution))
        asyncio.run(cls.context.execution_store.save_execution(current_execution))
        asyncio.run(cls.context.execution_store.save_execution(next_execution))
        asyncio.run(
            cls.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id="execution-current",
                    workflow_id="workflow-1",
                    event_type=ExecutionEventType.EXECUTION_STARTED,
                    sequence=1,
                    payload={},
                )
            )
        )
        asyncio.run(
            cls.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id="execution-current",
                    workflow_id="workflow-1",
                    agent_id="agent-reviewer",
                    actor="Reviewer",
                    task_id="task-review",
                    event_type=ExecutionEventType.AGENT_MESSAGE_CREATED,
                    sequence=2,
                    payload={"content": "Reviewed repository improvement ideas."},
                )
            )
        )

        async def fake_get_execution_state(execution_id: str):
            execution = await cls.context.execution_store.get_execution(execution_id)
            return type("Snapshot", (), {"execution": execution, "state": None})()

        class FakeRuntimeContainerManager:
            def list_managed_containers(self, *, all_containers: bool = True):
                return [
                    RuntimeContainerState(
                        container_id="container-current",
                        name="agency-execution-current",
                        image="agency-runtime:rev-1",
                        status="running",
                        labels={"agency.execution_id": "execution-current",
                                "agency.runtime_revision_id": "runtime-rev-1"},
                        started_at=utc_now(),
                    )
                ]

            def read_container_logs(self, container_id: str) -> str:
                return f"log line for {container_id}"

        class FakeRuntimeReconciler:
            async def reconcile_once(self):
                return ReconciliationReport(
                    scanned_executions=3,
                    scanned_containers=1,
                    actions=[
                        ReconciliationAction(
                            action="terminal_execution_container_reaped",
                            execution_id="execution-old",
                            container_id="container-old",
                            detail="Container status=running",
                        )
                    ],
                )

        cls.context.runtime_registry.get_execution_state = fake_get_execution_state
        cls.context.runtime_container_manager = FakeRuntimeContainerManager()
        cls.context.runtime_reconciler = FakeRuntimeReconciler()
        cls.context.runtime_operations.increment("reconcile.runs", 2)
        cls.context.runtime_operations.record_action("container_watch_exit", execution_id="execution-current")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def test_get_execution_includes_runtime_and_replacement_visibility(self):
        response = self.client.get("/executions/execution-current")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["execution"]["id"], "execution-current")
        self.assertEqual(payload["runtime"]["runtime_revision"]["id"], "runtime-rev-1")
        self.assertEqual(payload["runtime"]["container"]["container_id"], "container-current")
        self.assertEqual(payload["replacement"]["restart_reason"], "runtime_revision_superseded")
        self.assertEqual(payload["replacement"]["replaces_execution"]["id"], "execution-old")
        self.assertEqual(payload["replacement"]["replaced_by_executions"][0]["id"], "execution-next")

    def test_retry_failed_execution_task_queues_replacement_execution(self):
        workflow = WorkflowDefinition(
            id="workflow-retry-task",
            name="Retry Task Workflow",
            entrypoint="node-task-retry",
            task_definitions=[
                TaskDefinition(
                    id="task-retry",
                    name="Retry failed task",
                    description="Task that failed and can be retried.",
                )
            ],
            nodes=[
                WorkflowNodeDefinition(
                    id="node-task-retry",
                    name="Retry failed task",
                    node_type="task",
                    task_id="task-retry",
                )
            ],
        )
        failed_execution = Execution(
            id="execution-task-failed",
            workflow_id=workflow.id,
            runtime_adapter_id="native",
            status=ExecutionStatus.FAILED,
            input_payload={"topic": "retry"},
            trigger_payload={"type": "manual", "created_by": "user-executions"},
            created_by="user-executions",
        )
        asyncio.run(self.context.workflow_repo.create(workflow))
        asyncio.run(self.context.execution_store.save_execution(failed_execution))
        asyncio.run(
            self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id=failed_execution.id,
                    workflow_id=workflow.id,
                    task_id="task-retry",
                    event_type=ExecutionEventType.AGENT_STEP_FAILED,
                    sequence=1,
                    payload={"error": "Task failed"},
                )
            )
        )

        original_queue_start = self.context.control_plane.queue_start

        async def fake_queue_start(execution_id: str):
            execution = await self.context.execution_store.get_execution(execution_id)
            execution.status = ExecutionStatus.QUEUED
            await self.context.execution_store.update_execution(execution)
            return execution

        self.context.control_plane.queue_start = fake_queue_start
        try:
            response = self.client.post(
                "/executions/execution-task-failed/tasks/task-retry/retry",
                json={"reason": "Retry from graph"},
            )
        finally:
            self.context.control_plane.queue_start = original_queue_start

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["source_execution_id"], failed_execution.id)
        self.assertEqual(payload["task_id"], "task-retry")
        self.assertEqual(payload["node_id"], "node-task-retry")
        replacement_id = payload["replacement_execution_id"]
        replacement = asyncio.run(self.context.execution_store.get_execution(replacement_id))
        self.assertEqual(replacement.replacement_of_execution_id, failed_execution.id)
        self.assertEqual(replacement.metadata["task_retry"]["task_id"], "task-retry")
        self.assertEqual(replacement.metadata["task_retry"]["node_id"], "node-task-retry")

    def test_resume_from_checkpoint_queues_replacement_after_last_completed_node(self):
        workflow = WorkflowDefinition(
            id="workflow-checkpoint-resume",
            name="Checkpoint Resume Workflow",
            entrypoint="node-task-a",
            task_definitions=[
                TaskDefinition(
                    id="task-a",
                    name="Task A",
                    description="Completed task.",
                ),
                TaskDefinition(
                    id="task-b",
                    name="Task B",
                    description="Task to resume.",
                ),
            ],
            nodes=[
                WorkflowNodeDefinition(id="node-task-a", name="Task A", node_type="task", task_id="task-a"),
                WorkflowNodeDefinition(id="node-task-b", name="Task B", node_type="task", task_id="task-b"),
            ],
            edges=[
                WorkflowEdgeDefinition(
                    id="edge-a-b",
                    source_node_id="node-task-a",
                    target_node_id="node-task-b",
                )
            ],
        )
        failed_execution = Execution(
            id="execution-checkpoint-failed",
            workflow_id=workflow.id,
            runtime_adapter_id="native",
            status=ExecutionStatus.FAILED,
            input_payload={"topic": "checkpoint"},
            output_payload={
                "node_outputs": {
                    "node-task-a": "Task A output",
                },
                "checkpoint": {
                    "current_node_id": "node-task-b",
                    "current_task_id": "task-b",
                    "completed_node_ids": ["node-task-a"],
                },
            },
            trigger_payload={"type": "manual", "created_by": "user-executions"},
            created_by="user-executions",
        )
        asyncio.run(self.context.workflow_repo.create(workflow))
        asyncio.run(self.context.execution_store.save_execution(failed_execution))

        original_queue_start = self.context.control_plane.queue_start

        async def fake_queue_start(execution_id: str):
            execution = await self.context.execution_store.get_execution(execution_id)
            execution.status = ExecutionStatus.QUEUED
            await self.context.execution_store.update_execution(execution)
            return execution

        self.context.control_plane.queue_start = fake_queue_start
        try:
            response = self.client.post(
                "/executions/execution-checkpoint-failed/resume-from-checkpoint",
                json={"reason": "Resume from checkpoint"},
            )
        finally:
            self.context.control_plane.queue_start = original_queue_start

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["source_execution_id"], failed_execution.id)
        self.assertEqual(payload["resume_node_id"], "node-task-b")
        self.assertEqual(payload["task_id"], "task-b")
        replacement = asyncio.run(self.context.execution_store.get_execution(payload["replacement_execution_id"]))
        self.assertEqual(replacement.replacement_of_execution_id, failed_execution.id)
        self.assertEqual(replacement.metadata["checkpoint_resume"]["last_completed_node_id"], "node-task-a")
        self.assertEqual(replacement.metadata["checkpoint_resume"]["resume_node_id"], "node-task-b")
        self.assertEqual(replacement.metadata["task_retry"]["prior_node_outputs"]["node-task-a"], "Task A output")

    def test_list_runtime_revisions_endpoint(self):
        response = self.client.get("/executions/runtime/revisions")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["items"][0]["id"], "runtime-rev-1")

    def test_get_runtime_revision_includes_linked_executions(self):
        response = self.client.get("/executions/runtime/revisions/runtime-rev-1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["runtime_revision"]["id"], "runtime-rev-1")
        linked_ids = {item["id"] for item in payload["executions"]}
        self.assertEqual(linked_ids, {"execution-current", "execution-next"})

    def test_list_managed_runtime_containers_endpoint(self):
        response = self.client.get("/executions/runtime/containers")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["items"][0]["container_id"], "container-current")
        self.assertEqual(payload["items"][0]["labels"]["agency.runtime_revision_id"], "runtime-rev-1")

    def test_reconcile_runtime_endpoint_returns_report(self):
        response = self.client.post("/executions/runtime/reconcile")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scanned_executions"], 3)
        self.assertEqual(payload["scanned_containers"], 1)
        self.assertEqual(payload["actions"][0]["action"], "terminal_execution_container_reaped")

    def test_runtime_metrics_endpoint_returns_operation_snapshot(self):
        response = self.client.get("/executions/runtime/metrics")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["counters"]["reconcile.runs"], 2)
        self.assertEqual(payload["recent_actions"][0]["action"], "container_watch_exit")

    def test_execution_runtime_logs_endpoint_reads_container_logs(self):
        response = self.client.get("/executions/execution-current/runtime/logs")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["container_id"], "container-current")
        self.assertIn("container-current", payload["logs"])
        self.assertIn("Workflow execution started", payload["logs"])
        self.assertEqual(payload["workflow_logs"][0]["event_type"], "execution.started")
        self.assertEqual(payload["agent_logs"][0]["agent_name"], "Reviewer")
        self.assertIn("Reviewed repository improvement ideas", payload["agent_logs"][0]["logs"][0]["message"])
        self.assertIn("container-current", payload["raw_container_logs"])

    def test_execution_runtime_logs_endpoint_returns_empty_logs_without_container(self):
        response = self.client.get("/executions/execution-next/runtime/logs")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["container_id"])
        self.assertEqual(payload["execution_id"], "execution-next")
        self.assertEqual(payload["logs"], "")
