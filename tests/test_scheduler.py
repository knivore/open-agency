from __future__ import annotations

import asyncio
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.api.routes.schedules import create_schedules_router
from app.core.config import reset_settings_cache
from app.core.time import utc_now
from app.domain import Execution, ExecutionStatus, UserDefinition, WorkflowDefinition
from app.scheduler.triggers import compute_next_fire


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

        with self.assertLogs("app.scheduler.scheduler", level="ERROR") as logs:
            results = await self.context.scheduler.run_due_schedules()

        self.assertEqual(results, [])
        self.assertTrue(any("Scheduled workflow fire failed" in message for message in logs.output))
        metrics = self.context.runtime_operations.snapshot_dict()
        self.assertEqual(metrics["counters"]["scheduler.due_fire_failed"], 1)
        self.assertEqual(metrics["counters"]["scheduler.due_fire_failed.ScheduleConcurrencyError"], 1)

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

        due = await self.context.scheduler.patch_schedule(
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
        import asyncio

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
            with self.assertLogs("app.api.main", level="WARNING") as logs:
                with TestClient(create_app(context=context)):
                    time.sleep(0.1)
        self.assertTrue(any("fire-claim support is unavailable" in message for message in logs.output))
        metrics = context.runtime_operations.snapshot_dict()
        self.assertEqual(metrics["counters"]["scheduler.fire_claim_support_missing"], 1)


if __name__ == "__main__":
    unittest.main()
