from __future__ import annotations

import json
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.domain import ConnectorCredentialValidationPayload
from app.integrations import get_connector_definition
from app.services import CredentialService
from ._crud import serializable_validation_errors

RAW_SECRET_PAYLOAD_KEYS = {"secret", "raw_secret", "value", "token", "password", "api_key"}


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


def _reject_raw_secret_payload(payload: dict[str, Any]) -> None:
    raw_keys = RAW_SECRET_PAYLOAD_KEYS.intersection({key.lower() for key in payload})
    if raw_keys:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Raw secret material must be stored in the secret store and referenced by secret_ref",
        )


def _raw_secret_payload_errors(payload: dict[str, Any]) -> list[str]:
    raw_keys = sorted(RAW_SECRET_PAYLOAD_KEYS.intersection({key.lower() for key in payload}))
    if not raw_keys:
        return []
    return ["Raw secret material must be stored in the secret store and referenced by secret_ref"]


def create_credentials_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = CredentialService(context)
    router = APIRouter(prefix="/credentials", tags=["Credentials"])

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
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        return created.model_dump(mode="json")

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
        return created.model_dump(mode="json")

    @router.get("", summary="List Credentials")
    async def list_credentials(request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        items = await service.list_credentials_for_owner(current_user.id)
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.get("/{credential_id}", summary="Get Credential By Id")
    async def get_credential(credential_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        item = await service.get_credential_for_owner(credential_id, current_user.id)
        if item is None:
            raise _hide_missing_or_cross_owner()
        return item.model_dump(mode="json")

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
        if item is None:
            raise _hide_missing_or_cross_owner()
        return item.model_dump(mode="json")

    @router.put("/{credential_id}/connector", summary="Update Connector Credential")
    async def update_connector_credential(credential_id: str, patch: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            item = await service.update_connector_credential_for_owner(
                credential_id=credential_id,
                owner_user_id=current_user.id,
                patch=patch,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        if item is None:
            provider_key = patch.get("provider")
            if provider_key is not None and service.resolve_connector_capability(str(provider_key))[0] is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail=f"Connector '{provider_key}' not found")
            raise _hide_missing_or_cross_owner()
        return item.model_dump(mode="json")

    @router.post("/{credential_id}/revoke", summary="Revoke Credential")
    async def revoke_credential(credential_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        item = await service.revoke_credential_for_owner(credential_id, current_user.id)
        if item is None:
            raise _hide_missing_or_cross_owner()
        return item.model_dump(mode="json")

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
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        if item is None:
            raise _hide_missing_or_cross_owner()
        return item.model_dump(mode="json")

    @router.delete("/{credential_id}", summary="Delete Credential")
    async def delete_credential(credential_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        deleted = await service.delete_credential_for_owner(credential_id, current_user.id)
        if not deleted:
            raise _hide_missing_or_cross_owner()
        return {"deleted": True, "id": credential_id}

    return router
