"""Local admin bootstrap and password-auth helpers for first-run onboarding.

This stays deliberately small and backend-owned so the beginner-friendly setup
flow can create the first local admin without introducing a full external auth
provider dependency. Password state lives in user metadata for now to avoid a
schema migration while the onboarding UX is still being proven.
"""

from __future__ import annotations

import bcrypt
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.domain import API_TOKEN_SCOPE_DEFINITIONS, ApiTokenDefinition, UserDefinition

if TYPE_CHECKING:
    from app.api.context import ApiContext

LOCAL_AUTH_METADATA_KEY = "local_auth"
LOCAL_AUTH_TOKEN_PREFIX = "agt"
LOCAL_AUTH_TOKEN_NAME = "Local auth session"
LOCAL_AUTH_SESSION_TTL = timedelta(hours=24)
ALL_LOCAL_AUTH_SCOPES = [scope.id for scope in API_TOKEN_SCOPE_DEFINITIONS]


class LocalAuthError(RuntimeError):
    pass


class LocalAuthBootstrapUnavailableError(LocalAuthError):
    pass


@dataclass(slots=True)
class LocalAuthBootstrapResult:
    user: UserDefinition


@dataclass(slots=True)
class LocalAuthLoginResult:
    raw_token: str
    token: ApiTokenDefinition
    user: UserDefinition


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _generate_token() -> str:
    return f"{LOCAL_AUTH_TOKEN_PREFIX}_{secrets.token_urlsafe(32)}"


def _local_auth_metadata(user: UserDefinition) -> dict[str, Any]:
    value = user.metadata.get(LOCAL_AUTH_METADATA_KEY)
    return value if isinstance(value, dict) else {}


def has_local_password(user: UserDefinition) -> bool:
    password_hash = _local_auth_metadata(user).get("password_hash")
    return isinstance(password_hash, str) and bool(password_hash.strip())


@dataclass(slots=True)
class LocalAuthService:
    context: ApiContext

    async def bootstrap_local_admin(
            self,
            *,
            email: str,
            password: str,
            display_name: str | None = None,
    ) -> LocalAuthBootstrapResult:
        normalized_email = email.strip().lower()
        if not normalized_email:
            raise ValueError("Email is required.")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters.")

        existing_users = await self.context.user_repo.list()
        if any("admin" in user.roles for user in existing_users):
            raise LocalAuthBootstrapUnavailableError(
                "A local admin already exists for this Agency backend."
            )

        password_hash = _hash_password(password)
        now = datetime.now(timezone.utc).isoformat()
        existing_user = await self.context.user_repo.find_by_email(normalized_email)
        if existing_user is not None:
            existing_password_hash = _local_auth_metadata(existing_user).get("password_hash")
            if isinstance(existing_password_hash, str) and existing_password_hash:
                if not _verify_password(password, existing_password_hash):
                    raise ValueError("The existing local user's password is invalid.")
            elif existing_user.provider != "dev-auth":
                raise ValueError(f"User '{normalized_email}' already exists.")

            # Dev auth may synchronize the first user before browser onboarding.
            # Claim that exact local identity instead of creating a duplicate.
            metadata = dict(existing_user.metadata)
            metadata[LOCAL_AUTH_METADATA_KEY] = {
                "password_hash": password_hash,
                "password_updated_at": now,
                "bootstrap_created": True,
                "promoted_existing_user": True,
            }
            roles = list(dict.fromkeys([*existing_user.roles, "admin"]))
            promoted = existing_user.model_copy(
                update={
                    "display_name": (display_name or existing_user.display_name or normalized_email).strip()
                                    or normalized_email,
                    "roles": roles,
                    "metadata": metadata,
                }
            )
            saved = await self.context.user_repo.save(promoted)
            return LocalAuthBootstrapResult(user=saved)

        user = UserDefinition(
            email=normalized_email,
            display_name=(display_name or normalized_email).strip() or normalized_email,
            roles=["admin"],
            provider="local",
            provider_subject=normalized_email,
            provider_account_id=normalized_email,
            metadata={
                LOCAL_AUTH_METADATA_KEY: {
                    "password_hash": password_hash,
                    "password_updated_at": now,
                    "bootstrap_created": True,
                }
            },
        )
        created = await self.context.user_repo.create(user)
        return LocalAuthBootstrapResult(user=created)

    async def authenticate(self, *, email: str, password: str) -> LocalAuthLoginResult | None:
        normalized_email = email.strip().lower()
        if not normalized_email or not password:
            return None

        user = await self.context.user_repo.find_by_email(normalized_email)
        if user is None or not has_local_password(user):
            return None

        password_hash = _local_auth_metadata(user).get("password_hash")
        if not isinstance(password_hash, str) or not _verify_password(password, password_hash):
            return None

        raw_token = _generate_token()
        now = datetime.now(timezone.utc)
        token = ApiTokenDefinition(
            owner_user_id=user.id,
            name=LOCAL_AUTH_TOKEN_NAME,
            token_hash=_token_hash(raw_token),
            prefix=raw_token[:8],
            last4=raw_token[-4:],
            scopes=list(ALL_LOCAL_AUTH_SCOPES),
            expires_at=now + LOCAL_AUTH_SESSION_TTL,
            metadata={
                "issued_by": "local_auth",
                "session": True,
            },
        )
        created = await self.context.api_token_repo.create(token)
        return LocalAuthLoginResult(raw_token=raw_token, token=created, user=user)
