"""Isolated worker process entrypoint for Docker-backed executions.

Workers bootstrap an API context from environment variables, claim one
execution, emit heartbeats, run the selected adapter, and translate failures
into stable worker exit codes for the reconciler.
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from typing import TYPE_CHECKING, Awaitable

from app.core.time import utc_now
from app.domain import ExecutionEventType, ExecutionStatus
from app.runtime.native.errors import ExecutionNotFoundError
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState
from app.runtime.worker_protocol import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    WORKER_EXIT_BOOTSTRAP_FAILED,
    WORKER_EXIT_CANCELLED,
    WORKER_EXIT_INFRA_FAILED,
    WORKER_EXIT_SUCCESS,
    WORKER_EXIT_SUSPENDED,
    WORKER_EXIT_WORKFLOW_FAILED,
)

if TYPE_CHECKING:
    from app.api.context import ApiContext

logger = logging.getLogger(__name__)


def worker_id_for_execution(execution_id: str) -> str:
    return f"container-worker-{execution_id}"


def load_worker_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    source = env or os.environ
    execution_id = source.get("AGENCY_EXECUTION_ID")
    workflow_id = source.get("AGENCY_WORKFLOW_ID")
    goal_id = source.get("AGENCY_GOAL_ID") or None
    runtime_revision_id = source.get("AGENCY_RUNTIME_REVISION_ID")
    runtime_adapter_id = source.get("AGENCY_RUNTIME_ADAPTER_ID")
    if not execution_id:
        raise RuntimeError("AGENCY_EXECUTION_ID is required")
    if not workflow_id:
        raise RuntimeError("AGENCY_WORKFLOW_ID is required")
    if not runtime_revision_id:
        raise RuntimeError("AGENCY_RUNTIME_REVISION_ID is required")
    if not runtime_adapter_id:
        raise RuntimeError("AGENCY_RUNTIME_ADAPTER_ID is required")
    return {
        "execution_id": execution_id,
        "workflow_id": workflow_id,
        "goal_id": goal_id,
        "runtime_revision_id": runtime_revision_id,
        "runtime_adapter_id": runtime_adapter_id,
        "worker_id": source.get("AGENCY_WORKER_ID") or worker_id_for_execution(execution_id),
        "heartbeat_interval_seconds": float(
            source.get("AGENCY_HEARTBEAT_INTERVAL_SECONDS", str(DEFAULT_HEARTBEAT_INTERVAL_SECONDS))
        ),
        "execution_timeout_seconds": (
            float(source["AGENCY_EXECUTION_TIMEOUT_SECONDS"])
            if source.get("AGENCY_EXECUTION_TIMEOUT_SECONDS")
            else None
        ),
    }


async def run_execution_worker(
        *,
        context: "ApiContext",
        execution_id: str,
        workflow_id: str,
        runtime_revision_id: str,
        runtime_adapter_id: str,
        worker_id: str,
        goal_id: str | None = None,
        lock_retry_seconds: float = 5.0,
        lock_retry_interval_seconds: float = 0.2,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        execution_timeout_seconds: float | None = None,
) -> int:
    if runtime_adapter_id != "native":
        execution = await context.execution_store.get_execution(execution_id)
        if execution is not None:
            await _mark_worker_failure(
                context,
                execution,
                f"Isolated worker does not support runtime adapter '{runtime_adapter_id}'",
            )
        return WORKER_EXIT_BOOTSTRAP_FAILED

    deadline = asyncio.get_running_loop().time() + lock_retry_seconds
    acquired = False
    while asyncio.get_running_loop().time() < deadline:
        acquired = await context.execution_store.acquire_lock(execution_id, worker_id, stale_after_seconds=1)
        if acquired:
            break
        await asyncio.sleep(lock_retry_interval_seconds)

    if not acquired:
        execution = await context.execution_store.get_execution(execution_id)
        if execution is not None:
            await _mark_worker_failure(context, execution, "Timed out acquiring execution lock inside worker")
        return WORKER_EXIT_BOOTSTRAP_FAILED

    # Record an initial heartbeat as soon as the isolated worker owns the lock so
    # stale detection can distinguish a live startup from a dead container.
    await context.execution_store.heartbeat(execution_id, worker_id)
    stop_heartbeat = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(
            context=context,
            execution_id=execution_id,
            worker_id=worker_id,
            interval_seconds=heartbeat_interval_seconds,
            stop_signal=stop_heartbeat,
        )
    )
    try:
        execution = await context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        execution.runtime_revision_id = runtime_revision_id
        execution.runtime_fingerprint = execution.runtime_fingerprint
        worker_context = dict(execution.metadata.get("worker_context") or {})
        worker_context.update(
            {
                "execution_id": execution_id,
                "workflow_id": workflow_id,
                "goal_id": goal_id or execution.goal_id,
                "runtime_revision_id": runtime_revision_id,
                "runtime_adapter_id": runtime_adapter_id,
                "worker_id": worker_id,
            }
        )
        execution.metadata["worker_context"] = worker_context
        await context.execution_store.update_execution(execution)
        execution_coro = context.runtime_registry.start_execution(execution_id)
        if execution_timeout_seconds is not None:
            result = await _run_with_active_execution_timeout(
                context=context,
                execution_id=execution_id,
                execution_coro=execution_coro,
                timeout_seconds=execution_timeout_seconds,
            )
        else:
            result = await execution_coro
        return _exit_code_for_execution(result)
    except asyncio.TimeoutError:
        execution = await context.execution_store.get_execution(execution_id)
        if execution is not None:
            await _mark_worker_failure(
                context,
                execution,
                f"Execution worker timed out after {execution_timeout_seconds} seconds",
            )
        return WORKER_EXIT_INFRA_FAILED
    except ExecutionNotFoundError:
        return WORKER_EXIT_BOOTSTRAP_FAILED
    except Exception as exc:
        logger.exception("Execution worker failed for execution '%s'", execution_id)
        execution = await context.execution_store.get_execution(execution_id)
        traceback_text = traceback.format_exc(limit=20)
        if execution is not None:
            execution.metadata.setdefault("runtime_diagnostics", {})["worker_exception"] = {
                "error_type": exc.__class__.__name__,
                "message": str(exc),
                "traceback": traceback_text.splitlines()[-20:],
            }
            await _mark_worker_failure(
                context,
                execution,
                f"Execution worker failed before completion: {exc.__class__.__name__}: {exc}",
            )
        traceback.print_exc()
        return WORKER_EXIT_INFRA_FAILED
    finally:
        stop_heartbeat.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        await context.execution_store.release_lock(execution_id, worker_id)


async def _run_with_active_execution_timeout(
        *,
        context: "ApiContext",
        execution_id: str,
        execution_coro: Awaitable,
        timeout_seconds: float,
):
    """Enforce worker compute time without charging time spent waiting on a human."""
    task = asyncio.ensure_future(execution_coro)
    loop = asyncio.get_running_loop()
    remaining_seconds = timeout_seconds
    # Let the execution coroutine publish an immediate approval wait before the
    # first timeout interval is classified as active work.
    await asyncio.sleep(0)
    current = await context.execution_store.get_execution(execution_id)
    waiting_for_approval = current is not None and current.status == ExecutionStatus.WAITING_FOR_APPROVAL
    last_checked_at = loop.time()
    poll_interval_seconds = min(0.1, max(0.01, timeout_seconds / 4))
    try:
        while True:
            wait_seconds = poll_interval_seconds if waiting_for_approval else min(
                poll_interval_seconds,
                max(remaining_seconds, 0),
            )
            done, _ = await asyncio.wait({task}, timeout=wait_seconds)
            checked_at = loop.time()
            if not waiting_for_approval:
                remaining_seconds -= checked_at - last_checked_at
            last_checked_at = checked_at
            if done:
                return await task

            current = await context.execution_store.get_execution(execution_id)
            waiting_for_approval = current is not None and current.status == ExecutionStatus.WAITING_FOR_APPROVAL
            if remaining_seconds <= 0 and not waiting_for_approval:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise asyncio.TimeoutError
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _exit_code_for_execution(execution) -> int:
    if execution.status == ExecutionStatus.COMPLETED:
        return WORKER_EXIT_SUCCESS
    if execution.status == ExecutionStatus.CANCELLED:
        return WORKER_EXIT_CANCELLED
    if execution.status == ExecutionStatus.FAILED:
        return WORKER_EXIT_WORKFLOW_FAILED
    if execution.status in {
        ExecutionStatus.WAITING_FOR_INPUT,
        ExecutionStatus.WAITING_FOR_APPROVAL,
        ExecutionStatus.WAITING_FOR_EVENT,
        ExecutionStatus.SLEEPING,
        ExecutionStatus.PAUSED,
    }:
        # Durable suspension is a normal worker handoff. The next wake creates
        # a fresh worker and resumes from the persisted checkpoint.
        return WORKER_EXIT_SUSPENDED
    return WORKER_EXIT_INFRA_FAILED


async def _heartbeat_loop(
        *,
        context: "ApiContext",
        execution_id: str,
        worker_id: str,
        interval_seconds: float,
        stop_signal: asyncio.Event,
) -> None:
    while not stop_signal.is_set():
        await asyncio.sleep(interval_seconds)
        if stop_signal.is_set():
            break
        await context.execution_store.heartbeat(execution_id, worker_id)


async def _mark_worker_failure(context: ApiContext, execution, error: str) -> None:
    execution.status = execution.status.__class__.FAILED
    execution.error = error
    execution.completed_at = utc_now()
    diagnostics = execution.metadata.setdefault("runtime_diagnostics", {})
    diagnostics["worker_failure"] = {
        "error": error,
        "worker_id": execution.worker_id,
        "runtime_revision_id": execution.runtime_revision_id,
    }
    await context.execution_store.update_execution(execution)
    emitter = ExecutionEventEmitter(context.execution_store)
    state = await _event_state_for(context, execution.id, execution.workflow_id)
    await emitter.emit(
        state,
        ExecutionEventType.EXECUTION_FAILED,
        payload={"error": error, "diagnostics": diagnostics["worker_failure"]},
    )


async def _event_state_for(context: "ApiContext", execution_id: str, workflow_id: str) -> NativeExecutionState:
    existing_events = await context.execution_store.list_events(execution_id)
    state = NativeExecutionState(execution_id=execution_id, workflow_id=workflow_id)
    if existing_events:
        last_event = existing_events[-1]
        state.sequence = last_event.sequence
        state.last_event_id = last_event.id
        state.trace_id = last_event.trace_id or state.trace_id
    return state


async def _async_main() -> int:
    from app.api.context import create_worker_api_context

    worker_env = load_worker_environment()
    context = create_worker_api_context(worker_id=worker_env["worker_id"])
    return await run_execution_worker(
        context=context,
        execution_id=worker_env["execution_id"],
        workflow_id=worker_env["workflow_id"],
        goal_id=worker_env["goal_id"],
        runtime_revision_id=worker_env["runtime_revision_id"],
        runtime_adapter_id=worker_env["runtime_adapter_id"],
        worker_id=worker_env["worker_id"],
        heartbeat_interval_seconds=worker_env["heartbeat_interval_seconds"],
        execution_timeout_seconds=worker_env["execution_timeout_seconds"],
    )


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
