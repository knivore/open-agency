from __future__ import annotations

from datetime import datetime
from enum import Enum
from pydantic import AliasChoices, Field, model_validator
from typing import Any, Dict, Optional
from uuid import uuid4

from .credentials import DomainModel


class ScheduleType(str, Enum):
    MANUAL = "manual"
    CRON = "cron"
    INTERVAL = "interval"
    WEBHOOK = "webhook"
    FILE_CREATED = "file_created"
    EMAIL_RECEIVED = "email_received"
    DATABASE_POLL = "database_poll"
    EVENT_MATCH = "event_match"


class ScheduleDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    workflow_id: str
    enabled: bool = True
    trigger_type: ScheduleType = Field(
        default=ScheduleType.MANUAL,
        validation_alias=AliasChoices("trigger_type", "schedule_type"),
        serialization_alias="trigger_type",
    )
    trigger_config: Dict[str, Any] = Field(default_factory=dict)
    input_template: Dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("input_template", "input_payload"),
        serialization_alias="input_template",
    )
    runtime_adapter_override: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("runtime_adapter_override", "runtime_adapter_id"),
        serialization_alias="runtime_adapter_override",
    )
    max_concurrent_executions: int = 1
    timezone: str = "UTC"
    next_fire_at: Optional[datetime] = Field(
        default=None,
        validation_alias=AliasChoices("next_fire_at", "next_run_at"),
        serialization_alias="next_fire_at",
    )
    last_fire_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_schedule_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        updated = dict(data)
        trigger_type = updated.get("trigger_type", updated.get("schedule_type", ScheduleType.MANUAL.value))
        trigger_config = dict(updated.get("trigger_config") or {})
        if "cron" in updated and updated.get("cron") is not None:
            trigger_config.setdefault("cron", updated["cron"])
        if "interval_seconds" in updated and updated.get("interval_seconds") is not None:
            trigger_config.setdefault("interval_seconds", updated["interval_seconds"])
        updated["trigger_config"] = trigger_config
        if "input_payload" in updated and "input_template" not in updated:
            updated["input_template"] = updated["input_payload"]
        if "runtime_adapter_id" in updated and "runtime_adapter_override" not in updated:
            updated["runtime_adapter_override"] = updated["runtime_adapter_id"]
        if "next_run_at" in updated and "next_fire_at" not in updated:
            updated["next_fire_at"] = updated["next_run_at"]
        updated["trigger_type"] = trigger_type
        updated.pop("schedule_type", None)
        updated.pop("cron", None)
        updated.pop("interval_seconds", None)
        updated.pop("runtime_adapter_id", None)
        updated.pop("input_payload", None)
        updated.pop("next_run_at", None)
        return updated

    @property
    def schedule_type(self) -> ScheduleType:
        return self.trigger_type

    @property
    def runtime_adapter_id(self) -> Optional[str]:
        return self.runtime_adapter_override

    @property
    def input_payload(self) -> Dict[str, Any]:
        return self.input_template

    @property
    def next_run_at(self) -> Optional[datetime]:
        return self.next_fire_at

    @property
    def cron(self) -> Optional[str]:
        return self.trigger_config.get("cron")

    @property
    def interval_seconds(self) -> Optional[int]:
        interval = self.trigger_config.get("interval_seconds")
        return int(interval) if interval is not None else None

    @model_validator(mode="after")
    def validate_schedule_configuration(self) -> "ScheduleDefinition":
        if self.trigger_type == ScheduleType.CRON and self.trigger_config.get("cron") is None:
            raise ValueError("Cron schedules require trigger_config.cron")
        if self.trigger_type == ScheduleType.INTERVAL and self.trigger_config.get("interval_seconds") is None:
            raise ValueError("Interval schedules require trigger_config.interval_seconds")
        if self.max_concurrent_executions < 1:
            raise ValueError("max_concurrent_executions must be at least 1")
        return self
