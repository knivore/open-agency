"""Shared CRUD router factory for catalog-style resources.

The concrete route modules pass repositories, domain models, scopes, and optional
hooks here. Keep resource-specific behavior in those modules and reserve this
file for common validation, redaction, ownership, and response-shaping behavior.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from typing import Any, Awaitable, Callable, Iterable, Optional

from app.api.context import ApiContext
from app.api.identity import resolve_current_user, resolve_current_user_if_present

REDACTED_SECRET_VALUE = "[REDACTED]"
SECRET_RESPONSE_KEYS = {
    "api_key",
    "apikey",
    "api_key_ref",
    "access_token",
    "refresh_token",
    "token",
    "password",
    "secret",
    "authorization",
}


def serializable_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    serializable = []
    for item in exc.errors():
        entry = dict(item)
        ctx = entry.get("ctx")
        if isinstance(ctx, dict):
            entry["ctx"] = {
                key: str(value) if isinstance(value, Exception) else value
                for key, value in ctx.items()
            }
        serializable.append(entry)
    return serializable


def redact_secret_fields(value: Any) -> Any:
    if isinstance(value, list):
        return [redact_secret_fields(item) for item in value]
    if not isinstance(value, dict):
        return value

    redacted: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = key.strip().lower()
        if normalized_key in SECRET_RESPONSE_KEYS and item:
            redacted[key] = REDACTED_SECRET_VALUE
        else:
            redacted[key] = redact_secret_fields(item)
    return redacted


def strip_redacted_secret_updates(value: Any) -> Any:
    if isinstance(value, list):
        return [strip_redacted_secret_updates(item) for item in value]
    if not isinstance(value, dict):
        return value

    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = key.strip().lower()
        if normalized_key in SECRET_RESPONSE_KEYS and item == REDACTED_SECRET_VALUE:
            continue
        cleaned[key] = strip_redacted_secret_updates(item)
    return cleaned


def dump_response(item: Any) -> dict[str, Any]:
    return redact_secret_fields(item.model_dump(mode="json"))


def build_crud_router(
        *,
        prefix: str,
        tag: str,
        summary_name: str,
        repo: Any,
        model_cls: Any,
        context: ApiContext | None = None,
        read_scopes: Optional[Iterable[str]] = None,
        write_scopes: Optional[Iterable[str]] = None,
        protected_ids: Optional[Iterable[str]] = None,
        require_read_auth: bool = False,
        require_write_auth: bool = False,
        before_list: Callable[[], Awaitable[None]] | None = None,
        response_filter: Callable[[Any], bool] | None = None,
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[tag])
    protected = set(protected_ids or [])
    read_scope_list = list(read_scopes or [])
    write_scope_list = list(write_scopes or [])

    @router.post("", summary=f"Create {summary_name}")
    async def create_item(payload: dict[str, Any], request: Request):
        if context is not None:
            if require_write_auth:
                await resolve_current_user(request, context, required_scopes=write_scope_list)
            else:
                await resolve_current_user_if_present(request, context, required_scopes=write_scope_list)
        try:
            created = await repo.create(model_cls.model_validate(payload))
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        return dump_response(created)

    @router.get("", summary=f"List {summary_name}")
    async def list_items(request: Request):
        if context is not None:
            if require_read_auth:
                await resolve_current_user(request, context, required_scopes=read_scope_list)
            else:
                await resolve_current_user_if_present(request, context, required_scopes=read_scope_list)
        if before_list is not None:
            await before_list()
        items = await repo.list()
        if response_filter is not None:
            items = [item for item in items if response_filter(item)]
        return {"items": [dump_response(item) for item in items]}

    @router.get("/{item_id}", summary=f"Get {summary_name} By Id")
    async def get_item(item_id: str, request: Request):
        if context is not None:
            if require_read_auth:
                await resolve_current_user(request, context, required_scopes=read_scope_list)
            else:
                await resolve_current_user_if_present(request, context, required_scopes=read_scope_list)
        item = await repo.get(item_id)
        if item is None or (response_filter is not None and not response_filter(item)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{summary_name} '{item_id}' not found")
        return dump_response(item)

    @router.put("/{item_id}", summary=f"Update {summary_name}")
    async def update_item(item_id: str, patch: dict[str, Any], request: Request):
        if context is not None:
            if require_write_auth:
                await resolve_current_user(request, context, required_scopes=write_scope_list)
            else:
                await resolve_current_user_if_present(request, context, required_scopes=write_scope_list)
        if item_id in protected:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"{summary_name} '{item_id}' is protected")
        try:
            item = await repo.update(item_id, strip_redacted_secret_updates(patch))
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{summary_name} '{item_id}' not found")
        return dump_response(item)

    @router.delete("/{item_id}", summary=f"Soft Delete {summary_name}")
    async def delete_item(item_id: str, request: Request):
        if context is not None:
            if require_write_auth:
                await resolve_current_user(request, context, required_scopes=write_scope_list)
            else:
                await resolve_current_user_if_present(request, context, required_scopes=write_scope_list)
        if item_id in protected:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"{summary_name} '{item_id}' is protected")
        deleted = await repo.soft_delete(item_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{summary_name} '{item_id}' not found")
        return {"deleted": True, "id": item_id}

    return router
