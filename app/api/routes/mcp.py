"""MCP server catalog routes and discovery trigger."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.domain import MCPServerDefinition
from app.services.models import ModelCatalogService
from ._crud import build_crud_router


def create_mcp_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = ModelCatalogService(context)
    router = build_crud_router(
        prefix="/mcp-servers",
        tag="MCP Servers",
        summary_name="MCP Server",
        repo=context.mcp_server_repo,
        model_cls=MCPServerDefinition,
        context=context,
        read_scopes=["integrations:read"],
        write_scopes=["integrations:write"],
    )

    @router.post("/discover", summary="Discover MCP Tools, Resources, And Prompts")
    async def discover_mcp_servers(request: Request, payload: dict[str, Any] | None = None):
        await resolve_current_user_if_present(request, context, required_scopes=["integrations:write"])
        server_id = payload.get("serverId") if payload else None
        try:
            return await service.sync_mcp_catalog(server_id=server_id)
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return router


from app.api.identity import resolve_current_user_if_present
