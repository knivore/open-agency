from __future__ import annotations

from fastapi import HTTPException, Request, status
from typing import Iterable

from app.api.context import ApiContext
from app.domain import UserDefinition


LOCAL_SYSTEM_USER = UserDefinition(
    id="local-user",
    email="local@agency.local",
    display_name="Local User",
    roles=["admin"],
    provider="local",
    provider_subject="local-user",
)


def require_active_user(user: UserDefinition) -> None:
    status_value = getattr(user.status, "value", user.status)
    if status_value == "disabled":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Current user is disabled")


def header_value(request: Request, name: str) -> str | None:
    value = request.headers.get(name)
    return value.strip() if value and value.strip() else None


def require_trusted_identity_source(request: Request) -> None:
    return None


def bearer_token(request: Request) -> str | None:
    authorization = header_value(request, "authorization")
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def request_has_identity(request: Request) -> bool:
    return bool(
        header_value(request, "x-agency-user-id")
        or header_value(request, "x-agency-user-email")
    )


async def resolve_user_from_bearer_token(request: Request, context: ApiContext) -> UserDefinition | None:
    return None


async def resolve_current_user(
        request: Request,
        context: ApiContext,
        required_scopes: Iterable[str] | None = None,
) -> UserDefinition:
    user_id = header_value(request, "x-agency-user-id")
    email = header_value(request, "x-agency-user-email")

    if user_id or email:
        return UserDefinition(
            id=user_id or email or LOCAL_SYSTEM_USER.id,
            email=email or f"{user_id}@agency.local",
            display_name=header_value(request, "x-agency-user-name"),
            roles=["admin"],
            provider=header_value(request, "x-agency-auth-provider") or "local",
            provider_subject=header_value(request, "x-agency-provider-subject") or user_id,
            provider_account_id=header_value(request, "x-agency-provider-account-id") or email,
        )
    return LOCAL_SYSTEM_USER


async def resolve_current_user_if_present(
        request: Request,
        context: ApiContext,
        required_scopes: Iterable[str] | None = None,
) -> UserDefinition | None:
    return await resolve_current_user(request, context, required_scopes=required_scopes)
