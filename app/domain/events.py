from __future__ import annotations

from datetime import datetime
from enum import Enum
from pydantic import Field, model_validator
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.core.time import utc_now
from .credentials import DomainModel


class ExecutionEventType(str, Enum):
    EXECUTION_CREATED = "execution.created"
    EXECUTION_STARTED = "execution.started"
    EXECUTION_PAUSED = "execution.paused"
    EXECUTION_RESUMED = "execution.resumed"
    EXECUTION_CANCELLED = "execution.cancelled"
    EXECUTION_COMPLETED = "execution.completed"
    EXECUTION_FAILED = "execution.failed"
    EXECUTION_REPAIRED = "execution.repaired"
    TASK_STARTED = "task.started"
    AGENT_MESSAGE_CREATED = "agent.message.created"
    LLM_REQUEST_CREATED = "llm.request.created"
    LLM_RESPONSE_CREATED = "llm.response.created"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    TOOL_CALL_FAILED = "tool.call.failed"
    HANDOFF_REQUESTED = "handoff.requested"
    ARTIFACT_CREATED = "artifact.created"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    CONTAINER_CREATED = "container.created"
    CONTAINER_STARTED = "container.started"
    CONTAINER_REPLACED = "container.replaced"
    CONTAINER_STOPPED = "container.stopped"
    CONTAINER_FAILED = "container.failed"
    RUNTIME_REVISION_RESOLVED = "runtime.revision.resolved"
    RUNTIME_REVISION_INVALIDATED = "runtime.revision.invalidated"
    RUNTIME_BUILD_STARTED = "runtime.build.started"
    RUNTIME_BUILD_COMPLETED = "runtime.build.completed"
    RUNTIME_BUILD_FAILED = "runtime.build.failed"
    MONITOR_FINDING_CREATED = "monitor.finding.created"
    MONITOR_EVALUATION_RECORDED = "monitor.evaluation.recorded"
    MONITOR_IMPROVEMENT_PROPOSED = "monitor.improvement.proposed"
    MONITOR_IMPROVEMENT_COMPARED = "monitor.improvement.compared"


class ExecutionEvent(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    execution_id: str
    workflow_id: Optional[str] = None
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    model_request_id: Optional[str] = None
    parent_event_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    event_type: ExecutionEventType
    timestamp: datetime = Field(default_factory=utc_now)
    sequence: int = 0
    actor_type: str = "system"
    actor: Optional[str] = Field(default=None, alias="actor_id")
    payload: Dict[str, Any] = Field(default_factory=dict, alias="payload_json")
    metrics: Dict[str, Any] = Field(default_factory=dict)
    redacted_fields: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_event_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        updated = dict(data)
        if "actor_type" not in updated:
            updated["actor_type"] = "agent" if updated.get("actor") or updated.get("actor_id") else "system"
        return updated
