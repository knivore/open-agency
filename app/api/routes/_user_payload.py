"""Safe user serialization helpers for routes that expose user records."""

from __future__ import annotations

from app.domain import UserDefinition
from app.services.local_auth import LOCAL_AUTH_METADATA_KEY, has_local_password


def public_user_payload(user: UserDefinition) -> dict:
    payload = user.model_dump(mode="json")
    payload["local_credentials_enabled"] = has_local_password(user)
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return payload

    local_auth = metadata.get(LOCAL_AUTH_METADATA_KEY)
    if not isinstance(local_auth, dict):
        return payload

    redacted_local_auth = {key: value for key, value in local_auth.items() if key != "password_hash"}
    payload["metadata"] = {
        **metadata,
        LOCAL_AUTH_METADATA_KEY: redacted_local_auth,
    }
    return payload
