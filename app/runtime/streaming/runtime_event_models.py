from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from pydantic import Field, field_validator
from typing import Any, Dict, Optional
from uuid import uuid4

from app.core.time import utc_now
from app.domain.credentials import DomainModel

AGENCY_RUNTIME_EVENT_SCHEMA_VERSION = "agency.runtime-event.v1"


class RuntimeEventSourceType(str, Enum):
    AGENCY = "agency"
    HERMES = "hermes"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    CUSTOM = "custom"
    LOCAL = "local"


class RuntimeEventLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"


class RuntimeEventType(str, Enum):
    AGENT_STATUS_CHANGED = "agent_status_changed"
    AGENT_SPOKE = "agent_spoke"
    TASK_STARTED = "task_started"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    LOG_RECEIVED = "log_received"
    APPROVAL_REQUIRED = "approval_required"
    FILE_CHANGED = "file_changed"
    WORKFLOW_TRANSITIONED = "workflow_transitioned"


class RuntimeEventActor(DomainModel):
    id: str
    name: Optional[str] = None
    role: Optional[str] = None
    avatar_asset_id: Optional[str] = Field(default=None, alias="avatarAssetId")


class RuntimeEventWorkflow(DomainModel):
    id: str
    name: Optional[str] = None
    room_id: Optional[str] = Field(default=None, alias="roomId")


class RuntimeEventTask(DomainModel):
    id: str
    title: Optional[str] = None
    progress: Optional[float] = Field(default=None, ge=0, le=1)


class RuntimeStreamEvent(DomainModel):
    id: str = Field(default_factory=lambda: f"runtime-event:{uuid4()}")
    schema_version: str = Field(default=AGENCY_RUNTIME_EVENT_SCHEMA_VERSION, alias="schemaVersion")
    source: str = "agency"
    source_type: RuntimeEventSourceType = Field(default=RuntimeEventSourceType.AGENCY, alias="sourceType")
    type: RuntimeEventType
    timestamp: datetime = Field(default_factory=utc_now)
    actor: Optional[RuntimeEventActor] = None
    workflow: Optional[RuntimeEventWorkflow] = None
    task: Optional[RuntimeEventTask] = None
    level: RuntimeEventLevel = RuntimeEventLevel.INFO
    message: Optional[str] = Field(default=None, max_length=1000)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata must contain only JSON-serializable values") from exc
        return value

    def to_external_event(self) -> Dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)
