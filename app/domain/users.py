from __future__ import annotations

from enum import Enum
from pydantic import Field
from typing import Any
from uuid import uuid4

from .credentials import DomainModel


class UserStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    INVITED = "invited"


class UserDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    email: str
    display_name: str | None = None
    avatar_url: str | None = None
    status: UserStatus = UserStatus.ACTIVE
    roles: list[str] = Field(default_factory=list)
    provider: str | None = None
    provider_subject: str | None = None
    provider_account_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
