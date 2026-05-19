from __future__ import annotations

import unittest
from datetime import datetime

from app.core.time import utc_now
from app.domain import Execution, ExecutionStatus
from app.runtime.containers import RuntimeContainerState
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import InMemoryExecutionStore, NativeExecutionState
from app.runtime.reconcile import RuntimeReconciler
from app.runtime.worker_protocol import WORKER_EXIT_CANCELLED, WORKER_EXIT_SUCCESS, WORKER_EXIT_WORKFLOW_FAILED


class _FakeRuntimeContainerManager:
    def __init__(self, containers: list[RuntimeContainerState]):
        self._containers = {container.container_id: container for container in containers}
        self.stopped: list[str] = []
        self.removed: list[tuple[str, bool]] = []

    def list_managed_containers(self, all_containers: bool = True) -> list[RuntimeContainerState]:
        return list(self._containers.values())

    def stop_container(self, container_id: str, *, timeout: int = 10) -> RuntimeContainerState:
        current = self._containers[container_id]
        stopped = RuntimeContainerState(
            container_id=current.container_id,
            name=current.name,
            image=current.image,
            status="exited",
            labels=current.labels,
            started_at=current.started_at,
            finished_at=current.finished_at or utc_now(),
            exit_code=current.exit_code if current.exit_code is not None else 0,
        )
        self._containers[container_id] = stopped
        self.stopped.append(container_id)
        return stopped

    def remove_container(self, container_id: str, *, force: bool = False) -> None:
        self.removed.append((container_id, force))
        self._containers.pop(container_id, None)


class RuntimeReconcilerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.execution_store = InMemoryExecutionStore()

    async def _save_execution(
            self,
            *,
            execution_id: str,
            status: ExecutionStatus,
            container_id: str | None = None,
            container_status: str | None = None,
    ) -> Execution:
        execution = Execution(
            id=execution_id,
            workflow_id="workflow-1",
            runtime_adapter_id="native",
            runtime_revision_id="rev-1",
            runtime_fingerprint="fp-1",
            status=status,
            container_id=container_id,
            container_name=f"agency-execution-{execution_id}" if container_id else None,
            container_image="agency-runtime:rev-1" if container_id else None,
            container_status=container_status,
        )
        await self.execution_store.save_execution(execution)
        emitter = ExecutionEventEmitter(self.execution_store)
        await emitter.emit(
            NativeExecutionState(execution_id=execution.id, workflow_id=execution.workflow_id),
            event_type="execution.created",
            payload={},
        )
        return execution

    async def test_reconcile_marks_active_execution_failed_when_container_missing(self) -> None:
        execution = await self._save_execution(
            execution_id="exec-missing",
            status=ExecutionStatus.RUNNING,
            container_id="container-missing",
            container_status="running",
        )
        reconciler = RuntimeReconciler(
            execution_store=self.execution_store,
            runtime_container_manager=_FakeRuntimeContainerManager([]),
        )

        report = await reconciler.reconcile_once()

        current = await self.execution_store.get_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        assert current is not None
        self.assertEqual(current.status, ExecutionStatus.FAILED)
        self.assertEqual(current.container_status, "missing")
        self.assertEqual(report.actions[0].action, "execution_missing_container")
        event_types = [event.event_type.value for event in events]
        self.assertIn("container.failed", event_types)
        self.assertIn("execution.failed", event_types)

    async def test_reconcile_marks_active_execution_failed_when_container_exits(self) -> None:
        finished_at = utc_now()
        execution = await self._save_execution(
            execution_id="exec-exited",
            status=ExecutionStatus.RUNNING,
            container_id="container-exited",
            container_status="running",
        )
        manager = _FakeRuntimeContainerManager(
            [
                RuntimeContainerState(
                    container_id="container-exited",
                    name="agency-execution-exec-exited",
                    image="agency-runtime:rev-1",
                    status="exited",
                    labels={"agency.execution_id": execution.id},
                    finished_at=finished_at,
                    exit_code=WORKER_EXIT_WORKFLOW_FAILED,
                )
            ]
        )
        reconciler = RuntimeReconciler(execution_store=self.execution_store, runtime_container_manager=manager)

        report = await reconciler.reconcile_once()

        current = await self.execution_store.get_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        assert current is not None
        self.assertEqual(current.status, ExecutionStatus.FAILED)
        self.assertEqual(current.container_status, "removed")
        self.assertEqual(current.container_exit_code, WORKER_EXIT_WORKFLOW_FAILED)
        self.assertEqual(current.container_ended_at, finished_at)
        self.assertEqual(report.actions[0].action, "execution_container_exited")
        self.assertEqual(report.actions[1].action, "execution_container_removed")
        self.assertEqual(manager.removed, [("container-exited", True)])
        event_types = [event.event_type.value for event in events]
        self.assertIn("container.failed", event_types)
        self.assertIn("execution.failed", event_types)

    async def test_reconcile_marks_active_execution_completed_when_worker_exits_zero(self) -> None:
        finished_at = utc_now()
        execution = await self._save_execution(
            execution_id="exec-completed",
            status=ExecutionStatus.RUNNING,
            container_id="container-completed",
            container_status="running",
        )
        manager = _FakeRuntimeContainerManager(
            [
                RuntimeContainerState(
                    container_id="container-completed",
                    name="agency-execution-exec-completed",
                    image="agency-runtime:rev-1",
                    status="exited",
                    labels={"agency.execution_id": execution.id},
                    finished_at=finished_at,
                    exit_code=WORKER_EXIT_SUCCESS,
                )
            ]
        )
        reconciler = RuntimeReconciler(execution_store=self.execution_store, runtime_container_manager=manager)

        report = await reconciler.reconcile_once()

        current = await self.execution_store.get_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        assert current is not None
        self.assertEqual(current.status, ExecutionStatus.COMPLETED)
        self.assertEqual(current.container_status, "removed")
        self.assertEqual(current.container_exit_code, WORKER_EXIT_SUCCESS)
        self.assertEqual(report.actions[0].action, "execution_container_exited")
        self.assertEqual(report.actions[1].action, "execution_container_removed")
        self.assertEqual(manager.removed, [("container-completed", True)])
        event_types = [event.event_type.value for event in events]
        self.assertIn("container.stopped", event_types)
        self.assertIn("execution.completed", event_types)

    async def test_reconcile_marks_active_execution_cancelled_when_worker_exits_cancelled(self) -> None:
        finished_at = utc_now()
        execution = await self._save_execution(
            execution_id="exec-cancelled",
            status=ExecutionStatus.RUNNING,
            container_id="container-cancelled",
            container_status="running",
        )
        manager = _FakeRuntimeContainerManager(
            [
                RuntimeContainerState(
                    container_id="container-cancelled",
                    name="agency-execution-exec-cancelled",
                    image="agency-runtime:rev-1",
                    status="exited",
                    labels={"agency.execution_id": execution.id},
                    finished_at=finished_at,
                    exit_code=WORKER_EXIT_CANCELLED,
                )
            ]
        )
        reconciler = RuntimeReconciler(execution_store=self.execution_store, runtime_container_manager=manager)

        report = await reconciler.reconcile_once()

        current = await self.execution_store.get_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        assert current is not None
        self.assertEqual(current.status, ExecutionStatus.CANCELLED)
        self.assertEqual(current.container_status, "removed")
        self.assertEqual(current.container_exit_code, WORKER_EXIT_CANCELLED)
        self.assertEqual(report.actions[0].action, "execution_container_exited")
        self.assertEqual(report.actions[1].action, "execution_container_removed")
        self.assertEqual(manager.removed, [("container-cancelled", True)])
        event_types = [event.event_type.value for event in events]
        self.assertIn("container.stopped", event_types)
        self.assertIn("execution.cancelled", event_types)

    async def test_reconcile_can_retain_finished_container_for_always_on_execution(self) -> None:
        finished_at = utc_now()
        execution = await self._save_execution(
            execution_id="exec-always-on",
            status=ExecutionStatus.RUNNING,
            container_id="container-always-on",
            container_status="running",
        )
        execution.metadata["execution_lifecycle"] = {
            "run_mode": "always_on",
            "terminate_container_on_completion": False,
        }
        await self.execution_store.update_execution(execution)
        manager = _FakeRuntimeContainerManager(
            [
                RuntimeContainerState(
                    container_id="container-always-on",
                    name="agency-execution-exec-always-on",
                    image="agency-runtime:rev-1",
                    status="exited",
                    labels={"agency.execution_id": execution.id},
                    finished_at=finished_at,
                    exit_code=WORKER_EXIT_SUCCESS,
                )
            ]
        )
        reconciler = RuntimeReconciler(execution_store=self.execution_store, runtime_container_manager=manager)

        report = await reconciler.reconcile_once()

        current = await self.execution_store.get_execution(execution.id)
        assert current is not None
        self.assertEqual(current.status, ExecutionStatus.COMPLETED)
        self.assertEqual(current.container_status, "exited")
        self.assertEqual(manager.removed, [])
        self.assertEqual([action.action for action in report.actions], ["execution_container_exited"])

        second_report = await reconciler.reconcile_once()
        self.assertEqual(manager.removed, [])
        self.assertEqual(
            [action.action for action in second_report.actions],
            ["terminal_execution_container_retained"],
        )

    async def test_reconcile_reaps_terminal_execution_container(self) -> None:
        execution = await self._save_execution(
            execution_id="exec-terminal",
            status=ExecutionStatus.COMPLETED,
            container_id="container-terminal",
            container_status="running",
        )
        manager = _FakeRuntimeContainerManager(
            [
                RuntimeContainerState(
                    container_id="container-terminal",
                    name="agency-execution-exec-terminal",
                    image="agency-runtime:rev-1",
                    status="running",
                    labels={"agency.execution_id": execution.id},
                    started_at=utc_now(),
                )
            ]
        )
        reconciler = RuntimeReconciler(execution_store=self.execution_store, runtime_container_manager=manager)

        report = await reconciler.reconcile_once()

        current = await self.execution_store.get_execution(execution.id)
        events = await self.execution_store.list_events(execution.id)
        assert current is not None
        self.assertEqual(current.container_status, "removed")
        self.assertEqual(manager.stopped, ["container-terminal"])
        self.assertEqual(manager.removed, [("container-terminal", True)])
        self.assertEqual(report.actions[0].action, "terminal_execution_container_reaped")
        self.assertIn("container.stopped", [event.event_type.value for event in events])

    async def test_reconcile_reaps_orphan_managed_container(self) -> None:
        manager = _FakeRuntimeContainerManager(
            [
                RuntimeContainerState(
                    container_id="container-orphan",
                    name="agency-execution-orphan",
                    image="agency-runtime:rev-1",
                    status="running",
                    labels={"agency.execution_id": "missing-execution"},
                    started_at=utc_now(),
                )
            ]
        )
        reconciler = RuntimeReconciler(execution_store=self.execution_store, runtime_container_manager=manager)

        report = await reconciler.reconcile_once()

        self.assertEqual(manager.stopped, ["container-orphan"])
        self.assertEqual(manager.removed, [("container-orphan", True)])
        self.assertEqual(report.actions[0].action, "orphan_container_reaped")

    async def test_reconcile_syncs_live_container_state_without_failing_execution(self) -> None:
        started_at = utc_now()
        execution = await self._save_execution(
            execution_id="exec-sync",
            status=ExecutionStatus.RUNNING,
            container_id="container-sync",
            container_status="created",
        )
        manager = _FakeRuntimeContainerManager(
            [
                RuntimeContainerState(
                    container_id="container-sync",
                    name="agency-execution-exec-sync",
                    image="agency-runtime:rev-1",
                    status="running",
                    labels={"agency.execution_id": execution.id},
                    started_at=started_at,
                )
            ]
        )
        reconciler = RuntimeReconciler(execution_store=self.execution_store, runtime_container_manager=manager)

        report = await reconciler.reconcile_once()

        current = await self.execution_store.get_execution(execution.id)
        assert current is not None
        self.assertEqual(current.status, ExecutionStatus.RUNNING)
        self.assertEqual(current.container_status, "running")
        self.assertEqual(current.container_started_at, started_at)
        self.assertEqual(report.actions, [])
