from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import uuid4

from app.core.time import utc_now
from app.domain import ExecutionArtifact, ExecutionEvent, ExecutionEventType
from app.observability.event_bus import get_default_event_bus
from app.runtime.native.state import ExecutionStore, NativeExecutionState
from app.runtime.streaming.execution_event_mapper import map_execution_event_to_runtime_events
from app.runtime.streaming.runtime_event_publisher import RuntimeEventPublisher


class ExecutionEventEmitter:
    def __init__(self, store: ExecutionStore):
        self.store = store
        self.event_bus = get_default_event_bus()
        self.runtime_event_publisher = RuntimeEventPublisher()

    async def emit(
            self,
            state: NativeExecutionState,
            event_type: ExecutionEventType,
            *,
            actor: Optional[str] = None,
            payload: Optional[Dict[str, Any]] = None,
            metrics: Optional[Dict[str, Any]] = None,
            metadata: Optional[Dict[str, Any]] = None,
            agent_id: Optional[str] = None,
            task_id: Optional[str] = None,
            tool_call_id: Optional[str] = None,
            model_request_id: Optional[str] = None,
            parent_event_id: Optional[str] = None,
    ) -> ExecutionEvent:
        event = ExecutionEvent(
            execution_id=state.execution_id,
            workflow_id=state.workflow_id,
            agent_id=agent_id or state.current_agent_id,
            task_id=task_id or state.current_task_id,
            tool_call_id=tool_call_id,
            model_request_id=model_request_id,
            parent_event_id=parent_event_id or state.last_event_id,
            trace_id=state.trace_id,
            span_id=str(uuid4()),
            event_type=event_type,
            timestamp=utc_now(),
            sequence=state.next_sequence(),
            actor=actor,
            payload=payload or {},
            metrics=metrics or {},
            metadata=metadata or {},
        )
        prepared = self.event_bus.publish(event)
        saved = await self.store.save_event(prepared)
        await self._publish_runtime_events(saved)
        state.sequence = saved.sequence
        state.last_event_id = saved.id
        return saved

    async def _publish_runtime_events(self, event: ExecutionEvent) -> None:
        try:
            runtime_events = map_execution_event_to_runtime_events(event)
            for runtime_event in runtime_events:
                await self.runtime_event_publisher.publish(runtime_event)
        except Exception:
            # Runtime visualization must never block canonical execution event persistence.
            return

    async def record_artifact(
            self,
            state: NativeExecutionState,
            *,
            name: str,
            artifact_type: str,
            uri: str,
            media_type: Optional[str] = None,
            size_bytes: Optional[int] = None,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> ExecutionArtifact:
        artifact = ExecutionArtifact(
            execution_id=state.execution_id,
            name=name,
            artifact_type=artifact_type,
            uri=uri,
            media_type=media_type,
            size_bytes=size_bytes,
            metadata=metadata or {},
        )
        await self.store.save_artifact(artifact)
        await self.emit(
            state,
            ExecutionEventType.ARTIFACT_CREATED,
            payload={
                "artifact_id": artifact.id,
                "name": name,
                "artifact_type": artifact_type,
                "uri": uri,
            },
            metrics={"artifact_size_bytes": size_bytes or 0},
        )
        return artifact
