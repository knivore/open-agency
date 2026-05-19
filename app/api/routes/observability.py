from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.observability.service import ObservabilityService
from app.runtime.native.errors import ExecutionNotFoundError


def create_observability_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = ObservabilityService(context)
    router = APIRouter(prefix="/observability", tags=["Observability"])

    @router.get("/executions/{execution_id}/timeline", summary="Get Execution Timeline")
    async def get_execution_timeline(execution_id: str):
        try:
            return await service.get_execution_timeline(execution_id)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/agents/{agent_id}/metrics", summary="Get Agent Metrics")
    async def get_agent_metrics(agent_id: str):
        return await service.get_agent_metrics(agent_id)

    @router.get("/workflows/{workflow_id}/metrics", summary="Get Workflow Metrics")
    async def get_workflow_metrics(workflow_id: str):
        return await service.get_workflow_metrics(workflow_id)

    @router.get("/models/usage", summary="Get Model Usage")
    async def get_model_usage():
        return await service.get_model_usage()

    return router
