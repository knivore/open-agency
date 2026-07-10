from __future__ import annotations

import unittest
from datetime import timedelta

from app.core.time import utc_now
from app.domain import Execution, ExecutionEvent, ExecutionEventType, ExecutionStatus
from app.runtime.native.state import InMemoryExecutionStore
from app.services.execution_classification import classify_execution_staleness


class ExecutionActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_event_records_agent_activity_metadata(self):
        store = InMemoryExecutionStore()
        execution = Execution(
            id="execution-activity",
            workflow_id="workflow-activity",
            runtime_adapter="native",
            status=ExecutionStatus.RUNNING,
            started_at=utc_now(),
        )
        await store.save_execution(execution)

        saved = await store.save_event(
            ExecutionEvent(
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                event_type=ExecutionEventType.LLM_REQUEST_CREATED,
                sequence=0,
                payload={"attempt": 1},
            )
        )

        current = await store.get_execution(execution.id)
        self.assertIsNotNone(current)
        assert current is not None
        activity = current.metadata["runtime_activity"]
        self.assertEqual(activity["last_activity_event_id"], saved.id)
        self.assertEqual(activity["last_activity_event_type"], "llm.request.created")
        self.assertEqual(activity["last_activity_sequence"], saved.sequence)

    async def test_heartbeat_does_not_hide_idle_agent_activity(self):
        now = utc_now()
        execution = Execution(
            id="execution-idle",
            workflow_id="workflow-idle",
            runtime_adapter="native",
            status=ExecutionStatus.RUNNING,
            started_at=now - timedelta(minutes=20),
            updated_at=now - timedelta(minutes=10),
            last_heartbeat_at=now,
            metadata={
                "runtime_activity": {
                    "last_activity_at": (now - timedelta(seconds=601)).isoformat(),
                    "last_activity_event_type": "llm.request.created",
                }
            },
        )

        classification = classify_execution_staleness(
            execution,
            stale_after_seconds=300,
            idle_timeout_seconds=600,
            run_timeout_seconds=7200,
        )

        self.assertTrue(classification["is_stale"])
        self.assertEqual(classification["stale_kind"], "alive_but_idle")
        self.assertEqual(classification["heartbeat_age_seconds"], 0)
        self.assertGreaterEqual(classification["activity_age_seconds"], 600)

    async def test_paused_execution_is_intentional_wait_not_stale(self):
        now = utc_now()
        execution = Execution(
            id="execution-paused",
            workflow_id="workflow-paused",
            runtime_adapter="native",
            status=ExecutionStatus.PAUSED,
            started_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
            metadata={"pending_subagent_input": {"status": "needs_input", "step_id": "step-review"}},
        )

        classification = classify_execution_staleness(
            execution,
            stale_after_seconds=300,
            idle_timeout_seconds=600,
            run_timeout_seconds=3600,
        )

        self.assertFalse(classification["is_stale"])
        self.assertTrue(classification["intentionally_waiting"])
        self.assertEqual(classification["wait_state"], "paused")
        self.assertIsNone(classification["stale_kind"])

    async def test_waiting_for_approval_execution_is_intentional_wait_not_stale(self):
        now = utc_now()
        execution = Execution(
            id="execution-approval-wait",
            workflow_id="workflow-approval-wait",
            runtime_adapter="native",
            status=ExecutionStatus.WAITING_FOR_APPROVAL,
            started_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
            metadata={"pending_subagent_approval": {"status": "needs_approval", "step_id": "step-approval"}},
        )

        classification = classify_execution_staleness(
            execution,
            stale_after_seconds=300,
            idle_timeout_seconds=600,
            run_timeout_seconds=3600,
        )

        self.assertFalse(classification["is_stale"])
        self.assertTrue(classification["eligible_status"])
        self.assertTrue(classification["intentionally_waiting"])
        self.assertEqual(classification["wait_state"], "waiting_for_approval")

    async def test_always_on_execution_suppresses_run_timeout_but_not_dead_heartbeat(self):
        now = utc_now()
        live_execution = Execution(
            id="execution-always-on-live",
            workflow_id="workflow-always-on",
            runtime_adapter="native",
            status=ExecutionStatus.RUNNING,
            started_at=now - timedelta(hours=2),
            updated_at=now,
            last_heartbeat_at=now,
            metadata={"execution_lifecycle": {"run_mode": "always_on"}},
        )

        live_classification = classify_execution_staleness(
            live_execution,
            stale_after_seconds=300,
            idle_timeout_seconds=600,
            run_timeout_seconds=3600,
        )

        self.assertFalse(live_classification["is_stale"])
        self.assertTrue(live_classification["intentionally_long_running"])

        dead_execution = live_execution.model_copy(
            update={
                "id": "execution-always-on-dead",
                "updated_at": now - timedelta(minutes=20),
                "last_heartbeat_at": now - timedelta(minutes=20),
            }
        )

        dead_classification = classify_execution_staleness(
            dead_execution,
            stale_after_seconds=300,
            idle_timeout_seconds=600,
            run_timeout_seconds=3600,
        )

        self.assertTrue(dead_classification["is_stale"])
        self.assertEqual(dead_classification["stale_kind"], "worker_unresponsive")
        self.assertTrue(dead_classification["intentionally_long_running"])


if __name__ == "__main__":
    unittest.main()
