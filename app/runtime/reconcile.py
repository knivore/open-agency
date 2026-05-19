from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import get_settings
from app.core.time import ensure_utc, utc_now
from app.domain import ExecutionEventType
from app.runtime.containers import ContainerRuntimeError, RuntimeContainerState
from app.runtime.execution_lifecycle import should_terminate_container_on_completion
from app.runtime.lifecycle import RuntimeLifecycleEventEmitter
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState
from app.runtime.operations import RuntimeOperationsRecorder
from app.runtime.worker_protocol import (
    WORKER_EXIT_BOOTSTRAP_FAILED,
    WORKER_EXIT_CANCELLED,
    WORKER_EXIT_INFRA_FAILED,
    WORKER_EXIT_SUCCESS,
    WORKER_EXIT_WORKFLOW_FAILED,
    worker_exit_reason,
)

ACTIVE_EXECUTION_STATUSES = {"queued", "running", "waiting_for_approval", "paused", "cancelling"}
TERMINAL_EXECUTION_STATUSES = {"completed", "failed", "cancelled"}
EXITED_CONTAINER_STATUSES = {"exited", "dead"}
LIVE_CONTAINER_STATUSES = {"created", "running", "restarting", "paused"}


@dataclass(frozen=True, slots=True)
class ReconciliationAction:
    action: str
    execution_id: str | None = None
    container_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    scanned_executions: int
    scanned_containers: int
    actions: list[ReconciliationAction] = field(default_factory=list)


class RuntimeReconciler:
    def __init__(self, *, execution_store, runtime_container_manager,
                 operations: RuntimeOperationsRecorder | None = None):
        self.execution_store = execution_store
        self.runtime_container_manager = runtime_container_manager
        self.operations = operations
        self.emitter = ExecutionEventEmitter(execution_store)
        self.lifecycle_emitter = RuntimeLifecycleEventEmitter(self.emitter)

    async def reconcile_once(self) -> ReconciliationReport:
        executions = await self.execution_store.list_executions()
        containers = self.runtime_container_manager.list_managed_containers(all_containers=True)
        containers_by_id = {container.container_id: container for container in containers}
        executions_by_id = {execution.id: execution for execution in executions}
        actions: list[ReconciliationAction] = []

        for execution in executions:
            if not execution.container_id:
                continue
            container = containers_by_id.get(execution.container_id)
            if execution.status.value in ACTIVE_EXECUTION_STATUSES and container is None:
                await self._mark_execution_failed_for_missing_container(execution)
                actions.append(
                    ReconciliationAction(
                        action="execution_missing_container",
                        execution_id=execution.id,
                        container_id=execution.container_id,
                        detail="Active execution lost its managed container",
                    )
                )
                continue
            if container is None:
                continue
            if execution.status.value in ACTIVE_EXECUTION_STATUSES and container.status in EXITED_CONTAINER_STATUSES:
                await self._mark_execution_failed_for_exited_container(execution, container)
                actions.append(
                    ReconciliationAction(
                        action="execution_container_exited",
                        execution_id=execution.id,
                        container_id=container.container_id,
                        detail=f"Container status={container.status} exit_code={container.exit_code}",
                    )
                )
                if await self._maybe_remove_finished_execution_container(execution, container):
                    actions.append(
                        ReconciliationAction(
                            action="execution_container_removed",
                            execution_id=execution.id,
                            container_id=container.container_id,
                            detail="Execution container removed after terminal worker exit",
                        )
                    )
                continue
            if execution.status.value in TERMINAL_EXECUTION_STATUSES and container.status in LIVE_CONTAINER_STATUSES | EXITED_CONTAINER_STATUSES:
                if should_terminate_container_on_completion(execution):
                    await self._reap_terminal_execution_container(execution, container)
                    actions.append(
                        ReconciliationAction(
                            action="terminal_execution_container_reaped",
                            execution_id=execution.id,
                            container_id=container.container_id,
                            detail=f"Container status={container.status}",
                        )
                    )
                else:
                    await self._sync_execution_container_state(execution, container)
                    actions.append(
                        ReconciliationAction(
                            action="terminal_execution_container_retained",
                            execution_id=execution.id,
                            container_id=container.container_id,
                            detail="Execution lifecycle retained terminal container",
                        )
                    )
                continue
            await self._sync_execution_container_state(execution, container)

        for container in containers:
            execution_id = container.labels.get("agency.execution_id")
            if not execution_id or execution_id in executions_by_id:
                continue
            await self._reap_orphan_container(container)
            actions.append(
                ReconciliationAction(
                    action="orphan_container_reaped",
                    execution_id=execution_id,
                    container_id=container.container_id,
                    detail="Managed container had no matching execution row",
                )
            )

        actions.extend(await self._cleanup_expired_containers(containers))
        actions.extend(await self._cleanup_old_images())

        report = ReconciliationReport(
            scanned_executions=len(executions),
            scanned_containers=len(containers),
            actions=actions,
        )
        if self.operations is not None:
            self.operations.increment("reconcile.runs")
            self.operations.increment("reconcile.actions", len(actions))
            for action in actions:
                self.operations.record_action(
                    action.action,
                    execution_id=action.execution_id,
                    container_id=action.container_id,
                    detail=action.detail,
                )
        return report

    async def reconcile_execution(
            self,
            execution_id: str,
            *,
            known_container: RuntimeContainerState | None = None,
    ) -> ReconciliationReport:
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            return ReconciliationReport(scanned_executions=0, scanned_containers=0, actions=[])
        container = known_container
        if execution.container_id and container is None:
            try:
                container = self.runtime_container_manager.inspect_container(execution.container_id)
            except ContainerRuntimeError:
                container = None
        actions: list[ReconciliationAction] = []
        if execution.container_id:
            if execution.status.value in ACTIVE_EXECUTION_STATUSES and container is None:
                await self._mark_execution_failed_for_missing_container(execution)
                actions.append(
                    ReconciliationAction(
                        action="execution_missing_container",
                        execution_id=execution.id,
                        container_id=execution.container_id,
                        detail="Active execution lost its managed container",
                    )
                )
            elif container is not None and execution.status.value in ACTIVE_EXECUTION_STATUSES and container.status in EXITED_CONTAINER_STATUSES:
                await self._mark_execution_failed_for_exited_container(execution, container)
                actions.append(
                    ReconciliationAction(
                        action="execution_container_exited",
                        execution_id=execution.id,
                        container_id=container.container_id,
                        detail=f"Container status={container.status} exit_code={container.exit_code}",
                    )
                )
                if await self._maybe_remove_finished_execution_container(execution, container):
                    actions.append(
                        ReconciliationAction(
                            action="execution_container_removed",
                            execution_id=execution.id,
                            container_id=container.container_id,
                            detail="Execution container removed after terminal worker exit",
                        )
                    )
            elif container is not None:
                await self._sync_execution_container_state(execution, container)
                actions.append(
                    ReconciliationAction(
                        action="execution_container_synced",
                        execution_id=execution.id,
                        container_id=container.container_id,
                        detail=f"Container status={container.status}",
                    )
                )
        report = ReconciliationReport(
            scanned_executions=1,
            scanned_containers=1 if container is not None else 0,
            actions=actions,
        )
        if self.operations is not None:
            self.operations.increment("reconcile.single_runs")
            for action in actions:
                self.operations.record_action(
                    action.action,
                    execution_id=action.execution_id,
                    container_id=action.container_id,
                    detail=action.detail,
                )
        return report

    async def _mark_execution_failed_for_missing_container(self, execution) -> None:
        state = await self._event_state_for(execution.id, execution.workflow_id)
        execution.status = execution.status.__class__.FAILED
        execution.error = "Managed container is missing"
        execution.completed_at = utc_now()
        execution.container_status = "missing"
        execution.container_ended_at = execution.completed_at
        await self.execution_store.update_execution(execution)
        synthetic = RuntimeContainerState(
            container_id=execution.container_id or "unknown",
            name=execution.container_name or "unknown",
            image=execution.container_image or "unknown",
            status="missing",
            labels={},
            started_at=execution.container_started_at,
            finished_at=execution.container_ended_at,
            exit_code=execution.container_exit_code,
        )
        await self.lifecycle_emitter.emit_container_failed(
            state,
            synthetic,
            runtime_revision_id=execution.runtime_revision_id,
            reason="managed_container_missing",
            extra={"error": execution.error},
        )
        await self.emitter.emit(
            state,
            ExecutionEventType.EXECUTION_FAILED,
            payload={"error": execution.error},
        )

    async def _mark_execution_failed_for_exited_container(self, execution, container: RuntimeContainerState) -> None:
        state = await self._event_state_for(execution.id, execution.workflow_id)
        execution.container_status = container.status
        execution.container_ended_at = container.finished_at or execution.completed_at
        execution.container_exit_code = container.exit_code
        exit_reason = worker_exit_reason(container.exit_code)
        completion_time = execution.container_ended_at or utc_now()
        execution.completed_at = execution.completed_at or completion_time

        if container.exit_code == WORKER_EXIT_SUCCESS:
            execution.status = execution.status.__class__.COMPLETED
            execution.error = None
            await self.execution_store.update_execution(execution)
            await self.lifecycle_emitter.emit_container_stopped(
                state,
                container,
                runtime_revision_id=execution.runtime_revision_id,
                reason=exit_reason,
            )
            await self.emitter.emit(
                state,
                ExecutionEventType.EXECUTION_COMPLETED,
                payload={"reconciled": True, "reason": exit_reason},
            )
            return

        if container.exit_code == WORKER_EXIT_CANCELLED:
            execution.status = execution.status.__class__.CANCELLED
            execution.error = None
            await self.execution_store.update_execution(execution)
            await self.lifecycle_emitter.emit_container_stopped(
                state,
                container,
                runtime_revision_id=execution.runtime_revision_id,
                reason=exit_reason,
            )
            await self.emitter.emit(
                state,
                ExecutionEventType.EXECUTION_CANCELLED,
                payload={"execution_id": execution.id, "reconciled": True, "reason": exit_reason},
            )
            return

        execution.status = execution.status.__class__.FAILED
        if container.exit_code == WORKER_EXIT_WORKFLOW_FAILED:
            execution.error = "Managed container reported workflow failure"
        elif container.exit_code == WORKER_EXIT_BOOTSTRAP_FAILED:
            execution.error = "Managed container worker bootstrap failed"
        elif container.exit_code == WORKER_EXIT_INFRA_FAILED:
            execution.error = "Managed container worker infrastructure failed"
        else:
            execution.error = f"Managed container exited unexpectedly with status '{container.status}'"
        await self.execution_store.update_execution(execution)
        await self.lifecycle_emitter.emit_container_failed(
            state,
            container,
            runtime_revision_id=execution.runtime_revision_id,
            reason=exit_reason,
            extra={"error": execution.error, "exit_reason": exit_reason},
        )
        await self.emitter.emit(
            state,
            ExecutionEventType.EXECUTION_FAILED,
            payload={"error": execution.error, "reconciled": True, "reason": exit_reason},
        )

    async def _reap_terminal_execution_container(self, execution, container: RuntimeContainerState) -> None:
        state = await self._event_state_for(execution.id, execution.workflow_id)
        current = container
        if container.status in LIVE_CONTAINER_STATUSES:
            current = self.runtime_container_manager.stop_container(container.container_id)
        self.runtime_container_manager.remove_container(container.container_id,
                                                        force=current.status in EXITED_CONTAINER_STATUSES)
        execution.container_status = "removed"
        execution.container_ended_at = current.finished_at or execution.container_ended_at or utc_now()
        execution.container_exit_code = current.exit_code
        await self.execution_store.update_execution(execution)
        await self.lifecycle_emitter.emit_container_stopped(
            state,
            current,
            runtime_revision_id=execution.runtime_revision_id,
            reason="terminal_execution_cleanup",
        )

    async def _maybe_remove_finished_execution_container(self, execution, container: RuntimeContainerState) -> bool:
        if container.status not in EXITED_CONTAINER_STATUSES:
            return False
        if not should_terminate_container_on_completion(execution):
            return False
        try:
            self.runtime_container_manager.remove_container(
                container.container_id,
                force=container.status in EXITED_CONTAINER_STATUSES,
            )
        except ContainerRuntimeError:
            return False
        execution.container_status = "removed"
        execution.container_ended_at = container.finished_at or execution.container_ended_at or utc_now()
        execution.container_exit_code = container.exit_code
        await self.execution_store.update_execution(execution)
        return True

    async def _reap_orphan_container(self, container: RuntimeContainerState) -> None:
        current = container
        if container.status in LIVE_CONTAINER_STATUSES:
            current = self.runtime_container_manager.stop_container(container.container_id)
        self.runtime_container_manager.remove_container(container.container_id,
                                                        force=current.status in EXITED_CONTAINER_STATUSES)

    async def _sync_execution_container_state(self, execution, container: RuntimeContainerState) -> None:
        changed = False
        if execution.container_status != container.status:
            execution.container_status = container.status
            changed = True
        if execution.container_started_at != container.started_at and container.started_at is not None:
            execution.container_started_at = container.started_at
            changed = True
        if execution.container_ended_at != container.finished_at and container.finished_at is not None:
            execution.container_ended_at = container.finished_at
            changed = True
        if execution.container_exit_code != container.exit_code and container.exit_code is not None:
            execution.container_exit_code = container.exit_code
            changed = True
        if changed:
            await self.execution_store.update_execution(execution)

    async def _cleanup_expired_containers(self, containers: list[RuntimeContainerState]) -> list[ReconciliationAction]:
        ttl_seconds = get_settings().runtime_container_ttl_seconds
        now = utc_now()
        actions: list[ReconciliationAction] = []
        for container in containers:
            if container.status not in EXITED_CONTAINER_STATUSES or container.finished_at is None:
                continue
            finished_at = ensure_utc(container.finished_at)
            age_seconds = (now - finished_at).total_seconds()
            if age_seconds < ttl_seconds:
                continue
            try:
                self.runtime_container_manager.remove_container(container.container_id, force=True)
            except ContainerRuntimeError:
                continue
            actions.append(
                ReconciliationAction(
                    action="expired_container_reaped",
                    execution_id=container.labels.get("agency.execution_id"),
                    container_id=container.container_id,
                    detail=f"Container older than TTL ({ttl_seconds}s)",
                )
            )
        return actions

    async def _cleanup_old_images(self) -> list[ReconciliationAction]:
        if not hasattr(self.runtime_container_manager, "list_managed_images") or not hasattr(
                self.runtime_container_manager, "remove_image"
        ):
            return []
        retention = get_settings().runtime_image_retention_count
        images = sorted(
            self.runtime_container_manager.list_managed_images(),
            key=lambda item: item.created_at or datetime.min,
            reverse=True,
        )
        if len(images) <= retention:
            return []
        active_image_refs = {
            container.image
            for container in self.runtime_container_manager.list_managed_containers(all_containers=True)
        }
        actions: list[ReconciliationAction] = []
        for image in images[retention:]:
            refs = image.tags or [image.image_id]
            removable_ref = next((ref for ref in refs if ref not in active_image_refs), None)
            if removable_ref is None:
                continue
            try:
                self.runtime_container_manager.remove_image(removable_ref, force=True)
            except ContainerRuntimeError:
                continue
            actions.append(
                ReconciliationAction(
                    action="stale_image_removed",
                    detail=removable_ref,
                )
            )
        return actions

    async def _event_state_for(self, execution_id: str, workflow_id: str) -> NativeExecutionState:
        existing_events = await self.execution_store.list_events(execution_id)
        state = NativeExecutionState(execution_id=execution_id, workflow_id=workflow_id)
        if existing_events:
            last_event = existing_events[-1]
            state.sequence = last_event.sequence
            state.last_event_id = last_event.id
            state.trace_id = last_event.trace_id or state.trace_id
        return state
