from __future__ import annotations

import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.domain import Execution, ExecutionEventType, ExecutionStatus, GoalDefinition, GoalStatus, WorkflowDefinition
from app.services.goals import GoalStartupReconciler


class GoalStartupReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.workflow = WorkflowDefinition(
            id="workflow-goal-reconcile",
            name="Goal Reconcile Workflow",
            nodes=[],
            edges=[],
            entrypoint="manual",
            task_definitions=[],
            agent_definitions=[],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.context.workflow_repo.create(self.workflow)

    async def _save_execution(self, execution: Execution) -> Execution:
        return await self.context.execution_store.save_execution(execution)

    async def test_reconciles_active_goal_links_back_to_execution(self) -> None:
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-reconcile-active",
                objective="Repair active goal execution links",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-needs-goal-link", "execution-missing"],
                success_criteria=[{"kind": "artifact", "description": "Evidence exists"}],
            )
        )
        await self._save_execution(
            Execution(
                id="execution-needs-goal-link",
                workflow_id=self.workflow.id,
                runtime_adapter_id="native",
                status=ExecutionStatus.RUNNING,
                input_payload={},
                metadata={},
            )
        )

        report = await GoalStartupReconciler(self.context).reconcile_once()

        self.assertEqual(report.repaired_execution_goal_links, 1)
        self.assertEqual(report.orphaned_goal_execution_references, 1)
        execution = await self.context.execution_store.get_execution("execution-needs-goal-link")
        assert execution is not None
        self.assertEqual(execution.goal_id, "goal-reconcile-active")
        self.assertEqual(execution.input_payload["goal_id"], "goal-reconcile-active")
        self.assertEqual(execution.trigger_payload["goal_id"], "goal-reconcile-active")
        events = await self.context.execution_store.list_events(execution.id)
        self.assertTrue(
            any(
                event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED
                and event.payload["category"] == "execution_goal_link_repaired"
                for event in events
            )
        )
        goal = await self.context.goal_repo.get("goal-reconcile-active")
        assert goal is not None
        reconciliation = goal.metadata["goal_startup_reconciliation"]
        self.assertEqual(reconciliation["missing_execution_ids"], ["execution-missing"])
        self.assertEqual(reconciliation["repaired_execution_ids"], ["execution-needs-goal-link"])

    async def test_reconciles_execution_goal_hint_back_to_goal(self) -> None:
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-from-execution-hint",
                objective="Adopt execution hints",
                status=GoalStatus.ACTIVE,
                success_criteria=[{"kind": "artifact", "description": "Evidence exists"}],
            )
        )
        await self._save_execution(
            Execution(
                id="execution-with-goal-hint",
                workflow_id=self.workflow.id,
                runtime_adapter_id="native",
                status=ExecutionStatus.RUNNING,
                trigger_payload={"goal_id": "goal-from-execution-hint"},
                input_payload={},
                metadata={},
            )
        )

        report = await GoalStartupReconciler(self.context).reconcile_once()

        self.assertEqual(report.repaired_goal_execution_links, 1)
        self.assertEqual(report.repaired_execution_goal_links, 1)
        goal = await self.context.goal_repo.get("goal-from-execution-hint")
        assert goal is not None
        self.assertEqual(goal.execution_ids, ["execution-with-goal-hint"])
        execution = await self.context.execution_store.get_execution("execution-with-goal-hint")
        assert execution is not None
        self.assertEqual(execution.goal_id, "goal-from-execution-hint")
        self.assertEqual(execution.metadata["goal_id"], "goal-from-execution-hint")

    async def test_active_goal_survives_restart_reconciliation_and_restores_execution_links(self) -> None:
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-restart-active",
                objective="Continue active goal after restart",
                status=GoalStatus.ACTIVE,
                success_criteria=[{"kind": "artifact", "description": "Restart evidence exists"}],
            )
        )
        await self._save_execution(
            Execution(
                id="execution-restart-active",
                workflow_id=self.workflow.id,
                runtime_adapter_id="native",
                status=ExecutionStatus.RUNNING,
                input_payload={"goal_id": "goal-restart-active"},
                metadata={},
            )
        )

        report = await GoalStartupReconciler(self.context).reconcile_once()

        self.assertEqual(report.active_goals_scanned, 1)
        self.assertEqual(report.repaired_goal_execution_links, 1)
        self.assertEqual(report.repaired_execution_goal_links, 1)
        goal = await self.context.goal_repo.get("goal-restart-active")
        assert goal is not None
        self.assertEqual(goal.status, GoalStatus.ACTIVE)
        self.assertEqual(goal.execution_ids, ["execution-restart-active"])
        execution = await self.context.execution_store.get_execution("execution-restart-active")
        assert execution is not None
        self.assertEqual(execution.goal_id, goal.id)
        self.assertEqual(execution.metadata["goal_id"], goal.id)
        self.assertEqual(execution.trigger_payload["goal_id"], goal.id)

    async def test_flags_execution_referencing_missing_goal(self) -> None:
        await self._save_execution(
            Execution(
                id="execution-orphaned-goal",
                workflow_id=self.workflow.id,
                runtime_adapter_id="native",
                status=ExecutionStatus.RUNNING,
                goal_id="goal-does-not-exist",
                input_payload={"goal_id": "goal-does-not-exist"},
                metadata={},
            )
        )

        report = await GoalStartupReconciler(self.context).reconcile_once()

        self.assertEqual(report.orphaned_execution_goal_references, 1)
        self.assertEqual(report.findings[0]["category"], "orphaned_execution_goal_reference")
        execution = await self.context.execution_store.get_execution("execution-orphaned-goal")
        assert execution is not None
        reconciliation = execution.metadata["goal_startup_reconciliation"]
        self.assertEqual(reconciliation["status"], "orphaned_execution_goal_reference")
        self.assertEqual(reconciliation["goal_id"], "goal-does-not-exist")
        events = await self.context.execution_store.list_events(execution.id)
        self.assertEqual(events[-1].payload["category"], "orphaned_execution_goal_reference")


class GoalStartupReconciliationLifespanTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_settings_cache()

    def test_app_lifespan_runs_goal_startup_reconciliation(self) -> None:
        reconcile_once = AsyncMock()
        with patch("app.api.main.GoalStartupReconciler.reconcile_once", reconcile_once):
            with TestClient(create_app(context=create_test_api_context())):
                time.sleep(0.1)

        reconcile_once.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
