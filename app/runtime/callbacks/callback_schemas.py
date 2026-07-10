"""Schemas used by internal sub-agent runtime callbacks."""

from __future__ import annotations

from datetime import datetime
from pydantic import Field, model_validator
from typing import Any

from app.core.time import utc_now
from app.domain import DomainModel, SubAgentStatusUpdate

STRUCTURED_STATUS_FIELDS = {
    "status",
    "current_task",
    "completed_step",
    "blocker",
    "clarification_needed",
    "confidence",
    "token_usage",
    "context_health",
    "tool_result_summary",
    "next_action",
    "progress_percent",
}


class SubAgentCallbackPayload(DomainModel):
    run_id: str
    step_id: str
    agent_id: str
    workflow_id: str | None = None
    source: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status_update: SubAgentStatusUpdate | None = None
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def normalize_required_ids(self) -> "SubAgentCallbackPayload":
        self.run_id = self.run_id.strip()
        self.step_id = self.step_id.strip()
        self.agent_id = self.agent_id.strip()
        if isinstance(self.workflow_id, str):
            self.workflow_id = self.workflow_id.strip() or None
        if isinstance(self.source, str):
            self.source = self.source.strip() or None
        if isinstance(self.idempotency_key, str):
            self.idempotency_key = self.idempotency_key.strip() or None
        missing = [
            name
            for name, value in (
                ("run_id", self.run_id),
                ("step_id", self.step_id),
                ("agent_id", self.agent_id),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required callback identifiers: {', '.join(missing)}")
        self.status_update = self._normalize_status_update()
        return self

    def _normalize_status_update(self) -> SubAgentStatusUpdate | None:
        nested = self.payload.get("status_update")
        if isinstance(nested, dict):
            return SubAgentStatusUpdate.model_validate(nested)
        structured = {
            key: self.payload[key]
            for key in STRUCTURED_STATUS_FIELDS
            if key in self.payload
        }
        if not structured:
            return None
        return SubAgentStatusUpdate.model_validate(structured)


class CallbackReceipt(DomainModel):
    ok: bool = True
    event_id: str
    run_id: str
    step_id: str
    status: str = "recorded"
    created_at: datetime = Field(default_factory=utc_now)
