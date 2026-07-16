from __future__ import annotations

import asyncio
import unittest

from app.core.time import utc_now
from app.domain import Execution, ExecutionStatus, ExecutionWaitStatus
from app.runtime.native.approvals import ApprovalManager
from app.runtime.native.errors import ExecutionApprovalSuspendedError
from app.runtime.native.state import InMemoryExecutionStore


class ApprovalManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = InMemoryExecutionStore()
        self.execution = Execution(
            id="execution-durable-approval",
            workflow_id="workflow-durable-approval",
            runtime_adapter="native",
            status=ExecutionStatus.RUNNING,
            started_at=utc_now(),
        )
        await self.store.save_execution(self.execution)

    async def _wait_for_pending_request(self) -> dict:
        for _ in range(100):
            requests = await self.store.list_approval_requests(self.execution.id)
            if requests:
                return requests[-1]
            await asyncio.sleep(0.01)
        self.fail("Approval request was not persisted before the wait began")

    async def test_worker_suspends_and_consumes_decision_recorded_by_another_manager(self) -> None:
        worker_manager = ApprovalManager(self.store, poll_interval_seconds=0.01)
        api_manager = ApprovalManager(self.store, poll_interval_seconds=0.01)
        with self.assertRaises(ExecutionApprovalSuspendedError):
            await worker_manager.request_approval(
                execution_id=self.execution.id,
                tool_id="agency.voice.generate",
                payload={"text": "hello"},
            )
        request = await self._wait_for_pending_request()

        approved = await api_manager.approve(
            execution_id=self.execution.id,
            tool_id="agency.voice.generate",
            reason="Approved remotely",
        )
        decision = await worker_manager.request_approval(
            execution_id=self.execution.id,
            tool_id="agency.voice.generate",
            payload={"text": "hello"},
        )

        self.assertTrue(approved)
        self.assertTrue(decision.granted)
        self.assertEqual(decision.reason, "Approved remotely")
        persisted = await self.store.get_approval_request(request["id"])
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted["status"], "approved")
        waits = await self.store.list_execution_waits(self.execution.id)
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0].status, ExecutionWaitStatus.RESOLVED)
        current = await self.store.get_execution(self.execution.id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status, ExecutionStatus.WAITING_FOR_APPROVAL)
        self.assertNotIn("pending_approval", current.metadata)
        self.assertIn("active_wait", current.metadata)

    async def test_resolution_is_idempotent_but_conflicting_decision_is_rejected(self) -> None:
        worker_manager = ApprovalManager(self.store, poll_interval_seconds=0.01)
        api_manager = ApprovalManager(self.store, poll_interval_seconds=0.01)
        with self.assertRaises(ExecutionApprovalSuspendedError):
            await worker_manager.request_approval(
                execution_id=self.execution.id,
                tool_id="agency.voice.generate",
                payload={"text": "hello"},
            )
        await self._wait_for_pending_request()

        first = await api_manager.reject(
            execution_id=self.execution.id,
            tool_id="agency.voice.generate",
            reason="Not allowed",
        )
        repeated = await api_manager.reject(
            execution_id=self.execution.id,
            tool_id="agency.voice.generate",
            reason="Repeated click",
        )
        conflicting = await api_manager.approve(
            execution_id=self.execution.id,
            tool_id="agency.voice.generate",
            reason="Conflicting click",
        )
        decision = await worker_manager.request_approval(
            execution_id=self.execution.id,
            tool_id="agency.voice.generate",
            payload={"text": "hello"},
        )

        self.assertTrue(first)
        self.assertTrue(repeated)
        self.assertFalse(conflicting)
        self.assertFalse(decision.granted)
        self.assertEqual(decision.reason, "Not allowed")
        waits = await self.store.list_execution_waits(self.execution.id)
        self.assertEqual(waits[0].status, ExecutionWaitStatus.RESOLVED)

    async def test_repeated_tool_approvals_get_distinct_durable_records(self) -> None:
        worker_manager = ApprovalManager(self.store, poll_interval_seconds=0.01)
        api_manager = ApprovalManager(self.store, poll_interval_seconds=0.01)

        for cycle in range(2):
            with self.assertRaises(ExecutionApprovalSuspendedError):
                await worker_manager.request_approval(
                    execution_id=self.execution.id,
                    tool_id="agency.voice.generate",
                    payload={"text": f"cycle {cycle}"},
                )
            for _ in range(100):
                requests = await self.store.list_approval_requests(self.execution.id)
                if len(requests) == cycle + 1:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(await api_manager.approve(
                execution_id=self.execution.id,
                tool_id="agency.voice.generate",
                reason=f"Approved cycle {cycle}",
            ))
            self.assertTrue((await worker_manager.request_approval(
                execution_id=self.execution.id,
                tool_id="agency.voice.generate",
                payload={"text": f"cycle {cycle}"},
            )).granted)
            current = await self.store.get_execution(self.execution.id)
            current.status = ExecutionStatus.RUNNING
            current.metadata.pop("active_wait", None)
            await self.store.update_execution(current)

        requests = await self.store.list_approval_requests(self.execution.id)
        waits = await self.store.list_execution_waits(self.execution.id)
        self.assertEqual(len({item["id"] for item in requests}), 2)
        self.assertEqual(len({item.id for item in waits}), 2)
        self.assertTrue(all(item.status == ExecutionWaitStatus.RESOLVED for item in waits))

    async def test_resolved_approval_binds_canonical_arguments_and_is_consumed_once(self) -> None:
        worker_manager = ApprovalManager(self.store, poll_interval_seconds=0.01)
        api_manager = ApprovalManager(self.store, poll_interval_seconds=0.01)
        original = {"action": "publish", "options": {"audience": "internal", "priority": 2}}
        reordered = {"options": {"priority": 2, "audience": "internal"}, "action": "publish"}

        with self.assertRaises(ExecutionApprovalSuspendedError):
            await worker_manager.request_approval(
                execution_id=self.execution.id,
                tool_id="agency.http.request",
                payload=original,
            )
        first_request = await self._wait_for_pending_request()
        self.assertTrue(await api_manager.approve(
            execution_id=self.execution.id,
            tool_id="agency.http.request",
            reason="Approved exact invocation",
        ))

        decision = await worker_manager.request_approval(
            execution_id=self.execution.id,
            tool_id="agency.http.request",
            payload=reordered,
        )

        self.assertTrue(decision.granted)
        current = await self.store.get_execution(self.execution.id)
        assert current is not None
        self.assertNotIn("pending_approval", current.metadata)
        self.assertEqual(
            first_request["request_payload"]["invocation_digest"],
            ApprovalManager._invocation_digest(reordered),  # noqa: SLF001
        )

        with self.assertRaises(ExecutionApprovalSuspendedError):
            await worker_manager.request_approval(
                execution_id=self.execution.id,
                tool_id="agency.http.request",
                payload=reordered,
            )
        requests = await self.store.list_approval_requests(self.execution.id)
        self.assertEqual([item["status"] for item in requests], ["approved", "pending"])

    async def test_changed_arguments_require_a_fresh_approval(self) -> None:
        execution = self.execution.model_copy(update={"id": "execution-approval-argument-substitution"})
        await self.store.save_execution(execution)
        worker_manager = ApprovalManager(self.store, poll_interval_seconds=0.01)
        api_manager = ApprovalManager(self.store, poll_interval_seconds=0.01)
        approved_payload = {"method": "POST", "url": "https://api.example.test/reports/preview"}
        substituted_payload = {"method": "DELETE", "url": "https://api.example.test/reports/all"}

        with self.assertRaises(ExecutionApprovalSuspendedError):
            await worker_manager.request_approval(
                execution_id=execution.id,
                tool_id="agency.http.request",
                payload=approved_payload,
            )
        self.assertTrue(await api_manager.approve(
            execution_id=execution.id,
            tool_id="agency.http.request",
            reason="Approved preview only",
        ))

        with self.assertRaises(ExecutionApprovalSuspendedError):
            await worker_manager.request_approval(
                execution_id=execution.id,
                tool_id="agency.http.request",
                payload=substituted_payload,
            )

        requests = await self.store.list_approval_requests(execution.id)
        self.assertEqual([item["status"] for item in requests], ["approved", "pending"])
        self.assertNotEqual(
            requests[0]["request_payload"]["invocation_digest"],
            requests[1]["request_payload"]["invocation_digest"],
        )
        current = await self.store.get_execution(execution.id)
        assert current is not None
        self.assertEqual(
            current.metadata["pending_approval"]["invocation_digest"],
            requests[1]["request_payload"]["invocation_digest"],
        )

    async def test_redacted_payload_is_persisted_while_digest_binds_raw_arguments(self) -> None:
        manager = ApprovalManager(self.store, poll_interval_seconds=0.01)
        raw_payload = {"text": "typed-password-value", "x": 20}
        redacted_payload = {"text": "[REDACTED]", "x": 20}

        with self.assertRaises(ExecutionApprovalSuspendedError):
            await manager.request_approval(
                execution_id=self.execution.id,
                tool_id="mcp:computer:type",
                payload=raw_payload,
                redacted_payload=redacted_payload,
            )

        request = await self._wait_for_pending_request()
        self.assertEqual(request["request_payload"]["arguments"], redacted_payload)
        self.assertEqual(
            request["request_payload"]["invocation_digest"],
            ApprovalManager._invocation_digest(raw_payload),  # noqa: SLF001
        )
        current = await self.store.get_execution(self.execution.id)
        assert current is not None
        self.assertEqual(current.metadata["pending_approval"]["payload"], redacted_payload)
        waits = await self.store.list_execution_waits(self.execution.id)
        self.assertEqual(waits[-1].request_payload["arguments"], redacted_payload)

    async def test_failed_persistence_does_not_leave_a_phantom_waiter(self) -> None:
        class FailingApprovalStore(InMemoryExecutionStore):
            async def create_approval_request(self, **kwargs) -> str:
                raise RuntimeError("database unavailable")

        store = FailingApprovalStore()
        execution = self.execution.model_copy(update={"id": "execution-persistence-failure"})
        await store.save_execution(execution)
        manager = ApprovalManager(store, poll_interval_seconds=0.01)

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await manager.request_approval(
                execution_id=execution.id,
                tool_id="agency.voice.generate",
                payload={"text": "hello"},
            )

        approved = await manager.approve(
            execution_id=execution.id,
            tool_id="agency.voice.generate",
            reason="Should not resolve",
        )
        self.assertFalse(approved)


if __name__ == "__main__":
    unittest.main()
