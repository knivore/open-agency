"""Domain event contracts emitted during workflow execution."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pydantic import Field, model_validator
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.core.time import utc_now
from .credentials import DomainModel


class ExecutionEventType(str, Enum):
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    EXECUTION_CREATED = "execution.created"
    EXECUTION_STARTED = "execution.started"
    EXECUTION_PAUSED = "execution.paused"
    EXECUTION_RESUMED = "execution.resumed"
    EXECUTION_CANCELLED = "execution.cancelled"
    EXECUTION_COMPLETED = "execution.completed"
    EXECUTION_FAILED = "execution.failed"
    EXECUTION_REPAIRED = "execution.repaired"
    TASK_STARTED = "task.started"
    AGENT_STEP_STARTED = "agent.step.started"
    AGENT_STEP_COMPLETED = "agent.step.completed"
    AGENT_STEP_FAILED = "agent.step.failed"
    SUBAGENT_TASK_ASSIGNED = "subagent.task.assigned"
    SUBAGENT_PROGRESS_UPDATED = "subagent.progress.updated"
    SUBAGENT_STEP_COMPLETED = "subagent.step.completed"
    SUBAGENT_STEP_FAILED = "subagent.step.failed"
    SUBAGENT_NEEDS_INPUT = "subagent.needs_input"
    SUBAGENT_NEEDS_APPROVAL = "subagent.needs_approval"
    AGENT_MESSAGE_CREATED = "agent.message.created"
    LLM_REQUEST_CREATED = "llm.request.created"
    LLM_RESPONSE_CREATED = "llm.response.created"
    MODEL_FALLBACK_USED = "model.fallback.used"
    MODEL_FALLBACK_FAILED = "model.fallback.failed"
    TOKEN_USAGE_RECORDED = "token.usage.recorded"
    TOKEN_BUDGET_WARNING = "token.budget.warning"
    TOKEN_BUDGET_EXCEEDED = "token.budget.exceeded"
    CONTEXT_HEALTH_RECORDED = "context.health.recorded"
    CONTEXT_COMPACTION_STARTED = "context.compaction.started"
    CONTEXT_COMPACTION_COMPLETED = "context.compaction.completed"
    CONTEXT_COMPACTION_FAILED = "context.compaction.failed"
    SUPERVISOR_STEERING_REQUESTED = "supervisor.steering.requested"
    SUPERVISOR_STEERING_APPLIED = "supervisor.steering.applied"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    TOOL_CALL_FAILED = "tool.call.failed"
    HANDOFF_REQUESTED = "handoff.requested"
    HANDOFF_COMPLETED = "handoff.completed"
    ARTIFACT_CREATED = "artifact.created"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    CONTAINER_CREATED = "container.created"
    CONTAINER_STARTED = "container.started"
    CONTAINER_REPLACED = "container.replaced"
    CONTAINER_STOPPED = "container.stopped"
    CONTAINER_FAILED = "container.failed"
    ONECLI_WORKER_ENFORCEMENT_RECORDED = "onecli.worker.enforcement.recorded"
    RUNTIME_REVISION_RESOLVED = "runtime.revision.resolved"
    RUNTIME_REVISION_INVALIDATED = "runtime.revision.invalidated"
    RUNTIME_BUILD_STARTED = "runtime.build.started"
    RUNTIME_BUILD_COMPLETED = "runtime.build.completed"
    RUNTIME_BUILD_FAILED = "runtime.build.failed"
    MONITOR_FINDING_CREATED = "monitor.finding.created"
    MONITOR_EVALUATION_RECORDED = "monitor.evaluation.recorded"
    MONITOR_IMPROVEMENT_PROPOSED = "monitor.improvement.proposed"
    MONITOR_IMPROVEMENT_COMPARED = "monitor.improvement.compared"
    OUTBOUND_WEBHOOK_QUEUED = "outbound_webhook.queued"
    OUTBOUND_WEBHOOK_SENT = "outbound_webhook.sent"
    OUTBOUND_WEBHOOK_FAILED = "outbound_webhook.failed"
    AGENT_IMPORT_PREVIEWED = "agent.import.previewed"
    AGENT_IMPORT_COMMITTED = "agent.import.committed"


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
    source: Optional[str] = None
    status: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict, alias="payload_json")
    payload_sha256: Optional[str] = None
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
