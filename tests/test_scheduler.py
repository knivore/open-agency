from __future__ import annotations

import asyncio
import logging
import os
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.api.routes.schedules import create_schedules_router
from app.core.config import reset_settings_cache
from app.core.time import utc_now
from app.domain import Execution, ExecutionStatus, GoalDefinition, GoalStatus, UserDefinition, WorkflowDefinition
from app.runtime.native.errors import WorkflowNotFoundError
from app.scheduler.triggers import compute_next_fire


@contextmanager
def assert_logs_with_enabled_logger(test_case: unittest.TestCase, logger_name: str, level: str):
    logger = logging.getLogger(logger_name)
    previous_disabled = logger.disabled
    previous_logging_disable = logging.root.manager.disable
    logger.disabled = False
    logging.disable(logging.NOTSET)
    try:
        with test_case.assertLogs(logger_name, level=level) as logs:
            yield logs
    finally:
        logger.disabled = previous_disabled
        logging.disable(previous_logging_disable)


class SchedulerApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = create_test_api_context()
        self.workflow = WorkflowDefinition(
            id="workflow-schedule",
            name="Scheduled Workflow",
            nodes=[],
            edges=[],
            entrypoint="manual",
            task_definitions=[],
            agent_definitions=[],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.context.workflow_repo.create(self.workflow)
        self.context.scheduler.execution_starter = AsyncMock()

    async def test_cron_and_interval_schedule_computation(self):
        cron_schedule = await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-cron",
                    "name": "Cron",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "cron",
                    "trigger_config": {"cron": "*/5 * * * *"},
                    "input_template": {},
                    "timezone": "UTC",
                }
            )
        )
        self.assertIsNotNone(cron_schedule.next_fire_at)

        interval_schedule = await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-interval",
                    "name": "Interval",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "interval",
                    "trigger_config": {"interval_seconds": 60},
                    "input_template": {},
                    "timezone": "UTC",
                    "next_fire_at": utc_now().isoformat(),
                }
            )
        )
        self.assertIsNotNone(interval_schedule.next_fire_at)

    async def test_cron_schedule_computation_uses_schedule_timezone(self):
        schedule = self.context.schedule_repo.model_cls.model_validate(
            {
                "id": "schedule-cron-timezone",
                "name": "Cron Timezone",
                "workflow_id": self.workflow.id,
                "trigger_type": "cron",
                "trigger_config": {"cron": "0 7 * * *"},
                "input_template": {},
                "timezone": "Asia/Singapore",
            }
        )
        computation = compute_next_fire(schedule, now=datetime(2026, 5, 14, 22, 30, tzinfo=timezone.utc))
        self.assertEqual(computation.next_fire_at, datetime(2026, 5, 14, 23, 0, tzinfo=timezone.utc))

    async def test_trigger_now_and_concurrency(self):
        schedule = await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-manual",
                    "name": "Manual",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "manual",
                    "trigger_config": {},
                    "input_template": {"topic": "hello"},
                    "timezone": "UTC",
                    "max_concurrent_executions": 1,
                }
            )
        )
        result = await self.context.scheduler.trigger_now(schedule.id)
        self.assertIsNotNone(result.execution_id)
        execution = await self.context.execution_store.get_execution(result.execution_id)
        self.assertIsNotNone(execution)
        assert execution is not None
        self.assertEqual(execution.trigger_type, "schedule")
        self.assertEqual(execution.metadata["execution_lifecycle"]["run_mode"], "scheduled")
        self.assertTrue(execution.metadata["execution_lifecycle"]["terminate_container_on_completion"])

        active_execution = Execution(
            id="active-execution",
            workflow_id=self.workflow.id,
            runtime_adapter_id="native",
            status=ExecutionStatus.RUNNING,
            input_payload={},
            metadata={"trigger": {"schedule_id": schedule.id}},
        )
        await self.context.execution_store.save_execution(active_execution)
        with self.assertRaisesRegex(Exception, "max_concurrent_executions"):
            await self.context.scheduler.trigger_now(schedule.id)

    async def test_trigger_now_queues_execution_and_advances_interval_schedule(self):
        now = utc_now()
        schedule = await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-interval-due",
                    "name": "Interval Due",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "interval",
                    "trigger_config": {"interval_seconds": 60},
                    "input_template": {},
                    "timezone": "UTC",
                    "next_fire_at": (now - timedelta(seconds=1)).isoformat(),
                }
            )
        )

        result = await self.context.scheduler.trigger_now(schedule.id)
        self.context.scheduler.execution_starter.assert_awaited_once_with(result.execution_id)
        updated = await self.context.schedule_repo.get(schedule.id)
        self.assertGreaterEqual(updated.next_fire_at, now + timedelta(seconds=55))

    async def test_due_schedule_can_create_goal_for_execution(self):
        now = utc_now()
        schedule = await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-goal-create",
                    "name": "Create Scheduled Goal",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "interval",
                    "trigger_config": {"interval_seconds": 60},
                    "input_template": {"topic": "daily summary"},
                    "timezone": "UTC",
                    "next_fire_at": (now - timedelta(seconds=1)).isoformat(),
                    "metadata": {
                        "goal": {
                            "mode": "create",
                            "objective": "Produce the daily summary",
                            "success_criteria": [
                                {"kind": "artifact", "description": "Daily summary artifact is attached"}
                            ],
                            "priority": "high",
                            "owner_actor": "scheduler",
                        }
                    },
                }
            )
        )

        results = await self.context.scheduler.run_due_schedules()

        self.assertEqual(len(results), 1)
        goal_id = results[0].metadata["goal_id"]
        self.assertTrue(goal_id)
        execution = await self.context.execution_store.get_execution(results[0].execution_id)
        self.assertIsNotNone(execution)
        assert execution is not None
        self.assertEqual(execution.goal_id, goal_id)
        self.assertEqual(execution.input_payload["goal_id"], goal_id)
        self.assertEqual(execution.trigger_payload["goal_id"], goal_id)
        goal = await self.context.goal_repo.get(goal_id)
        self.assertIsNotNone(goal)
        assert goal is not None
        self.assertEqual(goal.status, GoalStatus.ACTIVE)
        self.assertEqual(goal.objective, "Produce the daily summary")
        self.assertEqual(goal.priority, "high")
        self.assertIn(execution.id, goal.execution_ids)
        updated = await self.context.schedule_repo.get(schedule.id)
        self.assertEqual(updated.metadata["last_goal_id"], goal_id)
        self.assertEqual(updated.metadata["last_execution_id"], execution.id)

    async def test_trigger_now_can_continue_existing_goal(self):
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-scheduled-continuation",
                objective="Keep collecting scheduled evidence",
                status=GoalStatus.ACTIVE,
                success_criteria=[{"kind": "artifact", "description": "Scheduled evidence exists"}],
            )
        )
        schedule = await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-goal-continue",
                    "name": "Continue Scheduled Goal",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "manual",
                    "trigger_config": {},
                    "input_template": {},
                    "timezone": "UTC",
                    "metadata": {"goal": {"mode": "continue", "goal_id": goal.id}},
                }
            )
        )

        result = await self.context.scheduler.trigger_now(schedule.id)

        execution = await self.context.execution_store.get_execution(result.execution_id)
        self.assertIsNotNone(execution)
        assert execution is not None
        self.assertEqual(result.metadata["goal_id"], goal.id)
        self.assertEqual(execution.goal_id, goal.id)
        self.assertEqual(execution.input_payload["goal_id"], goal.id)
        updated_goal = await self.context.goal_repo.get(goal.id)
        self.assertEqual(updated_goal.execution_ids, [execution.id])
        self.assertEqual(len(await self.context.goal_repo.list()), 1)

    async def test_event_schedule_can_create_goal_and_wake_supervisor(self):
        wake = AsyncMock()
        self.context.scheduler.goal_supervisor_waker = wake
        await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-event-goal-create",
                    "name": "Create Goal On Event",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "event_match",
                    "trigger_config": {
                        "event_type": "deployment.completed",
                        "payload_matches": {"environment": "prod", "service.name": "api"},
                    },
                    "input_template": {"source": "event"},
                    "timezone": "UTC",
                    "metadata": {
                        "goal": {
                            "mode": "create",
                            "objective": "Verify production deployment",
                            "success_criteria": [
                                {"kind": "artifact", "description": "Deployment verification exists"}
                            ],
                        },
                        "goal_supervisor": {"wake_on_event": True},
                    },
                }
            )
        )
        await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-event-nonmatch",
                    "name": "Nonmatching Event",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "event_match",
                    "trigger_config": {"event_type": "deployment.failed"},
                    "input_template": {},
                    "timezone": "UTC",
                }
            )
        )

        results = await self.context.scheduler.dispatch_event(
            event_type="deployment.completed",
            payload={"environment": "prod", "service": {"name": "api"}},
            source="test-event",
        )

        self.assertEqual(len(results), 1)
        goal_id = results[0].metadata["goal_id"]
        wake.assert_awaited_once_with(goal_id)
        execution = await self.context.execution_store.get_execution(results[0].execution_id)
        assert execution is not None
        self.assertEqual(execution.goal_id, goal_id)
        self.assertEqual(execution.trigger_payload["event"]["event_type"], "deployment.completed")
        self.assertEqual(execution.trigger_payload["event"]["payload"]["service"]["name"], "api")
        goal = await self.context.goal_repo.get(goal_id)
        assert goal is not None
        self.assertEqual(goal.objective, "Verify production deployment")
        self.assertEqual(goal.execution_ids, [execution.id])
        metrics = self.context.runtime_operations.snapshot_dict()
        self.assertEqual(metrics["counters"]["scheduler.event_dispatched"], 1)
        self.assertEqual(metrics["counters"]["scheduler.goal_supervisor_woken"], 1)

    async def test_event_schedule_default_wake_runs_scoped_goal_monitor(self):
        await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-event-default-wake",
                    "name": "Default Wake",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "event_match",
                    "trigger_config": {"event_type": "deployment.ready"},
                    "input_template": {},
                    "timezone": "UTC",
                    "metadata": {
                        "goal": {
                            "mode": "create",
                            "objective": "Inspect deployment readiness",
                            "success_criteria": [{"kind": "artifact", "description": "Readiness evidence exists"}],
                        },
                        "goal_supervisor": {"wake_on_event": True},
                    },
                }
            )
        )

        results = await self.context.scheduler.dispatch_event(event_type="deployment.ready", payload={})

        self.assertEqual(len(results), 1)
        goal_id = results[0].metadata["goal_id"]
        metrics = self.context.runtime_operations.snapshot_dict()
        wake_actions = [
            action for action in metrics["recent_actions"] if action["action"] == "main_agent_monitor.goal_wake"
        ]
        self.assertEqual(len(wake_actions), 1)
        self.assertEqual(wake_actions[0]["goal_id"], goal_id)
        self.assertEqual(metrics["counters"]["action.main_agent_monitor.goal_wake"], 1)

    async def test_event_schedule_can_continue_existing_goal(self):
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-event-continuation",
                objective="Track deployment events",
                status=GoalStatus.ACTIVE,
                success_criteria=[{"kind": "artifact", "description": "Deployment event evidence exists"}],
            )
        )
        await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-event-goal-continue",
                    "name": "Continue Goal On Event",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "event_match",
                    "trigger_config": {"event_types": ["deployment.completed", "deployment.verified"]},
                    "input_template": {},
                    "timezone": "UTC",
                    "metadata": {"goal": {"mode": "continue", "goal_id": goal.id}},
                }
            )
        )

        results = await self.context.scheduler.dispatch_event(
            event_type="deployment.verified",
            payload={"environment": "staging"},
        )

        self.assertEqual(len(results), 1)
        execution = await self.context.execution_store.get_execution(results[0].execution_id)
        assert execution is not None
        self.assertEqual(results[0].metadata["goal_id"], goal.id)
        self.assertEqual(execution.goal_id, goal.id)
        updated_goal = await self.context.goal_repo.get(goal.id)
        self.assertEqual(updated_goal.execution_ids, [execution.id])

    async def test_schedule_fire_claim_prevents_duplicate_fire(self):
        fire_at = utc_now()
        acquired = await self.context.schedule_repo.acquire_schedule_fire_claim(
            schedule_id="schedule-claim",
            scheduled_fire_at=fire_at,
            claimed_by="scheduler-a",
            lease_seconds=300,
        )
        duplicate = await self.context.schedule_repo.acquire_schedule_fire_claim(
            schedule_id="schedule-claim",
            scheduled_fire_at=fire_at,
            claimed_by="scheduler-b",
            lease_seconds=300,
        )

        self.assertTrue(acquired)
        self.assertFalse(duplicate)

        await self.context.schedule_repo.mark_schedule_fire_claim_fired(
            schedule_id="schedule-claim",
            scheduled_fire_at=fire_at,
            execution_id="execution-claim",
            claimed_by="scheduler-a",
        )
        after_fired = await self.context.schedule_repo.acquire_schedule_fire_claim(
            schedule_id="schedule-claim",
            scheduled_fire_at=fire_at,
            claimed_by="scheduler-b",
            lease_seconds=300,
        )
        self.assertFalse(after_fired)

    async def test_due_schedule_failure_is_logged_and_recorded(self):
        schedule = await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-concurrency-due",
                    "name": "Concurrency Due",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "interval",
                    "trigger_config": {"interval_seconds": 60},
                    "input_template": {},
                    "timezone": "UTC",
                    "max_concurrent_executions": 1,
                    "next_fire_at": (utc_now() - timedelta(seconds=1)).isoformat(),
                }
            )
        )
        active_execution = Execution(
            id="active-schedule-concurrency",
            workflow_id=self.workflow.id,
            runtime_adapter_id="native",
            status=ExecutionStatus.RUNNING,
            input_payload={},
            metadata={"trigger": {"schedule_id": schedule.id}},
        )
        await self.context.execution_store.save_execution(active_execution)

        with assert_logs_with_enabled_logger(self, "app.scheduler.scheduler", level="ERROR") as logs:
            results = await self.context.scheduler.run_due_schedules()

        self.assertEqual(results, [])
        self.assertTrue(any("Scheduled workflow fire failed" in message for message in logs.output))
        metrics = self.context.runtime_operations.snapshot_dict()
        self.assertEqual(metrics["counters"]["scheduler.due_fire_failed"], 1)
        self.assertEqual(metrics["counters"]["scheduler.due_fire_failed.ScheduleConcurrencyError"], 1)

    async def test_due_schedule_missing_workflow_is_disabled(self):
        schedule = await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-missing-workflow",
                    "name": "Missing Workflow",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "interval",
                    "trigger_config": {"interval_seconds": 60},
                    "input_template": {},
                    "timezone": "UTC",
                }
            )
        )
        await self.context.workflow_repo.soft_delete(self.workflow.id)
        await self.context.scheduler.patch_schedule(
            schedule.id,
            {"next_fire_at": (utc_now() - timedelta(seconds=1)).isoformat()},
        )

        with assert_logs_with_enabled_logger(self, "app.scheduler.scheduler", level="WARNING") as logs:
            results = await self.context.scheduler.run_due_schedules()

        self.assertEqual(results, [])
        disabled = await self.context.schedule_repo.get(schedule.id)
        self.assertIsNotNone(disabled)
        self.assertFalse(disabled.enabled)
        self.assertIsNone(disabled.next_fire_at)
        self.assertTrue(any("Disabled schedule" in message for message in logs.output))
        metrics = self.context.runtime_operations.snapshot_dict()
        self.assertEqual(metrics["counters"]["scheduler.schedule_disabled_missing_workflow"], 1)
        self.assertEqual(metrics["counters"]["scheduler.due_fire_missing_workflow"], 1)
        self.assertEqual(metrics["counters"]["scheduler.due_fire_missing_workflow.WorkflowNotFoundError"], 1)

    async def test_trigger_now_missing_workflow_disables_schedule(self):
        schedule = await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-trigger-missing-workflow",
                    "name": "Trigger Missing Workflow",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "manual",
                    "trigger_config": {},
                    "input_template": {},
                    "timezone": "UTC",
                    "metadata": {
                        "goal": {
                            "mode": "create",
                            "objective": "Should not persist without an execution",
                        }
                    },
                }
            )
        )
        await self.context.workflow_repo.soft_delete(self.workflow.id)

        with self.assertRaises(WorkflowNotFoundError):
            await self.context.scheduler.trigger_now(schedule.id)

        disabled = await self.context.schedule_repo.get(schedule.id)
        self.assertIsNotNone(disabled)
        self.assertFalse(disabled.enabled)
        self.assertIsNone(disabled.next_fire_at)
        self.assertEqual(await self.context.goal_repo.list(), [])

    async def test_enable_disable_and_due_run(self):
        schedule = await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-enable",
                    "name": "Enable",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "interval",
                    "trigger_config": {"interval_seconds": 30},
                    "input_template": {},
                    "timezone": "UTC",
                    "enabled": False,
                }
            )
        )
        enabled = await self.context.scheduler.enable_schedule(schedule.id)
        self.assertTrue(enabled.enabled)
        disabled = await self.context.scheduler.disable_schedule(schedule.id)
        self.assertFalse(disabled.enabled)

        await self.context.scheduler.patch_schedule(
            schedule.id,
            {
                "enabled": True,
                "next_fire_at": (utc_now() - timedelta(seconds=1)).isoformat(),
            },
        )
        results = await self.context.scheduler.run_due_schedules()
        self.assertEqual(len(results), 1)

    async def test_patch_schedule_recomputes_next_fire_for_cron_changes(self):
        schedule = await self.context.scheduler.create_schedule(
            self.context.schedule_repo.model_cls.model_validate(
                {
                    "id": "schedule-reschedule",
                    "name": "Reschedule",
                    "workflow_id": self.workflow.id,
                    "trigger_type": "cron",
                    "trigger_config": {"cron": "0 7 * * *"},
                    "input_template": {},
                    "timezone": "Asia/Singapore",
                }
            )
        )
        original_next_fire_at = schedule.next_fire_at

        patched = await self.context.scheduler.patch_schedule(
            schedule.id,
            {
                "trigger_type": "cron",
                "trigger_config": {"cron": "0 8 * * *"},
                "timezone": "Asia/Singapore",
            },
        )

        self.assertIsNotNone(patched)
        self.assertEqual(patched.trigger_config["cron"], "0 8 * * *")
        self.assertIsNotNone(patched.next_fire_at)
        self.assertNotEqual(patched.next_fire_at, original_next_fire_at)


class SchedulerRouteTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        self.context.scheduler.execution_starter = AsyncMock()
        self.context.workflow_repo._items = {} if hasattr(self.context.workflow_repo,
                                                          "_items") else None  # noqa: SLF001

        asyncio.run(
            self.context.workflow_repo.create(
                WorkflowDefinition(
                    id="workflow-schedule",
                    name="Scheduled Workflow",
                    nodes=[],
                    edges=[],
                    entrypoint="manual",
                    task_definitions=[],
                    agent_definitions=[],
                    tool_definitions=[],
                    default_runtime_adapter_id="native",
                )
            )
        )
        app = FastAPI()
        app.include_router(create_schedules_router(self.context))
        self.client = TestClient(app)
        self.client.headers.update(
            {
                "x-agency-user-id": "user-schedules",
                "x-agency-user-email": "schedules@example.com",
            }
        )
        asyncio.run(
            self.context.user_repo.create(
                UserDefinition(id="user-schedules", email="schedules@example.com", display_name="Schedules User")
            )
        )

    def test_schedule_routes(self):
        payload = {
            "id": "schedule-api",
            "name": "API Schedule",
            "workflow_id": "workflow-schedule",
            "schedule_type": "cron",
            "cron": "0 2 * * *",
            "input_payload": {},
            "timezone": "UTC",
        }
        create = self.client.post("/schedules", json=payload)
        self.assertEqual(create.status_code, 200)
        self.assertEqual(self.client.get("/schedules/schedule-api").status_code, 200)
        self.assertEqual(self.client.patch("/schedules/schedule-api", json={"enabled": False}).json()["enabled"], False)
        self.assertTrue(self.client.post("/schedules/schedule-api/enable").json()["enabled"])
        self.assertFalse(self.client.post("/schedules/schedule-api/disable").json()["enabled"])
        trigger = self.client.post("/schedules/schedule-api/trigger-now")
        self.assertEqual(trigger.status_code, 200)
        self.assertTrue(trigger.json()["execution_id"])

    def test_schedule_event_dispatch_route(self):
        create = self.client.post(
            "/schedules",
            json={
                "id": "schedule-event-api",
                "name": "API Event Schedule",
                "workflow_id": "workflow-schedule",
                "trigger_type": "event_match",
                "trigger_config": {
                    "event_type": "ticket.created",
                    "payload_matches": {"priority": "high"},
                },
                "input_template": {},
                "timezone": "UTC",
            },
        )
        self.assertEqual(create.status_code, 200)

        dispatch = self.client.post(
            "/schedules/events/dispatch",
            json={
                "event_type": "ticket.created",
                "payload": {"priority": "high", "ticket_id": "T-1"},
                "source": "test-route",
            },
        )

        self.assertEqual(dispatch.status_code, 200)
        body = dispatch.json()
        self.assertEqual(body["count"], 1)
        self.assertTrue(body["items"][0]["execution_id"])
        self.assertEqual(body["items"][0]["metadata"]["source"], "test-route")


class SchedulerLifespanTests(unittest.TestCase):
    def tearDown(self) -> None:
        for key in [
            "WORKFLOW_SCHEDULER_ENABLED",
            "WORKFLOW_SCHEDULER_INTERVAL_SECONDS",
        ]:
            os.environ.pop(key, None)
        reset_settings_cache()

    def test_app_lifespan_starts_workflow_scheduler_loop_when_enabled(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "WORKFLOW_SCHEDULER_ENABLED": "true",
                    "WORKFLOW_SCHEDULER_INTERVAL_SECONDS": "1",
                },
                clear=False,
        ):
            reset_settings_cache()
            context = create_test_api_context()
            scheduler_mock = AsyncMock(return_value=[])
            context.scheduler.run_due_schedules = scheduler_mock
            with TestClient(create_app(context=context)):
                time.sleep(0.1)
        self.assertGreaterEqual(scheduler_mock.await_count, 1)

    def test_app_lifespan_warns_when_scheduler_fire_claim_support_is_unavailable(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "WORKFLOW_SCHEDULER_ENABLED": "true",
                    "WORKFLOW_SCHEDULER_INTERVAL_SECONDS": "1",
                },
                clear=False,
        ):
            reset_settings_cache()
            context = create_test_api_context()
            context.scheduler.fire_claim_support_available = lambda: False
            context.scheduler.run_due_schedules = AsyncMock(return_value=[])
            with assert_logs_with_enabled_logger(self, "app.api.main", level="WARNING") as logs:
                with TestClient(create_app(context=context)):
                    time.sleep(0.1)
        self.assertTrue(any("fire-claim support is unavailable" in message for message in logs.output))
        metrics = context.runtime_operations.snapshot_dict()
        self.assertEqual(metrics["counters"]["scheduler.fire_claim_support_missing"], 1)


if __name__ == "__main__":
    unittest.main()
