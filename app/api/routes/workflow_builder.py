from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from typing import Any, Literal, Optional

from app.api.context import ApiContext, get_default_api_context
from app.services.workflow_builder import WorkflowBuilderService


class WorkflowBuilderGenerateRequest(BaseModel):
    draft_type: Literal["tasks", "agents", "workflow"]
    conversation_history: str | None = None
    latest_instruction: str | None = None
    latest_tasks: str | None = None
    tasks: list[dict[str, Any]] | None = None
    agents: list[dict[str, Any]] | None = None
    model_profile_id: str | None = None


class WorkflowBuilderAgentRewriteRequest(BaseModel):
    agent: dict[str, Any]
    model_profile_id: str | None = None


class WorkflowBuilderTaskRewriteRequest(BaseModel):
    task: dict[str, Any]
    model_profile_id: str | None = None


def create_workflow_builder_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/workflow-builder", tags=["Workflow Builder"])
    service = WorkflowBuilderService(context)

    @router.post("/drafts/generate", summary="Generate Workflow Builder Draft")
    async def generate_workflow_builder_draft(payload: WorkflowBuilderGenerateRequest):
        try:
            return await service.generate_draft(
                payload.draft_type,
                conversation_history=payload.conversation_history,
                latest_instruction=payload.latest_instruction,
                latest_tasks=payload.latest_tasks,
                tasks=payload.tasks,
                agents=payload.agents,
                model_profile_id=payload.model_profile_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post("/rewrite/agent", summary="Rewrite Agent Draft")
    async def rewrite_workflow_builder_agent(payload: WorkflowBuilderAgentRewriteRequest):
        try:
            return {"data": await service.rewrite_agent(payload.agent, model_profile_id=payload.model_profile_id)}
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post("/rewrite/task", summary="Rewrite Task Draft")
    async def rewrite_workflow_builder_task(payload: WorkflowBuilderTaskRewriteRequest):
        try:
            return {"data": await service.rewrite_task(payload.task, model_profile_id=payload.model_profile_id)}
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return router
