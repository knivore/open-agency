"""User sync/admin routes and OneCLI identity mapping side effects."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import ValidationError
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import require_trusted_identity_source, resolve_current_user
from app.domain import UserDefinition, UserStatus
from app.services.onecli import OneCLIIdentityMappingService
from ._crud import serializable_validation_errors
from ._user_payload import public_user_payload


def _require_admin(current_user: UserDefinition) -> None:
    if "admin" not in current_user.roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role is required")


def create_users_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    onecli_service = OneCLIIdentityMappingService(context)
    router = APIRouter(tags=["Users"])

    @router.post("/users/sync", summary="Sync Current User")
    async def sync_user(payload: dict[str, Any], request: Request):
        require_trusted_identity_source(request)
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
