from __future__ import annotations

import unittest
from datetime import timedelta

from app.api.context import create_test_api_context
from app.core.time import utc_now
from app.domain import Execution, ExecutionStatus
from app.services.connector_retention import ConnectorRetentionService


class ConnectorRetentionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.service = ConnectorRetentionService(self.context)

    async def test_run_policy_prunes_old_connector_audits_but_keeps_latest_per_credential(self) -> None:
        now = utc_now()
        old_one = Execution(
            id="connector-old-1",
            workflow_id="connector-test",
            runtime_adapter_id="native",
            status=ExecutionStatus.COMPLETED,
            trigger_type="manual",
            trigger_payload={"mode": "connector_health_test"},
            metadata={"mode": "connector_health_test", "credential_id": "credential-1", "provider": "telegram"},
            created_at=now - timedelta(days=10),
            started_at=now - timedelta(days=10),
        )
        old_two = Execution(
            id="connector-old-2",
            workflow_id="connector-test",
            runtime_adapter_id="native",
            status=ExecutionStatus.COMPLETED,
            trigger_type="manual",
            trigger_payload={"mode": "connector_health_test"},
            metadata={"mode": "connector_health_test", "credential_id": "credential-1", "provider": "telegram"},
            created_at=now - timedelta(days=9),
            started_at=now - timedelta(days=9),
        )
        recent = Execution(
            id="connector-recent",
            workflow_id="connector-test",
            runtime_adapter_id="native",
            status=ExecutionStatus.COMPLETED,
            trigger_type="manual",
            trigger_payload={"mode": "connector_health_test"},
            metadata={"mode": "connector_health_test", "credential_id": "credential-1", "provider": "telegram"},
            created_at=now - timedelta(days=1),
            started_at=now - timedelta(days=1),
        )
        second_credential_old = Execution(
            id="connector-second-old",
            workflow_id="connector-test",
            runtime_adapter_id="native",
            status=ExecutionStatus.FAILED,
            trigger_type="manual",
            trigger_payload={"mode": "connector_health_test"},
            metadata={"mode": "connector_health_test", "credential_id": "credential-2", "provider": "discord"},
            created_at=now - timedelta(days=20),
            started_at=now - timedelta(days=20),
        )
        non_connector = Execution(
            id="workflow-exec",
            workflow_id="workflow-1",
            runtime_adapter_id="native",
            status=ExecutionStatus.COMPLETED,
            trigger_type="manual",
            metadata={},
            created_at=now - timedelta(days=20),
            started_at=now - timedelta(days=20),
        )

        for execution in (old_one, old_two, recent, second_credential_old, non_connector):
            await self.context.execution_store.save_execution(execution)

        report = await self.service.run_policy(
            started_before=now - timedelta(days=5),
            keep_latest_per_credential=1,
        )

        self.assertEqual(report.scanned, 4)
        self.assertEqual(report.matched, 2)
        self.assertEqual(report.deleted, 2)
        self.assertEqual(report.retained, 2)
        self.assertEqual(report.keepLatestPerCredential, 1)

        remaining_ids = {execution.id for execution in await self.context.execution_store.list_executions()}
        self.assertIn("connector-recent", remaining_ids)
        self.assertIn("connector-second-old", remaining_ids)
        self.assertIn("workflow-exec", remaining_ids)
        self.assertNotIn("connector-old-1", remaining_ids)
        self.assertNotIn("connector-old-2", remaining_ids)
        snapshot = self.context.runtime_operations.snapshot()
        self.assertEqual(snapshot.counters["connector_retention.runs"], 1)
        self.assertEqual(snapshot.counters["connector_retention.deleted"], 2)
        self.assertEqual(snapshot.recent_actions[-1]["action"], "connector_retention_run")
        self.assertEqual(snapshot.recent_actions[-1]["deleted"], 2)

    async def test_run_policy_retains_extra_recent_history_beyond_keep_latest(self) -> None:
        now = utc_now()
        latest = Execution(
            id="connector-latest",
            workflow_id="connector-test",
            runtime_adapter_id="native",
            status=ExecutionStatus.COMPLETED,
            trigger_type="manual",
            trigger_payload={"mode": "connector_health_test"},
            metadata={"mode": "connector_health_test", "credential_id": "credential-3", "provider": "telegram"},
            created_at=now - timedelta(days=1),
            started_at=now - timedelta(days=1),
        )
        recent_extra = Execution(
            id="connector-recent-extra",
            workflow_id="connector-test",
            runtime_adapter_id="native",
            status=ExecutionStatus.COMPLETED,
            trigger_type="manual",
            trigger_payload={"mode": "connector_health_test"},
            metadata={"mode": "connector_health_test", "credential_id": "credential-3", "provider": "telegram"},
            created_at=now - timedelta(days=2),
            started_at=now - timedelta(days=2),
        )
        stale = Execution(
            id="connector-stale",
            workflow_id="connector-test",
            runtime_adapter_id="native",
            status=ExecutionStatus.COMPLETED,
            trigger_type="manual",
            trigger_payload={"mode": "connector_health_test"},
            metadata={"mode": "connector_health_test", "credential_id": "credential-3", "provider": "telegram"},
            created_at=now - timedelta(days=40),
            started_at=now - timedelta(days=40),
        )

        for execution in (latest, recent_extra, stale):
            await self.context.execution_store.save_execution(execution)

        report = await self.service.run_policy(
            started_before=now - timedelta(days=30),
            keep_latest_per_credential=1,
        )

        self.assertEqual(report.scanned, 3)
        self.assertEqual(report.matched, 1)
        self.assertEqual(report.deleted, 1)
        self.assertEqual(report.retained, 2)

        remaining_ids = {execution.id for execution in await self.context.execution_store.list_executions()}
        self.assertIn("connector-latest", remaining_ids)
        self.assertIn("connector-recent-extra", remaining_ids)
        self.assertNotIn("connector-stale", remaining_ids)


if __name__ == "__main__":
    unittest.main()
