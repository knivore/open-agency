from __future__ import annotations

from datetime import datetime
from enum import Enum
from pydantic import Field, model_validator
from typing import Any, Dict, Optional
from uuid import uuid4

from app.core.time import utc_now
from .credentials import DomainModel


class ExecutionStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionArtifact(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    execution_id: str
    event_id: Optional[str] = None
    artifact_type: str
    name: str
    content_json: Optional[Dict[str, Any]] = None
    content_text: Optional[str] = None
    uri: Optional[str] = Field(default=None, alias="file_path")
    media_type: Optional[str] = Field(default=None, alias="mime_type")
    size_bytes: Optional[int] = None
    created_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict, alias="metadata_json")


class Execution(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    workflow_id: str
    workflow_version_id: Optional[str] = None
    runtime_adapter_id: str = Field(alias="runtime_adapter")
    runtime_revision_id: Optional[str] = None
    runtime_fingerprint: Optional[str] = None
    status: ExecutionStatus = ExecutionStatus.CREATED
    trigger_type: str = "manual"
    trigger_payload: Dict[str, Any] = Field(default_factory=dict)
    input_payload: Dict[str, Any] = Field(default_factory=dict, alias="input_json")
    output_payload: Optional[Dict[str, Any]] = Field(default=None, alias="output_json")
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = Field(default=None, alias="ended_at")
    updated_at: datetime = Field(default_factory=utc_now)
    created_by: Optional[str] = None
    worker_id: Optional[str] = None
    last_heartbeat_at: Optional[datetime] = None
    container_id: Optional[str] = None
    container_name: Optional[str] = None
    container_image: Optional[str] = None
    container_status: Optional[str] = None
    container_started_at: Optional[datetime] = None
    container_ended_at: Optional[datetime] = None
    container_exit_code: Optional[int] = None
    replacement_of_execution_id: Optional[str] = None
    restart_reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_execution_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        updated = dict(data)
        trigger = updated.get("metadata", {}).get("trigger") if isinstance(updated.get("metadata"), dict) else None
        if "trigger_payload" not in updated and isinstance(trigger, dict):
            updated["trigger_payload"] = trigger
        if "created_by" not in updated and isinstance(trigger, dict):
            updated["created_by"] = trigger.get("created_by") or trigger.get("run_by")
        if "trigger_type" not in updated:
            updated["trigger_type"] = (trigger or {}).get("type", "manual")
        if "error_json" in updated and "error" not in updated:
            error = updated.get("error_json")
            if isinstance(error, dict):
                updated["error"] = error.get("message") or error.get("error") or str(error)
            else:
                updated["error"] = error
        return updated

    @property
    def error_json(self) -> Optional[Dict[str, Any]]:
        return None if self.error is None else {"message": self.error}
