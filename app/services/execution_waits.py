"""Lifecycle service for durable execution suspension and idempotent wakeup."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.api.context import ApiContext
from app.core.time import ensure_utc, utc_now
from app.domain import (
    ExecutionEventType,
    ExecutionStatus,
    ExecutionWait,
    ExecutionWaitKind,
    ExecutionWaitStatus,
    RuntimeAdapterType,
    TERMINAL_EXECUTION_WAIT_STATUSES,
)
from app.runtime.native.errors import ExecutionNotFoundError
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState


WAIT_EXECUTION_STATUS = {
    ExecutionWaitKind.INPUT: ExecutionStatus.WAITING_FOR_INPUT,
    ExecutionWaitKind.APPROVAL: ExecutionStatus.WAITING_FOR_APPROVAL,
    ExecutionWaitKind.EVENT: ExecutionStatus.WAITING_FOR_EVENT,
    ExecutionWaitKind.SLEEP: ExecutionStatus.SLEEPING,
}

WAIT_CREATION_SOURCE_STATUSES = {
    ExecutionStatus.PAUSED,
    ExecutionStatus.WAITING_FOR_INPUT,
    ExecutionStatus.WAITING_FOR_APPROVAL,
    ExecutionStatus.WAITING_FOR_EVENT,
    ExecutionStatus.SLEEPING,
}


class ExecutionWaitNotFoundError(ValueError):
    pass


class ExecutionWaitConflictError(ValueError):
    pass


@dataclass(slots=True)
class ExecutionWaitService:
    context: ApiContext
    emitter: ExecutionEventEmitter = field(init=False)

    def __post_init__(self) -> None:
        self.emitter = ExecutionEventEmitter(self.context.execution_store)

    async def create_wait(
            self,
            *,
            execution_id: str,
            kind: ExecutionWaitKind,
            idempotency_key: str,
            checkpoint: dict[str, Any] | None = None,
            request_payload: dict[str, Any] | None = None,
            policy: dict[str, Any] | None = None,
            correlation_key: str | None = None,
            wake_at: datetime | None = None,
            deadline_at: datetime | None = None,
            metadata: dict[str, Any] | None = None,
    ) -> tuple[ExecutionWait, bool]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        if execution.status not in WAIT_CREATION_SOURCE_STATUSES:
            raise ExecutionWaitConflictError(
                "Execution must be paused at a persisted checkpoint before a durable wait can be created."
            )
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise ValueError("idempotency_key must not be empty")
        now = utc_now()
        if wake_at is not None and deadline_at is not None and ensure_utc(deadline_at) < ensure_utc(wake_at):
            raise ValueError("deadline_at cannot be earlier than wake_at")

        pending = await self.context.execution_store.list_execution_waits(
            execution_id,
            status=ExecutionWaitStatus.PENDING,
        )
        if pending:
            existing = pending[0]
            if existing.idempotency_key == normalized_key:
                return existing, False
            raise ExecutionWaitConflictError(
                f"Execution '{execution_id}' already has pending wait '{existing.id}'."
            )

        durable_checkpoint = deepcopy(checkpoint)
        if durable_checkpoint is None:
            durable_checkpoint = deepcopy(execution.output_payload) if isinstance(execution.output_payload, dict) else {}
        wait = ExecutionWait(
            execution_id=execution_id,
            kind=kind,
            idempotency_key=normalized_key,
            correlation_key=correlation_key.strip() if isinstance(correlation_key, str) and correlation_key.strip() else None,
            checkpoint=durable_checkpoint,
            request_payload=deepcopy(request_payload or {}),
            policy=deepcopy(policy or {}),
            wake_at=wake_at,
            deadline_at=deadline_at,
            metadata=deepcopy(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        try:
            saved = await self.context.execution_store.create_execution_wait(wait)
        except (IntegrityError, ValueError) as exc:
            pending = await self.context.execution_store.list_execution_waits(
                execution_id,
                status=ExecutionWaitStatus.PENDING,
            )
            if pending and pending[0].idempotency_key == normalized_key:
                return pending[0], False
            raise ExecutionWaitConflictError(
                f"Execution '{execution_id}' acquired another pending wait concurrently."
            ) from exc

        execution.status = WAIT_EXECUTION_STATUS[kind]
        execution.metadata = {
            **(execution.metadata or {}),
            "active_wait": {
                "wait_id": saved.id,
                "kind": saved.kind.value,
                "created_at": saved.created_at.isoformat(),
                "wake_at": saved.wake_at.isoformat() if saved.wake_at else None,
                "deadline_at": saved.deadline_at.isoformat() if saved.deadline_at else None,
            },
        }
        await self.context.execution_store.update_execution(execution)
        await self._emit(
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            event_type=ExecutionEventType.EXECUTION_WAITING,
            payload={
                "wait_id": saved.id,
                "kind": saved.kind.value,
                "status": saved.status.value,
                "correlation_key": saved.correlation_key,
                "wake_at": saved.wake_at.isoformat() if saved.wake_at else None,
                "deadline_at": saved.deadline_at.isoformat() if saved.deadline_at else None,
            },
        )
        return saved, True

    async def get_wait(self, execution_id: str, wait_id: str) -> ExecutionWait:
        wait = await self.context.execution_store.get_execution_wait(wait_id)
        if wait is None or wait.execution_id != execution_id:
            raise ExecutionWaitNotFoundError(f"Execution wait '{wait_id}' was not found")
        return wait

    async def list_waits(
            self,
            execution_id: str,
            *,
            status: ExecutionWaitStatus | None = None,
    ) -> list[ExecutionWait]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        return await self.context.execution_store.list_execution_waits(execution_id, status=status)

    async def resolve_wait(
            self,
            *,
            execution_id: str,
            wait_id: str,
            resolution_key: str,
            resolution_payload: dict[str, Any] | None = None,
            status: ExecutionWaitStatus = ExecutionWaitStatus.RESOLVED,
            resolved_by: str | None = None,
            resume: bool = True,
    ) -> dict[str, Any]:
        if status not in TERMINAL_EXECUTION_WAIT_STATUSES:
            raise ValueError("Execution waits may only resolve to resolved, expired, or cancelled.")
        normalized_key = resolution_key.strip()
        if not normalized_key:
            raise ValueError("resolution_key must not be empty")
        wait = await self.get_wait(execution_id, wait_id)
        execution = None
        if wait.status == ExecutionWaitStatus.PENDING:
            execution = await self.context.execution_store.get_execution(execution_id)
            if execution is None:
                raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
            expected_status = WAIT_EXECUTION_STATUS[wait.kind]
            active_wait = (execution.metadata or {}).get("active_wait")
            if execution.status != expected_status or not (
                    isinstance(active_wait, dict) and active_wait.get("wait_id") == wait.id
            ):
                # A pending wait must never pause an execution that an operator
                # has already cancelled, resumed, or otherwise redirected.
                raise ExecutionWaitConflictError(
                    f"Execution '{execution_id}' is no longer suspended on wait '{wait.id}'."
                )
            if (
                    resume
                    and status == ExecutionWaitStatus.RESOLVED
                    and execution.runtime_adapter_id != RuntimeAdapterType.NATIVE.value
            ):
                raise ExecutionWaitConflictError("Automatic durable wait resume currently requires the native runtime.")
        resolved, claimed = await self.context.execution_store.resolve_execution_wait(
            wait.id,
            status=status,
            resolution_key=normalized_key,
            resolution_payload=deepcopy(resolution_payload or {}),
            resolved_by=resolved_by,
        )
        if resolved is None:
            current = await self.get_wait(execution_id, wait_id)
            raise ExecutionWaitConflictError(
                f"Execution wait '{wait_id}' was already resolved by key '{current.resolution_key}'."
            )
        if not claimed:
            return {"wait": resolved, "claimed": False, "resumed": False, "execution": None}

        execution = execution or await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        metadata = dict(execution.metadata or {})
        active_wait = metadata.get("active_wait")
        if isinstance(active_wait, dict) and active_wait.get("wait_id") == wait_id:
            metadata.pop("active_wait", None)
        metadata["last_resolved_wait"] = {
            "wait_id": resolved.id,
            "kind": resolved.kind.value,
            "status": resolved.status.value,
            "resolution_key": resolved.resolution_key,
            "resolved_at": resolved.resolved_at.isoformat() if resolved.resolved_at else None,
        }
        execution.metadata = metadata
        wait_resolutions = dict((execution.input_payload or {}).get("wait_resolutions") or {})
        wait_resolutions[resolved.id] = {
            "kind": resolved.kind.value,
            "status": resolved.status.value,
            "payload": deepcopy(resolved.resolution_payload or {}),
            "resolved_at": resolved.resolved_at.isoformat() if resolved.resolved_at else None,
        }
        execution.input_payload = {**(execution.input_payload or {}), "wait_resolutions": wait_resolutions}
        execution.status = ExecutionStatus.PAUSED
        await self.context.execution_store.update_execution(execution)
        await self._emit(
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            event_type=ExecutionEventType.EXECUTION_WOKEN,
            payload={
                "wait_id": resolved.id,
                "kind": resolved.kind.value,
                "status": resolved.status.value,
                "resolution_key": resolved.resolution_key,
                "resume_requested": resume,
            },
        )

        resumed_execution = None
        should_resume = resume and status == ExecutionWaitStatus.RESOLVED
        if should_resume:
            resumed_execution = await self.context.control_plane.resume(execution.id)
        return {
            "wait": resolved,
            "claimed": True,
            "resumed": resumed_execution is not None,
            "execution": resumed_execution,
        }

    async def wake_due_waits(self, *, now: datetime | None = None, limit: int = 100) -> dict[str, Any]:
        current_time = now or utc_now()
        expired = await self.context.execution_store.list_expired_execution_waits(
            before=current_time,
            limit=limit,
        )
        outcomes: list[dict[str, Any]] = []
        for wait in expired:
            try:
                outcome = await self.resolve_wait(
                    execution_id=wait.execution_id,
                    wait_id=wait.id,
                    resolution_key=f"deadline:{wait.id}:{wait.deadline_at.isoformat() if wait.deadline_at else 'expired'}",
                    resolution_payload={
                        "deadline_at": wait.deadline_at.isoformat() if wait.deadline_at else None,
                        "reason": "Execution wait deadline expired.",
                    },
                    status=ExecutionWaitStatus.EXPIRED,
                    resolved_by="runtime_reconciler",
                    resume=False,
                )
                outcomes.append({
                    "wait_id": wait.id,
                    "execution_id": wait.execution_id,
                    "claimed": outcome["claimed"],
                    "resumed": False,
                    "status": ExecutionWaitStatus.EXPIRED.value,
                })
            except Exception as exc:
                outcomes.append({
                    "wait_id": wait.id,
                    "execution_id": wait.execution_id,
                    "error": str(exc),
                })
        due = await self.context.execution_store.list_due_execution_waits(before=current_time, limit=limit)
        for wait in due:
            try:
                outcome = await self.resolve_wait(
                    execution_id=wait.execution_id,
                    wait_id=wait.id,
                    resolution_key=f"timer:{wait.id}:{wait.wake_at.isoformat() if wait.wake_at else 'due'}",
                    resolution_payload={"wake_at": wait.wake_at.isoformat() if wait.wake_at else None},
                    resolved_by="runtime_reconciler",
                    resume=True,
                )
                outcomes.append({
                    "wait_id": wait.id,
                    "execution_id": wait.execution_id,
                    "claimed": outcome["claimed"],
                    "resumed": outcome["resumed"],
                    "status": ExecutionWaitStatus.RESOLVED.value,
                })
            except Exception as exc:
                outcomes.append({
                    "wait_id": wait.id,
                    "execution_id": wait.execution_id,
                    "error": str(exc),
                })
        return {
            "scanned": len(expired) + len(due),
            "expired_scanned": len(expired),
            "due_scanned": len(due),
            "items": outcomes,
        }

    async def wake_event(
            self,
            *,
            correlation_key: str,
            event_id: str,
            event_payload: dict[str, Any] | None = None,
            resolved_by: str | None = None,
            limit: int = 100,
    ) -> dict[str, Any]:
        normalized_correlation = correlation_key.strip()
        normalized_event_id = event_id.strip()
        if not normalized_correlation:
            raise ValueError("correlation_key must not be empty")
        if not normalized_event_id:
            raise ValueError("event_id must not be empty")
        candidates = await self.context.execution_store.list_pending_execution_waits_by_correlation(
            normalized_correlation,
            limit=limit,
        )
        event_waits = [wait for wait in candidates if wait.kind == ExecutionWaitKind.EVENT]
        outcomes: list[dict[str, Any]] = []
        for wait in event_waits:
            try:
                outcome = await self.resolve_wait(
                    execution_id=wait.execution_id,
                    wait_id=wait.id,
                    resolution_key=f"event:{normalized_event_id}:{wait.id}",
                    resolution_payload={
                        "event_id": normalized_event_id,
                        "correlation_key": normalized_correlation,
                        "payload": deepcopy(event_payload or {}),
                    },
                    resolved_by=resolved_by or "event_trigger",
                    resume=True,
                )
                outcomes.append({
                    "wait_id": wait.id,
                    "execution_id": wait.execution_id,
                    "claimed": outcome["claimed"],
                    "resumed": outcome["resumed"],
                })
            except Exception as exc:
                outcomes.append({
                    "wait_id": wait.id,
                    "execution_id": wait.execution_id,
                    "error": str(exc),
                })
        return {"correlation_key": normalized_correlation, "matched": len(event_waits), "items": outcomes}

    async def _emit(
            self,
            *,
            execution_id: str,
            workflow_id: str,
            event_type: ExecutionEventType,
            payload: dict[str, Any],
    ) -> None:
        state = NativeExecutionState(execution_id=execution_id, workflow_id=workflow_id)
        events = await self.context.execution_store.list_events(execution_id)
        if events:
            state.sequence = events[-1].sequence
            state.last_event_id = events[-1].id
            state.trace_id = events[-1].trace_id or state.trace_id
        await self.emitter.emit(state, event_type, payload=payload)
