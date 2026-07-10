"""Runtime event envelope creation and execution-event mapping."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pydantic import Field, model_validator
from typing import Any
from uuid import uuid4

from app.core.time import utc_now
from app.domain import DomainModel, ExecutionEvent, ExecutionEventType
from .payloads import payload_sha256


class RuntimeEventStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RuntimeEventEnvelope(DomainModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: ExecutionEventType
    run_id: str
    workflow_id: str | None = None
    agent_id: str | None = None
    step_id: str | None = None
    source: str
    status: RuntimeEventStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_sha256: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def normalize_runtime_event(self) -> "RuntimeEventEnvelope":
        self.run_id = self.run_id.strip()
        self.source = self.source.strip()
        if not self.run_id:
            raise ValueError("run_id is required")
        if not self.source:
            raise ValueError("source is required")
        self.workflow_id = self.workflow_id.strip() if isinstance(self.workflow_id, str) else self.workflow_id
        self.agent_id = self.agent_id.strip() if isinstance(self.agent_id, str) else self.agent_id
        self.step_id = self.step_id.strip() if isinstance(self.step_id, str) else self.step_id
        self.payload_sha256 = self.payload_sha256 or payload_sha256(self.payload)
        return self


def create_execution_event_from_runtime_event(
        event: RuntimeEventEnvelope,
        *,
        parent_event_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        actor: str | None = None,
) -> ExecutionEvent:
    metadata = {
        "runtime_event": True,
        "run_id": event.run_id,
        "step_id": event.step_id,
        "source": event.source,
        "status": event.status.value,
        "payload_sha256": event.payload_sha256,
    }
    return ExecutionEvent(
        id=event.event_id,
        execution_id=event.run_id,
        workflow_id=event.workflow_id,
        agent_id=event.agent_id,
        task_id=event.step_id,
        parent_event_id=parent_event_id,
        trace_id=trace_id,
        span_id=span_id,
        event_type=event.event_type,
        timestamp=event.created_at,
        actor=actor or event.source,
        source=event.source,
        status=event.status.value,
        payload=event.payload,
        payload_sha256=event.payload_sha256,
        metadata=metadata,
    )
