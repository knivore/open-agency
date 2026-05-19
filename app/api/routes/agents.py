from __future__ import annotations

from fastapi import APIRouter, Request
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.domain import AgentDefinition
from app.services import AgentService
from ._crud import build_crud_router


def create_agents_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = AgentService(context)
    router = build_crud_router(
        prefix="/agents",
        tag="Agents",
        summary_name="Agent",
        repo=context.agent_repo,
        model_cls=AgentDefinition,
        context=context,
        read_scopes=["agents:read"],
        write_scopes=["agents:write"],
    )

    @router.get("/{agent_id}/executions", summary="List Executions For Agent")
    async def list_agent_executions(agent_id: str, request: Request):
        await resolve_current_user(request, context, required_scopes=["executions:read"])
        return await service.list_agent_executions(agent_id)

    return router
