"""Main-agent monitor command center routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user, resolve_current_user_if_present
from app.services.workflows import WorkflowService


def create_main_agent_monitor_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/main-agent/monitor", tags=["Main Agent Monitor"])
    service = WorkflowService(context)

    @router.get("", summary="Get Main-Agent Monitor Command Center")
    async def get_main_agent_monitor(request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["workflows:read"])
        return await service.main_agent_monitor_command_center()

    @router.patch("/routes", summary="Update Main-Agent Monitor Notification Routes")
    async def update_main_agent_monitor_routes(patch: dict[str, Any], request: Request):
        await resolve_current_user(request, context, required_scopes=["workflows:write"])
        try:
            return await service.update_main_agent_monitor_routes(patch)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return router
