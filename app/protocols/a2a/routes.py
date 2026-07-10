"""FastAPI routes for the Agent2Agent compatibility boundary.

A2A is intentionally an adapter over Agency's canonical execution store. The
routes below expose an agent card, create tasks by creating normal executions,
append A2A messages as execution events, and read artifacts from the same store
used by native Agency runs.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.runtime.native.errors import WorkflowNotFoundError
from app.services.executions import ExecutionService
from .adapter import A2AAdapter
from .agent_card import agent_definition_to_card
from .artifacts import execution_artifact_to_a2a_artifact
from .messages import A2AMessageCreate, execution_event_to_a2a_message
from .tasks import A2ATaskCreate, execution_to_a2a_task


def create_a2a_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    adapter = A2AAdapter(execution_store=context.execution_store)
    execution_service = ExecutionService(context)
    router = APIRouter(tags=["A2A"])

    @router.get("/.well-known/agent-card.json", summary="Get A2A Agent Card")
    async def get_agent_card(request: Request):
        agents = await context.agent_repo.list()
        if agents:
            agent = agents[0]
        else:
            agent = (await context.workflow_repo.list())[0].agent_definitions[
                0] if await context.workflow_repo.list() else None
        if agent is None:
            return {
                "name": "agency",
                "description": "A2A endpoint",
                "capabilities": [],
                "input_modes": ["text", "json"],
                "output_modes": ["text", "artifact", "json"],
                "skills": [],
                "endpoint": str(request.base_url).rstrip("/") + "/a2a/tasks",
            }
        return agent_definition_to_card(agent, base_url=str(request.base_url).rstrip(" /"), endpoint_path="/a2a/tasks")

    @router.post("/a2a/tasks", summary="Create A2A Task")
    async def create_a2a_task(payload: A2ATaskCreate):
        workflow_id = payload.workflow_id
        if workflow_id is None and payload.agent_id is not None:
            workflows = await context.workflow_repo.list()
            for workflow in workflows:
                if any(agent.id == payload.agent_id for agent in workflow.agent_definitions):
                    workflow_id = workflow.id
                    break
        if workflow_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="workflowId or agentId is required")
        try:
            execution = await execution_service.create_execution(
                workflow_id=workflow_id,
                input_payload=payload.input,
                trigger=payload.trigger or {"created_by": "a2a"},
                runtime_adapter_id=payload.runtime_adapter_id,
            )
        except (WorkflowNotFoundError, KeyError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        if payload.message is not None:
            await adapter.append_message(execution["id"], payload.message)
        return execution_to_a2a_task((await context.execution_store.get_execution(execution["id"])))

    @router.get("/a2a/tasks/{task_id}", summary="Get A2A Task")
    async def get_a2a_task(task_id: str):
        task = await adapter.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"A2A task '{task_id}' was not found")
        return task

    @router.post("/a2a/tasks/{task_id}/messages", summary="Post A2A Task Message")
    async def post_a2a_message(task_id: str, payload: A2AMessageCreate):
        execution = await context.execution_store.get_execution(task_id)
        if execution is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"A2A task '{task_id}' was not found")
        event = await adapter.append_message(task_id, payload.model_dump(mode="json"))
        artifact = None
        if payload.artifact is not None:
            artifact = await adapter.append_artifact(task_id, payload.artifact)
        return {
            "message": execution_event_to_a2a_message(event),
            "artifact": execution_artifact_to_a2a_artifact(artifact) if artifact else None,
        }

    @router.get("/a2a/tasks/{task_id}/artifacts", summary="List A2A Task Artifacts")
    async def list_a2a_artifacts(task_id: str):
        execution = await context.execution_store.get_execution(task_id)
        if execution is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"A2A task '{task_id}' was not found")
        artifacts = await context.execution_store.list_artifacts(task_id)
        return {"items": [execution_artifact_to_a2a_artifact(artifact) for artifact in artifacts]}

    return router
