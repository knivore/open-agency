from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.services.onecli import OneCLIIdentityMappingService
from ._crud import serializable_validation_errors


def _hide_missing_or_cross_owner() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OneCLI identity mapping not found")


def _require_admin(current_user) -> None:
    if "admin" not in current_user.roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role is required")


def create_onecli_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = OneCLIIdentityMappingService(context)
    router = APIRouter(prefix="/onecli", tags=["OneCLI"])

    @router.get("/rule-profiles/default", summary="Get Default OneCLI Rule Profile")
    async def get_default_rule_profile(request: Request):
        await resolve_current_user(request, context, required_scopes=["integrations:read"])
        return service.public_default_rule_profile()

    @router.get("/identity-mappings", summary="List OneCLI Identity Mappings")
    async def list_identity_mappings(request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        mappings = await service.list_for_owner(current_user.id)
        return {"items": [service.public_mapping(item) for item in mappings]}

    @router.post("/identity-mappings", summary="Create OneCLI Identity Mapping")
    async def create_identity_mapping(payload: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            created = await service.create_for_owner(payload, current_user.id, actor_user_id=current_user.id)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return service.public_mapping(created)

    @router.get("/identity-mappings/{mapping_id}", summary="Get OneCLI Identity Mapping")
    async def get_identity_mapping(mapping_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        item = await service.get_for_owner(mapping_id, current_user.id)
        if item is None:
            raise _hide_missing_or_cross_owner()
        return service.public_mapping(item)

    @router.put("/identity-mappings/{mapping_id}", summary="Update OneCLI Identity Mapping")
    async def update_identity_mapping(mapping_id: str, patch: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            item = await service.update_for_owner(mapping_id, current_user.id, patch, actor_user_id=current_user.id)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if item is None:
            raise _hide_missing_or_cross_owner()
        return service.public_mapping(item)

    @router.delete("/identity-mappings/{mapping_id}", summary="Disable OneCLI Identity Mapping")
    async def disable_identity_mapping(mapping_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        disabled = await service.disable_for_owner(mapping_id, current_user.id, actor_user_id=current_user.id)
        if not disabled:
            raise _hide_missing_or_cross_owner()
        return {"disabled": True, "id": mapping_id}

    @router.get("/admin/identity-mappings", summary="Admin List OneCLI Identity Mappings")
    async def admin_list_identity_mappings(request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        _require_admin(current_user)
        mappings = await service.list_all(include_disabled=True)
        return {"items": [service.public_mapping(item) for item in mappings]}

    @router.get("/admin/rule-profiles/default", summary="Admin Get Default OneCLI Rule Profile")
    async def admin_get_default_rule_profile(request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        _require_admin(current_user)
        return service.public_default_rule_profile()

    @router.delete("/admin/users/{owner_user_id}/identity-mappings", summary="Admin Disable User OneCLI Mappings")
    async def admin_disable_user_identity_mappings(owner_user_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        _require_admin(current_user)
        disabled = await service.disable_active_for_owner(
            owner_user_id,
            actor_user_id=current_user.id,
            reason="admin_user_kill_switch",
            admin=True,
        )
        return {"disabled": True, "count": len(disabled), "ids": [item.id for item in disabled]}

    @router.delete(
        "/admin/workflows/{workflow_id}/identity-mappings",
        summary="Admin Disable Workflow OneCLI Mappings",
    )
    async def admin_disable_workflow_identity_mappings(workflow_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        _require_admin(current_user)
        disabled = await service.disable_active_for_workflow(
            workflow_id,
            actor_user_id=current_user.id,
            reason="admin_workflow_kill_switch",
        )
        return {"disabled": True, "count": len(disabled), "ids": [item.id for item in disabled]}

    @router.post("/admin/users/{owner_user_id}/identity-mappings", summary="Admin Create OneCLI Identity Mapping")
    async def admin_create_identity_mapping(owner_user_id: str, payload: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        _require_admin(current_user)
        try:
            created = await service.create_for_admin(
                {**payload, "owner_user_id": owner_user_id},
                actor_user_id=current_user.id,
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return service.public_mapping(created)

    @router.put("/admin/identity-mappings/{mapping_id}", summary="Admin Update OneCLI Identity Mapping")
    async def admin_update_identity_mapping(mapping_id: str, patch: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        _require_admin(current_user)
        try:
            item = await service.update_as_admin(mapping_id, patch, actor_user_id=current_user.id)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if item is None:
            raise _hide_missing_or_cross_owner()
        return service.public_mapping(item)

    @router.delete("/admin/identity-mappings/{mapping_id}", summary="Admin Disable OneCLI Identity Mapping")
    async def admin_disable_identity_mapping(mapping_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        _require_admin(current_user)
        disabled = await service.disable_as_admin(mapping_id, actor_user_id=current_user.id)
        if not disabled:
            raise _hide_missing_or_cross_owner()
        return {"disabled": True, "id": mapping_id}

    return router
