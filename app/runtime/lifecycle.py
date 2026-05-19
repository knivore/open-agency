from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain import ExecutionEventType, RuntimeRevision
from app.runtime.containers import RuntimeContainerState
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState


def runtime_revision_payload(
        revision: RuntimeRevision,
        *,
        reason: str | None = None,
        extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "runtime_revision_id": revision.id,
        "fingerprint": revision.fingerprint,
        "source_path": revision.source_path,
        "build_status": revision.build_status.value,
        "image_name": revision.image_name,
        "image_tag": revision.image_tag,
        "base_image": revision.base_image,
    }
    if reason is not None:
        payload["reason"] = reason
    if revision.invalidation_reason and "reason" not in payload:
        payload["reason"] = revision.invalidation_reason
    if extra:
        payload.update(extra)
    return payload


def container_payload(
        container: RuntimeContainerState,
        *,
        runtime_revision_id: str | None = None,
        reason: str | None = None,
        extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "container_id": getattr(container, "container_id", None),
        "container_name": getattr(container, "name", None),
        "image": getattr(container, "image", None),
        "status": getattr(container, "status", None),
        "labels": getattr(container, "labels", {}) or {},
        "exit_code": getattr(container, "exit_code", None),
    }
    if runtime_revision_id is not None:
        payload["runtime_revision_id"] = runtime_revision_id
    if reason is not None:
        payload["reason"] = reason
    if extra:
        payload.update(extra)
    return payload


@dataclass(slots=True)
class RuntimeLifecycleEventEmitter:
    emitter: ExecutionEventEmitter

    async def emit_runtime_revision_resolved(self, state: NativeExecutionState, revision: RuntimeRevision):
        return await self.emitter.emit(
            state,
            ExecutionEventType.RUNTIME_REVISION_RESOLVED,
            payload=runtime_revision_payload(revision),
        )

    async def emit_runtime_revision_invalidated(
            self,
            state: NativeExecutionState,
            revision: RuntimeRevision,
            *,
            reason: str | None = None,
    ):
        return await self.emitter.emit(
            state,
            ExecutionEventType.RUNTIME_REVISION_INVALIDATED,
            payload=runtime_revision_payload(revision, reason=reason),
        )

    async def emit_runtime_build_started(self, state: NativeExecutionState, revision: RuntimeRevision):
        return await self.emitter.emit(
            state,
            ExecutionEventType.RUNTIME_BUILD_STARTED,
            payload=runtime_revision_payload(revision),
        )

    async def emit_runtime_build_completed(self, state: NativeExecutionState, revision: RuntimeRevision):
        return await self.emitter.emit(
            state,
            ExecutionEventType.RUNTIME_BUILD_COMPLETED,
            payload=runtime_revision_payload(revision),
        )

    async def emit_runtime_build_failed(
            self,
            state: NativeExecutionState,
            revision: RuntimeRevision,
            *,
            reason: str | None = None,
    ):
        return await self.emitter.emit(
            state,
            ExecutionEventType.RUNTIME_BUILD_FAILED,
            payload=runtime_revision_payload(revision, reason=reason),
        )

    async def emit_container_created(
            self,
            state: NativeExecutionState,
            container: RuntimeContainerState,
            *,
            runtime_revision_id: str,
    ):
        return await self.emitter.emit(
            state,
            ExecutionEventType.CONTAINER_CREATED,
            payload=container_payload(container, runtime_revision_id=runtime_revision_id),
        )

    async def emit_container_started(
            self,
            state: NativeExecutionState,
            container: RuntimeContainerState,
            *,
            runtime_revision_id: str,
    ):
        return await self.emitter.emit(
            state,
            ExecutionEventType.CONTAINER_STARTED,
            payload=container_payload(container, runtime_revision_id=runtime_revision_id),
        )

    async def emit_container_replaced(
            self,
            state: NativeExecutionState,
            container: RuntimeContainerState,
            *,
            runtime_revision_id: str | None = None,
            reason: str | None = None,
            extra: dict[str, Any] | None = None,
    ):
        return await self.emitter.emit(
            state,
            ExecutionEventType.CONTAINER_REPLACED,
            payload=container_payload(
                container,
                runtime_revision_id=runtime_revision_id,
                reason=reason,
                extra=extra,
            ),
        )

    async def emit_container_stopped(
            self,
            state: NativeExecutionState,
            container: RuntimeContainerState,
            *,
            runtime_revision_id: str | None = None,
            reason: str | None = None,
    ):
        return await self.emitter.emit(
            state,
            ExecutionEventType.CONTAINER_STOPPED,
            payload=container_payload(container, runtime_revision_id=runtime_revision_id, reason=reason),
        )

    async def emit_container_failed(
            self,
            state: NativeExecutionState,
            container: RuntimeContainerState,
            *,
            runtime_revision_id: str | None = None,
            reason: str | None = None,
            extra: dict[str, Any] | None = None,
    ):
        return await self.emitter.emit(
            state,
            ExecutionEventType.CONTAINER_FAILED,
            payload=container_payload(
                container,
                runtime_revision_id=runtime_revision_id,
                reason=reason,
                extra=extra,
            ),
        )
