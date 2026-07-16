"""API-token management routes and token-secret handling."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.domain import API_TOKEN_SCOPE_DEFINITIONS, ApiTokenDefinition, ApiTokenPublicDefinition
from ._crud import serializable_validation_errors

TOKEN_PREFIX = "agt"
ALLOWED_API_TOKEN_SCOPES = {scope.id for scope in API_TOKEN_SCOPE_DEFINITIONS}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return f"{TOKEN_PREFIX}_{secrets.token_urlsafe(32)}"


def public_token(token: ApiTokenDefinition) -> ApiTokenPublicDefinition:
    return ApiTokenPublicDefinition.model_validate(token.model_dump(mode="json", exclude={"token_hash"}))


def hide_missing_or_cross_owner() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API token not found")


def normalize_requested_scopes(payload: dict[str, Any]) -> list[str]:
    raw_scopes = payload.get("scopes") or []
    if not isinstance(raw_scopes, list):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail="Scopes must be an array of strings")

    normalized = [str(scope).strip() for scope in raw_scopes if str(scope).strip()]
    invalid = [scope for scope in normalized if scope not in ALLOWED_API_TOKEN_SCOPES]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Unsupported API token scopes were provided.",
                "invalidScopes": invalid,
                "allowedScopes": sorted(ALLOWED_API_TOKEN_SCOPES),
            },
        )
    return normalized


def create_api_tokens_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/api-tokens", tags=["API Tokens"])

    @router.get("/scopes", summary="List API Token Scopes")
    async def list_token_scopes(request: Request):
        await resolve_current_user(request, context)
        return {"items": [scope.model_dump(mode="json") for scope in API_TOKEN_SCOPE_DEFINITIONS]}

    @router.post("", summary="Create API Token")
    async def create_token(payload: dict[str, Any], request: Request):
        current_user = await resolve_current_user(request, context)
        raw_token = generate_token()
        token_hash = hash_token(raw_token)
        scopes = normalize_requested_scopes(payload)
        caller_token = getattr(request.state, "authenticated_api_token", None)
        if caller_token is not None and not set(scopes).issubset(set(caller_token.scopes)):
            # Delegated tokens must never amplify their own authority by minting
            # a replacement with broader scopes.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API tokens may only delegate scopes they already hold",
            )
        try:
            definition = ApiTokenDefinition.model_validate(
                {
                    "owner_user_id": current_user.id,
                    "name": payload.get("name") or "API token",
                    "token_hash": token_hash,
                    "prefix": raw_token[:8],
                    "last4": raw_token[-4:],
                    "scopes": scopes,
                    "expires_at": payload.get("expires_at"),
                    "metadata": payload.get("metadata") or {},
                }
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        created = await context.api_token_repo.create(definition)
        context.runtime_operations.record_action(
            "api_token.created",
            token_id=created.id,
            owner_user_id=created.owner_user_id,
            name=created.name,
            scopes=created.scopes,
            prefix=created.prefix,
            last4=created.last4,
        )
        response = public_token(created).model_dump(mode="json")
        response["token"] = raw_token
        return response

    @router.get("", summary="List API Tokens")
    async def list_tokens(request: Request):
        current_user = await resolve_current_user(request, context)
        if hasattr(context.api_token_repo, "list_by_owner"):
            tokens = await context.api_token_repo.list_by_owner(current_user.id)
        else:
            tokens = [
                token
                for token in await context.api_token_repo.list()
                if token.owner_user_id == current_user.id
            ]
        return {"items": [public_token(token).model_dump(mode="json") for token in tokens]}

    @router.get("/{token_id}", summary="Get API Token By Id")
    async def get_token(token_id: str, request: Request):
        current_user = await resolve_current_user(request, context)
        token = await context.api_token_repo.get(token_id)
        if token is None or token.owner_user_id != current_user.id:
            raise hide_missing_or_cross_owner()
        return public_token(token).model_dump(mode="json")

    @router.post("/{token_id}/revoke", summary="Revoke API Token")
    async def revoke_token(token_id: str, request: Request):
        current_user = await resolve_current_user(request, context)
        token = await context.api_token_repo.get(token_id)
        if token is None or token.owner_user_id != current_user.id:
            raise hide_missing_or_cross_owner()
        revoked = await context.api_token_repo.update(
            token_id,
            {"revoked_at": datetime.now(timezone.utc)},
        )
        if revoked is None:
            raise hide_missing_or_cross_owner()
        context.runtime_operations.record_action(
            "api_token.revoked",
            token_id=revoked.id,
            owner_user_id=revoked.owner_user_id,
            name=revoked.name,
            scopes=revoked.scopes,
            prefix=revoked.prefix,
            last4=revoked.last4,
            revoked_at=revoked.revoked_at.isoformat() if revoked.revoked_at else None,
        )
        return public_token(revoked).model_dump(mode="json")

    return router
