"""User sync/admin routes and OneCLI identity mapping side effects."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, ValidationError, field_validator
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import header_value, require_trusted_identity_source, resolve_current_user
from app.core.config import get_settings
from app.domain import UserDefinition, UserStatus
from app.domain.users import PROFILE_PREFERENCES_METADATA_KEY
from app.services.onecli import OneCLIIdentityMappingService
from ._crud import serializable_validation_errors
from ._user_payload import public_user_payload


def _require_admin(current_user: UserDefinition) -> None:
    if "admin" not in current_user.roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role is required")


class CurrentUserProfilePatch(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("display_name", "timezone")
    @classmethod
    def strip_profile_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Profile settings cannot be blank.")
        return normalized

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA timezone such as Asia/Singapore or UTC.") from exc
        return value


def create_users_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    onecli_service = OneCLIIdentityMappingService(context)
    router = APIRouter(tags=["Users"])

    @router.post("/users/sync", summary="Sync Current User")
    async def sync_user(payload: dict[str, Any], request: Request):
        require_trusted_identity_source(request)
        if get_settings().app_env != "test":
            asserted_id = header_value(request, "x-agency-user-id")
            asserted_email = header_value(request, "x-agency-user-email")
            if not asserted_id and not asserted_email:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Trusted identity claims are required",
                )
            existing = await context.user_repo.get(asserted_id) if asserted_id else None
            if existing is None and asserted_email:
                existing = await context.user_repo.find_by_email(asserted_email)
            # The upstream identity bridge may refresh profile presentation fields,
            # but it must never grant Agency roles, reactivate users, or overwrite
            # security-sensitive metadata from a browser-provided JSON body.
            payload = {
                **payload,
                "id": asserted_id or (existing.id if existing else payload.get("id")),
                "email": asserted_email or (existing.email if existing else payload.get("email")),
                "roles": list(existing.roles) if existing else [],
                "status": existing.status if existing else UserStatus.ACTIVE,
                "metadata": dict(existing.metadata) if existing else {},
            }
        try:
            user = UserDefinition.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        saved = await context.user_repo.upsert_from_identity(user)
        if saved.status == UserStatus.DISABLED:
            await onecli_service.disable_active_for_owner(
                saved.id,
                actor_user_id=saved.id,
                reason="user_disabled",
            )
        return public_user_payload(saved)

    @router.put("/me", summary="Upsert Current User")
    async def upsert_me(payload: dict[str, Any], request: Request):
        return await sync_user(payload, request)

    @router.get("/me", summary="Get Current User")
    async def get_me(request: Request):
        user = await resolve_current_user(request, context)
        return public_user_payload(user)

    @router.patch("/me/profile", summary="Update Current User Profile Settings")
    async def update_my_profile(payload: CurrentUserProfilePatch, request: Request):
        user = await resolve_current_user(request, context)
        current_preferences = user.metadata.get(PROFILE_PREFERENCES_METADATA_KEY)
        preferences = dict(current_preferences) if isinstance(current_preferences, dict) else {}
        preferences.update(
            {
                "display_name": payload.display_name,
                "timezone": payload.timezone,
            }
        )
        # Only this allowlisted metadata namespace is writable from Profile so
        # auth, local-password, and integration metadata cannot be overwritten.
        updated = user.model_copy(
            update={
                "display_name": payload.display_name,
                "metadata": {
                    **user.metadata,
                    PROFILE_PREFERENCES_METADATA_KEY: preferences,
                },
            }
        )
        return public_user_payload(await context.user_repo.save(updated))

    @router.get("/users", summary="Search Users")
    async def search_users(email: str | None = Query(default=None)):
        if email:
            users = await context.user_repo.search_by_email(email)
        else:
            users = await context.user_repo.list()
        return {"items": [public_user_payload(user) for user in users]}

    @router.get("/users/{user_id}", summary="Get User By Id")
    async def get_user(user_id: str):
        user = await context.user_repo.get(user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User '{user_id}' not found")
        return public_user_payload(user)

    @router.delete("/users/{user_id}", summary="Disable User")
    async def disable_user(user_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        _require_admin(current_user)
        disabled = await context.user_repo.soft_delete(user_id)
        if not disabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User '{user_id}' not found")
        await onecli_service.disable_active_for_owner(
            user_id,
            actor_user_id=current_user.id,
            reason="user_deleted",
        )
        user = await context.user_repo.get(user_id)
        return public_user_payload(user) if user is not None else {"id": user_id, "status": "disabled"}

    return router
