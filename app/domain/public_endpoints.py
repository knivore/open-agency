"""Domain models for launcher-managed public endpoint discovery."""

from __future__ import annotations

from datetime import datetime
from pydantic import Field, field_validator
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from .credentials import DomainModel


class PublicEndpointRecord(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    endpoint_type: Literal["webhook_base_url"] = "webhook_base_url"
    provider: str
    url: str
    status: Literal["active", "inactive"] = "active"
    source: str = "launcher"
    metadata: dict[str, Any] = Field(default_factory=dict)
    last_seen_at: datetime | None = None

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        return value.strip().lower().replace("_", "-")

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        candidate = value.strip().rstrip("/")
        parsed = urlsplit(candidate)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Public endpoint URLs must be absolute https URLs.")
        return candidate
