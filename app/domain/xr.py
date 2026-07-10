"""XR simulator contracts for exercising canonical device orchestration."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pydantic import Field
from typing import Any
from uuid import uuid4

from app.core.time import utc_now
from .credentials import DomainModel


class XRSessionStatus(str, Enum):
    ACTIVE = "active"
    ENDED = "ended"


class XRSession(DomainModel):
    session_id: str = Field(default_factory=lambda: f"xr-session-{uuid4()}")
    device_id: str
    user_id: str | None = None
    status: XRSessionStatus = XRSessionStatus.ACTIVE
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
