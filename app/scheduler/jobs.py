from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain import ScheduleDefinition


@dataclass(slots=True)
class ScheduledJobResult:
    schedule: ScheduleDefinition
    execution_id: str | None
    triggered_at: datetime
    metadata: dict[str, Any]
