from __future__ import annotations

import asyncio
import logging
import os
import traceback
from datetime import datetime
from typing import TYPE_CHECKING

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
        await context.execution_store.update_execution(execution)
        execution_coro = context.runtime_registry.start_execution(execution_id)
        if execution_timeout_seconds is not None:
            result = await asyncio.wait_for(execution_coro, timeout=execution_timeout_seconds)
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


def _exit_code_for_execution(execution) -> int:
    if execution.status == ExecutionStatus.COMPLETED:
        return WORKER_EXIT_SUCCESS
    if execution.status == ExecutionStatus.CANCELLED:
        return WORKER_EXIT_CANCELLED
    if execution.status == ExecutionStatus.FAILED:
        return WORKER_EXIT_WORKFLOW_FAILED
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
