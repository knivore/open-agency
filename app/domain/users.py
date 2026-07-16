"""Domain contracts for authenticated users."""

from __future__ import annotations

from enum import Enum
from pydantic import Field
from typing import Any
from uuid import uuid4

from .credentials import DomainModel


PROFILE_PREFERENCES_METADATA_KEY = "profile_preferences"


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


def apply_profile_preference_overrides(
    existing: UserDefinition,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Keep owner-managed profile and local sign-in fields authoritative during identity re-syncs."""
    preferences = existing.metadata.get(PROFILE_PREFERENCES_METADATA_KEY)
    if isinstance(preferences, dict):
        preferred_name = preferences.get("display_name")
        if isinstance(preferred_name, str) and preferred_name.strip():
            incoming["display_name"] = preferred_name.strip()

    local_auth = existing.metadata.get("local_auth")
    if isinstance(local_auth, dict):
        owner_email = local_auth.get("email")
        if isinstance(owner_email, str) and owner_email.strip():
            # A browser may synchronize stale identity claims immediately before
            # credential-change sign-out; the locally verified email must win.
            incoming["email"] = owner_email.strip().lower()
            incoming["provider_account_id"] = owner_email.strip().lower()
    return incoming
