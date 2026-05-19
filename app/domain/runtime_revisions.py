from __future__ import annotations

from datetime import datetime
from enum import Enum
from pydantic import Field
from typing import Any, Dict, Optional
from uuid import uuid4

from app.core.time import utc_now
from .credentials import DomainModel


class RuntimeRevisionStatus(str, Enum):
    PENDING = "pending"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    INVALIDATED = "invalidated"


class RuntimeRevision(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    fingerprint: str
    source_path: str = "integrations/"
    build_status: RuntimeRevisionStatus = RuntimeRevisionStatus.PENDING
    image_name: Optional[str] = None
    image_tag: Optional[str] = None
    base_image: Optional[str] = None
    build_log_ref: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    ready_at: Optional[datetime] = None
    invalidated_at: Optional[datetime] = None
    invalidation_reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict, alias="metadata_json")
