from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Optional
from uuid import uuid4

from app.core.config import get_settings
from app.core.time import ensure_utc, utc_now
from app.domain import ExecutionEventType
from app.runtime.containers import ContainerRuntimeError, RuntimeContainerSpec
from app.runtime.lifecycle import RuntimeLifecycleEventEmitter, RuntimeContainerState
from app.runtime.native.approvals import ApprovalManager
from app.runtime.native.errors import ExecutionNotFoundError
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState
from app.runtime.registry import EXECUTION_HOST_DOCKER, EXECUTION_HOST_LOCAL, RuntimeAdapterRegistry

ACTIVE_REPLACEMENT_STATUSES = {"queued", "running", "waiting_for_approval", "paused", "cancelling"}
STALE_REPAIR_STATUSES = {"queued", "running", "paused", "cancelling"}
LIVE_CONTAINER_STATUSES = {"created", "running", "restarting", "paused"}
EXITED_CONTAINER_STATUSES = {"exited", "dead"}
TERMINAL_EXECUTION_STATUSES = {"completed", "failed", "cancelled"}


class ExecutionControlPlane:
    def __init__(
            self,
            *,
            runtime_registry: RuntimeAdapterRegistry,
            execution_store,
            approval_manager: ApprovalManager,
            runtime_revision_service=None,
            runtime_container_manager=None,
            runtime_reconciler=None,
            runtime_operations=None,
            execution_isolation_enabled: Optional[bool] = None,
            cancel_outdated_executions: Optional[bool] = None,
            worker_id: Optional[str] = None,
            stale_after_seconds: int = 30,
    ):
        self.runtime_registry = runtime_registry
        self.execution_store = execution_store
        self.approval_manager = approval_manager
        self.runtime_revision_service = runtime_revision_service
        self.runtime_container_manager = runtime_container_manager
        self.runtime_reconciler = runtime_reconciler
        self.runtime_operations = runtime_operations
        settings = get_settings()
        self.execution_isolation_enabled = settings.execution_isolation_enabled if execution_isolation_enabled is None else execution_isolation_enabled
        self.runtime_revision_shadow_mode = settings.runtime_revision_shadow_mode
        self.cancel_outdated_executions = (
            settings.cancel_outdated_executions if cancel_outdated_executions is None else cancel_outdated_executions
        )
        self.worker_id = worker_id or os.getenv("EXECUTION_WORKER_ID") or f"worker-{uuid4()}"
        self.stale_after_seconds = stale_after_seconds
        self._tasks: dict[str, asyncio.Task] = {}
        self._container_watch_tasks: dict[str, asyncio.Task] = {}
        self.emitter = ExecutionEventEmitter(execution_store)
        self.lifecycle_emitter = RuntimeLifecycleEventEmitter(self.emitter)

    async def queue_start(self, execution_id: str):
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        execution.status = execution.status.__class__.QUEUED
        await self.execution_store.update_execution(execution)
        if execution_id not in self._tasks or self._tasks[execution_id].done():
            self._tasks[execution_id] = asyncio.create_task(self._run_execution(execution_id))
        return execution

    async def _run_execution(self, execution_id: str):
        acquired = await self.execution_store.acquire_lock(
            execution_id,
            self.worker_id,
            stale_after_seconds=self.stale_after_seconds,
        )
        if not acquired:
            return
        try:
            execution = await self.execution_store.get_execution(execution_id)
            if execution is None:
                return
            execution_host = self._execution_host_for(execution)
            if execution_host == EXECUTION_HOST_DOCKER:
                prepared = await self._prepare_isolated_runtime(execution)
                if prepared is None:
                    return
                return
            elif self.runtime_revision_shadow_mode:
                execution = await self._prepare_shadow_runtime(execution)
            execution.status = execution.status.__class__.RUNNING
            execution.worker_id = self.worker_id
            execution.last_heartbeat_at = utc_now()
            await self.execution_store.update_execution(execution)
            await self.runtime_registry.start_execution(execution_id)
        finally:
            await self.execution_store.release_lock(execution_id, self.worker_id)
            self._tasks.pop(execution_id, None)

    def _execution_host_for(self, execution) -> str:
        metadata = execution.metadata if isinstance(execution.metadata, dict) else {}
        requested_host = metadata.get("execution_host")
        if isinstance(requested_host, str):
            normalized_host = requested_host.strip().lower()
            if normalized_host in {EXECUTION_HOST_LOCAL, EXECUTION_HOST_DOCKER}:
                return normalized_host
        return EXECUTION_HOST_DOCKER if self.execution_isolation_enabled else EXECUTION_HOST_LOCAL

    async def pause(self, execution_id: str):
        return await self.runtime_registry.pause_execution(execution_id)

    async def resume(self, execution_id: str):
        execution = await self.runtime_registry.resume_execution(execution_id)
        if execution.status.value in {"running", "queued"}:
            await self.queue_start(execution_id)
        return execution

    async def cancel(self, execution_id: str):
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        if execution.status.value in TERMINAL_EXECUTION_STATUSES or execution.status.value == "cancelling":
            return execution
        execution.status = execution.status.__class__.CANCELLING
        await self.execution_store.update_execution(execution)
        return await self.runtime_registry.cancel_execution(execution_id)

    async def approve(self, execution_id: str, tool_id: str, reason: str | None = None) -> bool:
        return await self.approval_manager.approve(execution_id=execution_id, tool_id=tool_id, reason=reason)

    async def reject(self, execution_id: str, tool_id: str, reason: str | None = None) -> bool:
        return await self.approval_manager.reject(execution_id=execution_id, tool_id=tool_id, reason=reason)

    async def heartbeat(self, execution_id: str):
        return await self.execution_store.heartbeat(execution_id, self.worker_id)

    async def recover_stale_executions(self, *, workflow_id: str | None = None):
        repaired = await self.repair_stale_executions(workflow_id=workflow_id)
        return [item["execution_id"] for item in repaired]

    async def repair_stale_executions(self, *, workflow_id: str | None = None) -> list[dict[str, object]]:
        recovered = []
        active = await self.execution_store.list_active_executions()
        for execution in active:
            if workflow_id is not None and execution.workflow_id != workflow_id:
                continue
            if execution.status.value not in STALE_REPAIR_STATUSES:
                continue
            classification = self._classify_stale_execution(execution)
            if not classification["is_stale"]:
                continue
            previous_status = execution.status.value
            if previous_status == "cancelling":
                repair_action = "marked_cancelled"
                execution.status = execution.status.__class__.CANCELLED
                execution.completed_at = execution.completed_at or utc_now()
                execution.error = execution.error or "Stale cancelling execution was marked cancelled"
                event_type = ExecutionEventType.EXECUTION_CANCELLED
            else:
                repair_action = "requeued"
                execution.status = execution.status.__class__.QUEUED
                event_type = ExecutionEventType.EXECUTION_REPAIRED
            execution.worker_id = None
            execution.last_heartbeat_at = None
            metadata = dict(execution.metadata or {})
            metadata["stale_repair"] = {
                "repaired_at": utc_now().isoformat(),
                "previous_status": previous_status,
                "repair_action": repair_action,
                "stale_after_seconds": self.stale_after_seconds,
                "age_seconds": classification["age_seconds"],
            }
            execution.metadata = metadata
            execution.updated_at = utc_now()
            await self.execution_store.update_execution(execution)
            state = await self._event_state_for(execution.id, execution.workflow_id)
            await self.emitter.emit(
                state,
                event_type,
                payload={
                    "execution_id": execution.id,
                    "previous_status": previous_status,
                    "new_status": execution.status.value,
                    "repair_action": repair_action,
                    "reason": classification["reason"],
                    "stale_classification": classification,
                },
            )
            if self.runtime_operations is not None:
                self.runtime_operations.increment("stale_execution_repairs")
                self.runtime_operations.increment(f"stale_execution_repairs.{repair_action}")
                self.runtime_operations.increment(f"stale_execution_repairs.status.{previous_status}")
                self.runtime_operations.record_action(
                    "stale_execution_repair",
                    execution_id=execution.id,
                    previous_status=previous_status,
                    new_status=execution.status.value,
                    repair_action=repair_action,
                    reason=classification["reason"],
                )
            recovered.append(
                {
                    "execution_id": execution.id,
                    "previous_status": previous_status,
                    "new_status": execution.status.value,
                    "repair_action": repair_action,
                    "reason": classification["reason"],
                }
            )
        return recovered

    def _classify_stale_execution(self, execution) -> dict[str, object]:
        reference = execution.last_heartbeat_at or execution.updated_at or execution.started_at or execution.created_at
        reference_at = ensure_utc(reference)
        age_seconds = max(0, int((utc_now() - reference_at).total_seconds()))
        is_stale = age_seconds > self.stale_after_seconds
        reason = None
        if is_stale:
            reason = (
                f"Execution has status '{execution.status.value}' with no recent heartbeat or update for "
                f"{age_seconds} seconds."
            )
        return {
            "is_stale": is_stale,
            "status": execution.status.value,
            "reference_at": reference_at.isoformat(),
            "age_seconds": age_seconds,
            "stale_after_seconds": self.stale_after_seconds,
            "reason": reason,
        }

    async def _prepare_isolated_runtime(self, execution):
        if self.runtime_revision_service is None or self.runtime_container_manager is None:
            raise RuntimeError("Execution isolation requires runtime revision and container manager services")
        if execution.runtime_adapter_id != "native":
            raise ContainerRuntimeError("Only the native runtime adapter is supported for isolated execution hosting")

        state = await self._event_state_for(execution.id, execution.workflow_id)
        settings = get_settings()
        try:
            revision = await self.runtime_revision_service.resolve_current_revision(
                metadata={
                    "execution_id": execution.id,
                    "workflow_id": execution.workflow_id,
                    "requested_adapter": execution.runtime_adapter_id,
                }
            )
            execution.runtime_revision_id = revision.id
            execution.runtime_fingerprint = revision.fingerprint
            await self.execution_store.update_execution(execution)
            await self.lifecycle_emitter.emit_runtime_revision_resolved(state, revision)
            invalidated = await self.runtime_revision_service.invalidate_superseded_revisions(
                revision.id,
                reason=f"superseded_by:{revision.id}",
            )
            for invalidated_revision in invalidated:
                await self.lifecycle_emitter.emit_runtime_revision_invalidated(
                    state,
                    invalidated_revision,
                    reason=invalidated_revision.invalidation_reason,
                )
            execution = await self._handle_outdated_executions(execution, revision.id)

            image = (
                f"{revision.image_name}:{revision.image_tag}"
                if revision.image_name and revision.image_tag
                else self.runtime_container_manager.config.runtime_base_image
            )
            worker_codex_cwd = os.getenv("EXECUTION_CODEX_CLI_CWD")
            if not worker_codex_cwd:
                if os.getenv("AGENCY_BACKEND_RUN_MODE") == "host":
                    worker_codex_cwd = settings.execution_container_workdir
                else:
                    worker_codex_cwd = os.getenv("CODEX_CLI_CWD", settings.execution_container_workdir)
            container_spec = RuntimeContainerSpec(
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                runtime_revision_id=revision.id,
                image=image,
                command=["python", "-m", "app.runtime.worker"],
                env={
                    "AGENCY_EXECUTION_ID": execution.id,
                    "AGENCY_WORKFLOW_ID": execution.workflow_id,
                    "AGENCY_RUNTIME_REVISION_ID": revision.id,
                    "AGENCY_RUNTIME_ADAPTER_ID": execution.runtime_adapter_id,
                    "AGENCY_WORKER_ID": f"container-worker-{execution.id}",
                    "AGENCY_HEARTBEAT_INTERVAL_SECONDS": "1.0",
                    "APP_ENV": settings.app_env,
                    "CODEX_HOME": os.getenv("CODEX_HOME", "/codex"),
                    "CODEX_CLI_CWD": worker_codex_cwd,
                    "CODEX_CLI_SANDBOX": os.getenv("CODEX_CLI_SANDBOX", "danger-full-access"),
                    **({"DATABASE_URL": settings.container_database_url} if settings.container_database_url else {}),
                },
            )
            created = self.runtime_container_manager.create_execution_container(container_spec)
            execution.container_id = created.container_id
            execution.container_name = created.name
            execution.container_image = created.image
            execution.container_status = created.status
            await self.execution_store.update_execution(execution)
            await self.lifecycle_emitter.emit_container_created(
                state,
                created,
                runtime_revision_id=revision.id,
            )

            started = self.runtime_container_manager.start_container(created.container_id)
            execution.container_status = started.status
            execution.container_started_at = started.started_at or utc_now()
            execution.container_ended_at = started.finished_at
            execution.container_exit_code = started.exit_code
            await self.execution_store.update_execution(execution)
            await self.lifecycle_emitter.emit_container_started(
                state,
                started,
                runtime_revision_id=revision.id,
            )
            execution.container_status = started.status
            await self.execution_store.update_execution(execution)
            self._start_container_watch(execution.id, started.container_id)
            return execution
        except ContainerRuntimeError as exc:
            execution.status = execution.status.__class__.FAILED
            execution.error = str(exc)
            execution.completed_at = utc_now()
            await self.execution_store.update_execution(execution)
            failed_container = RuntimeContainerState(
                container_id=execution.container_id or "unknown",
                name=execution.container_name or "unknown",
                image=execution.container_image or "unknown",
                status=execution.container_status or "failed",
                labels={},
                started_at=execution.container_started_at,
                finished_at=execution.container_ended_at,
                exit_code=execution.container_exit_code,
            )
            await self.lifecycle_emitter.emit_container_failed(
                state,
                failed_container,
                runtime_revision_id=execution.runtime_revision_id,
                reason=str(exc),
                extra={"error": str(exc)},
            )
            await self.emitter.emit(
                state,
                ExecutionEventType.EXECUTION_FAILED,
                payload={"error": str(exc)},
            )
            return None

    def _start_container_watch(self, execution_id: str, container_id: str) -> None:
        existing = self._container_watch_tasks.get(execution_id)
        if existing is not None and not existing.done():
            existing.cancel()
        self._container_watch_tasks[execution_id] = asyncio.create_task(
            self._watch_container_until_exit(execution_id, container_id)
        )

    async def _watch_container_until_exit(self, execution_id: str, container_id: str) -> None:
        if self.runtime_container_manager is None or self.runtime_reconciler is None:
            return
        try:
            state = await asyncio.to_thread(
                self.runtime_container_manager.wait_for_container_exit,
                container_id,
                timeout_seconds=max(get_settings().runtime_container_ttl_seconds, 60),
                poll_interval_seconds=1.0,
            )
            if self.runtime_operations is not None:
                self.runtime_operations.increment("container_watch.completed")
                self.runtime_operations.record_action(
                    "container_watch_exit",
                    execution_id=execution_id,
                    container_id=container_id,
                    status=state.status,
                    exit_code=state.exit_code,
                )
            await self.runtime_reconciler.reconcile_execution(execution_id, known_container=state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.runtime_operations is not None:
                self.runtime_operations.increment("container_watch.failed")
                self.runtime_operations.record_action(
                    "container_watch_failed",
                    execution_id=execution_id,
                    container_id=container_id,
                    detail=str(exc),
                )
        finally:
            self._container_watch_tasks.pop(execution_id, None)

    async def _prepare_shadow_runtime(self, execution):
        if self.runtime_revision_service is None:
            raise RuntimeError("Runtime revision shadow mode requires a runtime revision service")

        state = await self._event_state_for(execution.id, execution.workflow_id)
        revision = await self.runtime_revision_service.resolve_current_revision(
            metadata={
                "execution_id": execution.id,
                "workflow_id": execution.workflow_id,
                "requested_adapter": execution.runtime_adapter_id,
                "shadow_mode": True,
            }
        )
        execution.runtime_revision_id = revision.id
        execution.runtime_fingerprint = revision.fingerprint
        await self.execution_store.update_execution(execution)
        await self.lifecycle_emitter.emit_runtime_revision_resolved(state, revision)
        return execution

    async def _handle_outdated_executions(self, execution, runtime_revision_id: str):
        if execution.replacement_of_execution_id:
            return execution
        active = await self.execution_store.list_active_executions()
        outdated = [
            candidate
            for candidate in active
            if candidate.id != execution.id
               and candidate.workflow_id == execution.workflow_id
               and candidate.status.value in ACTIVE_REPLACEMENT_STATUSES
               and candidate.runtime_revision_id
               and candidate.runtime_revision_id != runtime_revision_id
        ]
        if not outdated:
            return execution

        replacement_target = outdated[0]
        changed = False
        if execution.replacement_of_execution_id != replacement_target.id:
            execution.replacement_of_execution_id = replacement_target.id
            changed = True
        if execution.restart_reason != "runtime_revision_superseded":
            execution.restart_reason = "runtime_revision_superseded"
            changed = True
        if changed:
            execution = await self.execution_store.update_execution(execution)

        if not self.cancel_outdated_executions:
            return execution

        for candidate in outdated:
            await self._replace_outdated_execution(
                candidate,
                replacement_execution=execution,
                runtime_revision_id=runtime_revision_id,
                reason="runtime_revision_superseded",
            )
        return execution

    async def replace_active_executions_for_workflow_revision(
            self,
            *,
            workflow_id: str,
            previous_revision: int | None,
            replacement_revision: int,
            source: str = "workflow_revision_change",
    ) -> list[str]:
        active = await self.execution_store.list_active_executions()
        replaced: list[str] = []
        candidates = [
            execution
            for execution in active
            if execution.workflow_id == workflow_id
               and execution.status.value in ACTIVE_REPLACEMENT_STATUSES
               and execution.restart_reason != "workflow_revision_superseded"
        ]
        for execution in candidates:
            replacement = await self.runtime_registry.create_execution(
                execution.workflow_id,
                execution.input_payload,
                {
                    "type": "workflow_revision_superseded",
                    "source": source,
                    "created_by": "workflow-update",
                    "replaces_execution_id": execution.id,
                    "previous_workflow_revision": previous_revision,
                    "replacement_workflow_revision": replacement_revision,
                },
                runtime_adapter_id=execution.runtime_adapter_id,
            )
            replacement.replacement_of_execution_id = execution.id
            replacement.restart_reason = "workflow_revision_superseded"
            await self.execution_store.update_execution(replacement)
            await self.queue_start(replacement.id)
            await self._replace_outdated_execution(
                execution,
                replacement_execution=replacement,
                runtime_revision_id=execution.runtime_revision_id or replacement.runtime_revision_id or "",
                reason="workflow_revision_superseded",
                extra={
                    "replacement_workflow_revision": replacement_revision,
                    "previous_workflow_revision": previous_revision,
                },
            )
            replaced.append(replacement.id)
        return replaced

    async def _replace_outdated_execution(
            self,
            execution,
            *,
            replacement_execution,
            runtime_revision_id: str,
            reason: str,
            extra: dict | None = None,
    ) -> None:
        state = await self._event_state_for(execution.id, execution.workflow_id)
        execution.restart_reason = reason
        try:
            execution = await self.runtime_registry.cancel_execution(execution.id)
        except Exception:
            execution.status = execution.status.__class__.CANCELLED
            execution.completed_at = execution.completed_at or utc_now()
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(
                state,
                ExecutionEventType.EXECUTION_CANCELLED,
                payload={
                    "execution_id": execution.id,
                    "reason": reason,
                    "replacement_execution_id": replacement_execution.id,
                },
            )

        execution.restart_reason = reason
        if not execution.container_id:
            await self.execution_store.update_execution(execution)
            return
        container = self.runtime_container_manager.inspect_container(execution.container_id)
        if container.status in LIVE_CONTAINER_STATUSES:
            container = self.runtime_container_manager.stop_container(execution.container_id)
        self.runtime_container_manager.remove_container(
            execution.container_id,
            force=container.status in EXITED_CONTAINER_STATUSES,
        )
        execution.container_status = "removed"
        execution.container_ended_at = container.finished_at or execution.container_ended_at or utc_now()
        execution.container_exit_code = container.exit_code
        await self.execution_store.update_execution(execution)
        await self.lifecycle_emitter.emit_container_replaced(
            state,
            container,
            runtime_revision_id=execution.runtime_revision_id,
            reason=reason,
            extra={
                "replacement_execution_id": replacement_execution.id,
                "replacement_runtime_revision_id": runtime_revision_id,
                **(extra or {}),
            },
        )

    async def _event_state_for(self, execution_id: str, workflow_id: str) -> NativeExecutionState:
        existing_events = await self.execution_store.list_events(execution_id)
        state = NativeExecutionState(execution_id=execution_id, workflow_id=workflow_id)
        if existing_events:
            last_event = existing_events[-1]
            state.sequence = last_event.sequence
            state.last_event_id = last_event.id
            state.trace_id = last_event.trace_id or state.trace_id
        return state
