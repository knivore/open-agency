"""Credential storage routes with owner isolation and raw-secret rejection."""

from __future__ import annotations

import json
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.domain import ConnectorCredentialValidationPayload
from app.integrations.connectors import get_connector_definition
from app.services.credentials import CredentialService
from ._crud import serializable_validation_errors

def _hide_missing_or_cross_owner() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")


async def _read_optional_json_payload(request: Request) -> dict[str, Any]:
    body = await request.body()
    if not body:
        return {}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payload must be a JSON object")
    return payload


def create_credentials_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = CredentialService(context)
    router = APIRouter(prefix="/credentials", tags=["Credentials"])

    @router.post("/connectors/resolve", summary="Resolve Connector Credential")
    async def resolve_connector_credential(payload: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        provider = payload.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="provider is required")
        filters = payload.get("filters")
        if filters is not None and not isinstance(filters, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="filters must be an object")
        credential_status = payload.get("status")
        if credential_status is not None and not isinstance(credential_status, str):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status must be a string")
        result = await service.resolve_connector_credential_for_owner(
            owner_user_id=current_user.id,
            provider_key=provider,
            filters=filters,
            status=credential_status if isinstance(credential_status, str) else "active",
        )
        if result.get("status") == "error":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=result.get("error"))
        return result

    @router.get("/connectors/{provider_key}/schema", summary="Get Connector Credential Schema")
    async def get_connector_schema(provider_key: str):
        canonical, capability = service.resolve_connector_capability(provider_key)
        if canonical is None or get_connector_definition(canonical) is None or capability is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector '{provider_key}' not found")
        return capability.model_dump(mode="json")

    @router.post(
        "/connectors/{provider_key}/validate",
        response_model=ConnectorCredentialValidationPayload,
        summary="Validate Connector Credential Payload",
    )
    async def validate_connector_credential(provider_key: str, payload: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        result = await service.validate_connector_payload(
            provider_key=provider_key,
            payload=payload,
            owner_user_id=current_user.id,
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector '{provider_key}' not found")
        return result

    @router.post("/connectors/{provider_key}", summary="Create Connector Credential")
    async def create_connector_credential(provider_key: str, payload: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            created = await service.create_connector_credential(
                provider_key=provider_key,
                payload=payload,
                owner_user_id=current_user.id,
            )
            if created is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail=f"Connector '{provider_key}' not found")
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return service.credential_api_payload(created)

    @router.post("", summary="Create Credential")
    async def create_credential(payload: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            created = await service.create_credential(payload=payload, owner_user_id=current_user.id)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return service.credential_api_payload(created)

    @router.get("", summary="List Credentials")
    async def list_credentials(request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        items = await service.list_credentials_for_owner(current_user.id)
        return {"items": [service.credential_api_payload(item) for item in items]}

    @router.get("/{credential_id}", summary="Get Credential By Id")
    async def get_credential(credential_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        item = await service.get_credential_for_owner(credential_id, current_user.id)
        if item is None:
            raise _hide_missing_or_cross_owner()
        return service.credential_api_payload(item)

    @router.put("/{credential_id}", summary="Update Credential")
    async def update_credential(credential_id: str, patch: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            item = await service.update_credential_for_owner(
                credential_id=credential_id,
                owner_user_id=current_user.id,
                patch=patch,
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if item is None:
            raise _hide_missing_or_cross_owner()
        return service.credential_api_payload(item)

    @router.put("/{credential_id}/connector", summary="Update Connector Credential")
    async def update_connector_credential(credential_id: str, patch: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            item = await service.update_connector_credential_for_owner(
                credential_id=credential_id,
                owner_user_id=current_user.id,
                patch=patch,
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if item is None:
            provider_key = patch.get("provider")
            if provider_key is not None and service.resolve_connector_capability(str(provider_key))[0] is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail=f"Connector '{provider_key}' not found")
            raise _hide_missing_or_cross_owner()
        return service.credential_api_payload(item)

    @router.post("/{credential_id}/revoke", summary="Revoke Credential")
    async def revoke_credential(credential_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        item = await service.revoke_credential_for_owner(credential_id, current_user.id)
        if item is None:
            raise _hide_missing_or_cross_owner()
        return service.credential_api_payload(item)

    @router.post("/{credential_id}/rotate", summary="Mark Credential Rotated")
    async def rotate_credential(credential_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        payload = await _read_optional_json_payload(request)
        try:
            item = await service.rotate_credential_for_owner(
                credential_id=credential_id,
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
            raise _hide_missing_or_cross_owner()
        return service.credential_api_payload(item)

    @router.delete("/{credential_id}", summary="Delete Credential")
    async def delete_credential(credential_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        deleted = await service.delete_credential_for_owner(credential_id, current_user.id)
        if not deleted:
            raise _hide_missing_or_cross_owner()
        return {"deleted": True, "id": credential_id}

    return router
