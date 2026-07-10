"""Connector health, history, and retention API routes."""

from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Request, status
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.domain import ConnectorHealthHistoryPayload, ConnectorHealthHistoryPrunePayload
from app.services.connectors import ConnectorService


def create_connectors_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = ConnectorService(context)
    router = APIRouter()

    @router.get("/integrations/connectors/{credential_id}/health", summary="Test Connector Credential Health")
    async def test_connector_credential_health(credential_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        result = await service.test_credential_for_owner(credential_id, current_user.id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Credential '{credential_id}' not found")
        return result

    @router.post("/integrations/connectors/{credential_id}/test", summary="Test Connector Credential")
    async def test_connector_credential(credential_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        result = await service.test_credential_for_owner(credential_id, current_user.id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Credential '{credential_id}' not found")
        return result

    @router.get(
        "/integrations/connectors/history",
        response_model=ConnectorHealthHistoryPayload,
        summary="List Connector Test History",
    )
    async def list_connector_history(
            request: Request,
            limit: int = Query(default=20, ge=1, le=100),
            offset: int = Query(default=0, ge=0),
            status_filter: str | None = Query(default=None, alias="status"),
            started_after: datetime | None = Query(default=None, alias="started_after"),
            started_before: datetime | None = Query(default=None, alias="started_before"),
            provider: str | None = Query(default=None),
    ):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        return await service.list_all_history_for_owner(
            current_user.id,
            limit=limit,
            offset=offset,
            status=status_filter,
            started_after=started_after,
            started_before=started_before,
            provider=provider,
        )

    @router.delete(
        "/integrations/connectors/history",
        response_model=ConnectorHealthHistoryPrunePayload,
        summary="Prune Connector Test History",
    )
    async def prune_connector_history(
            request: Request,
            status_filter: str | None = Query(default=None, alias="status"),
            started_before: datetime | None = Query(default=None, alias="started_before"),
            provider: str | None = Query(default=None),
            keep_latest: int | None = Query(default=None, alias="keep_latest", ge=0, le=1000),
    ):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        return await service.prune_all_history_for_owner(
            current_user.id,
            status=status_filter,
            started_before=started_before,
            provider=provider,
            keep_latest=keep_latest,
        )

    @router.get(
        "/integrations/connectors/{credential_id}/history",
        response_model=ConnectorHealthHistoryPayload,
        summary="List Connector Credential Test History",
    )
    async def list_connector_credential_history(
            credential_id: str,
            request: Request,
            limit: int = Query(default=20, ge=1, le=100),
            offset: int = Query(default=0, ge=0),
            status_filter: str | None = Query(default=None, alias="status"),
            started_after: datetime | None = Query(default=None, alias="started_after"),
            started_before: datetime | None = Query(default=None, alias="started_before"),
    ):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:read"])
        result = await service.list_credential_history_for_owner(
            credential_id,
            current_user.id,
            limit=limit,
            offset=offset,
            status=status_filter,
            started_after=started_after,
            started_before=started_before,
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Credential '{credential_id}' not found")
        return result

    @router.delete(
        "/integrations/connectors/{credential_id}/history",
        response_model=ConnectorHealthHistoryPrunePayload,
        summary="Prune Connector Credential Test History",
    )
    async def prune_connector_credential_history(
            credential_id: str,
            request: Request,
            status_filter: str | None = Query(default=None, alias="status"),
            started_before: datetime | None = Query(default=None, alias="started_before"),
            keep_latest: int | None = Query(default=None, alias="keep_latest", ge=0, le=1000),
    ):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        result = await service.prune_credential_history_for_owner(
            credential_id,
            current_user.id,
            status=status_filter,
            started_before=started_before,
            keep_latest=keep_latest,
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Credential '{credential_id}' not found")
        return result

    return router
