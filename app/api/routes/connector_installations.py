"""Backend-owned connector installation setup routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.domain import ConnectorInstallation, ConnectorSetupSessionPayload
from app.services.connector_installations import ConnectorInstallationService
from ._crud import serializable_validation_errors


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector installation not found")


def create_connector_installations_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = ConnectorInstallationService(context)
    router = APIRouter(prefix="/integrations/connectors", tags=["Connector Installations"])

    @router.post(
        "/{provider_key}/setup-sessions",
        response_model=ConnectorSetupSessionPayload,
        summary="Create Connector Setup Session",
    )
    async def create_setup_session(provider_key: str, payload: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            return await service.create_setup_session(
                provider_key=provider_key,
                payload=payload,
                owner_user_id=current_user.id,
            )
        except LookupError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.get(
        "/installations",
        summary="List Connector Installations",
    )
    async def list_installations(request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        items = await service.list_for_owner(current_user.id)
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.get(
        "/installations/{installation_id}",
        response_model=ConnectorInstallation,
        summary="Get Connector Installation",
    )
    async def get_installation(installation_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        item = await service.get_for_owner(installation_id, current_user.id)
        if item is None:
            raise _not_found()
        return item

    @router.get(
        "/installations/{installation_id}/setup-session",
        response_model=ConnectorSetupSessionPayload,
        summary="Resume Connector Setup Session",
    )
    async def resume_setup_session(installation_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        try:
            item = await service.resume_setup_session_for_owner(installation_id, current_user.id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if item is None:
            raise _not_found()
        return item

    @router.post(
        "/installations/{installation_id}/complete",
        response_model=ConnectorInstallation,
        summary="Complete Connector Setup Session",
    )
    async def complete_installation(installation_id: str, payload: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            item = await service.complete_for_owner(
                installation_id=installation_id,
                owner_user_id=current_user.id,
                payload=payload,
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if item is None:
            raise _not_found()
        return item

    @router.post(
        "/installations/{installation_id}/test",
        summary="Test Connector Installation",
    )
    async def test_installation(installation_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        result = await service.test_for_owner(installation_id, current_user.id)
        if result is None:
            raise _not_found()
        return result

    @router.post(
        "/installations/{installation_id}/rotate",
        response_model=ConnectorSetupSessionPayload,
        summary="Create Connector Rotation Setup Session",
    )
    async def rotate_installation(installation_id: str, payload: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            item = await service.rotate_for_owner(
                installation_id=installation_id,
                owner_user_id=current_user.id,
                payload=payload,
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if item is None:
            raise _not_found()
        return item

    @router.delete(
        "/installations/{installation_id}",
        response_model=ConnectorInstallation,
        summary="Revoke Connector Installation",
    )
    async def revoke_installation(installation_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        item = await service.revoke_for_owner(installation_id, current_user.id)
        if item is None:
            raise _not_found()
        return item

    return router
