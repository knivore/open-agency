"""Execution creation, control, events, artifacts, and runtime operations routes."""

from __future__ import annotations

import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import AliasChoices, BaseModel, Field
from starlette.responses import JSONResponse, StreamingResponse
from typing import Any, Literal, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import request_has_identity, resolve_current_user
from app.core.config import get_settings
from app.domain import (
    ExecutionWaitKind,
    ExecutionWaitStatus,
    ModelProfileDefinition,
    UserDefinition,
    WorkflowDefinition,
)
from app.runtime.containers import ContainerRuntimeError
from app.runtime.native.errors import ExecutionNotFoundError, WorkflowNotFoundError
from app.services.executions import ExecutionService
from app.services.execution_waits import (
    ExecutionWaitConflictError,
    ExecutionWaitNotFoundError,
    ExecutionWaitService,
)
from app.services.goals import GoalNotFoundError


class CreateExecutionRequest(BaseModel):
    workflow_id: str = Field(alias="workflowId")
    goal_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("goalId", "goal_id"))
    input: dict[str, Any] = Field(default_factory=dict)
    trigger: dict[str, Any] = Field(default_factory=dict)
    context_pack_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("contextPackId", "context_pack_id"),
    )
    runtime_adapter_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("runtimeAdapterId", "runtime_adapter_id"),
    )
    execution_host: Optional[Literal["local", "docker"]] = Field(
        default=None,
        validation_alias=AliasChoices("executionHost", "execution_host"),
    )
    workflow_definition: Optional[WorkflowDefinition] = None
    model_profiles: list[ModelProfileDefinition] = Field(default_factory=list)


class WorkflowExecutionRequest(BaseModel):
    goal_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("goalId", "goal_id"))
    input: dict[str, Any] = Field(default_factory=dict)
    trigger: dict[str, Any] = Field(default_factory=dict)
    context_pack_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("contextPackId", "context_pack_id"),
    )
    runtime_adapter_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("runtimeAdapterId", "runtime_adapter_id"),
    )
    execution_host: Optional[Literal["local", "docker"]] = Field(
        default=None,
        validation_alias=AliasChoices("executionHost", "execution_host"),
    )
    workflow_definition: Optional[WorkflowDefinition] = None
    model_profiles: list[ModelProfileDefinition] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    tool_id: str = Field(alias="toolId")
    reason: Optional[str] = None


class TaskRetryRequest(BaseModel):
    reason: Optional[str] = None


class CheckpointResumeRequest(BaseModel):
    reason: Optional[str] = None


class CreateExecutionWaitRequest(BaseModel):
    kind: ExecutionWaitKind
    idempotency_key: str = Field(min_length=1, max_length=255)
    correlation_key: Optional[str] = Field(default=None, max_length=255)
    checkpoint: dict[str, Any] | None = None
    request_payload: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    wake_at: Optional[datetime] = None
    deadline_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResolveExecutionWaitRequest(BaseModel):
    resolution_key: str = Field(min_length=1, max_length=255)
    resolution_payload: dict[str, Any] = Field(default_factory=dict)
    status: ExecutionWaitStatus = ExecutionWaitStatus.RESOLVED
    resume: bool = True


class WakeExecutionEventRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=255)
    event_payload: dict[str, Any] = Field(default_factory=dict)


def _workflow_owner_ids(workflow: WorkflowDefinition) -> list[str]:
    value = workflow.metadata.get("owner_ids")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _is_workflow_owner_or_admin(workflow: WorkflowDefinition, user: UserDefinition) -> bool:
    if "admin" in user.roles:
        return True
    created_by = workflow.metadata.get("created_by")
    return user.id in _workflow_owner_ids(workflow) or created_by == user.id


def _has_explicit_workflow_owner(workflow: WorkflowDefinition) -> bool:
    return bool(_workflow_owner_ids(workflow) or workflow.metadata.get("created_by"))


async def _ensure_workflow_run_access(
        workflow_id: str,
        workflow: WorkflowDefinition,
        user: UserDefinition,
        context: ApiContext,
) -> WorkflowDefinition:
    if _is_workflow_owner_or_admin(workflow, user):
        return workflow
    if _has_explicit_workflow_owner(workflow):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workflow owner access is required")
    metadata = dict(workflow.metadata)
    metadata["created_by"] = user.id
    metadata["owner_ids"] = [user.id]
    updated = await context.workflow_repo.update(workflow_id, {"metadata": metadata})
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
    return updated


async def _require_workflow_run_access(workflow_id: str, request: Request, context: ApiContext) -> WorkflowDefinition:
    current_user = await resolve_current_user(request, context, required_scopes=["workflows:run"])
    workflow = await context.workflow_repo.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
    return await _ensure_workflow_run_access(workflow_id, workflow, current_user, context)


async def _require_workflow_run_access_for_request(
        workflow_id: str,
        request: Request,
        context: ApiContext,
        *,
        workflow_definition: WorkflowDefinition | None = None,
        current_user: UserDefinition | None = None,
) -> WorkflowDefinition | None:
    current_user = current_user or await resolve_current_user(request, context, required_scopes=["workflows:run"])
    if workflow_definition is not None:
        owner_ids = _workflow_owner_ids(workflow_definition)
        created_by = workflow_definition.metadata.get("created_by")
        if owner_ids or created_by:
            if not _is_workflow_owner_or_admin(workflow_definition, current_user):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workflow owner access is required")
        return workflow_definition
    return await _require_workflow_run_access(workflow_id, request, context)


async def _resolve_execution_user(
        request: Request,
        context: ApiContext,
        *,
        scopes: list[str],
) -> UserDefinition | None:
    # Legacy unit fixtures call route functions without identity headers. Keep
    # that convenience confined to APP_ENV=test; deployed APIs are fail-closed.
    if get_settings().app_env == "test" and not request_has_identity(request):
        return None
    return await resolve_current_user(request, context, required_scopes=scopes)


async def _require_execution_access(
        execution_id: str,
        request: Request,
        context: ApiContext,
        *,
        scopes: list[str],
) -> UserDefinition | None:
    user = await _resolve_execution_user(request, context, scopes=scopes)
    if user is None:
        return None
    execution = await context.execution_store.get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Execution '{execution_id}' not found")
    if "admin" in user.roles or execution.created_by == user.id:
        return user
    workflow = await context.workflow_repo.get(execution.workflow_id)
    if workflow is not None and _is_workflow_owner_or_admin(workflow, user):
        return user
    # Avoid confirming execution identifiers across owner boundaries.
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Execution '{execution_id}' not found")


async def _require_runtime_admin(request: Request, context: ApiContext, *, scopes: list[str]) -> UserDefinition | None:
    user = await _resolve_execution_user(request, context, scopes=scopes)
    if user is not None and "admin" not in user.roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role is required")
    return user


def create_executions_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = ExecutionService(context)
    wait_service = ExecutionWaitService(context)
    router = APIRouter(tags=["Executions"])
    execution_router = APIRouter(prefix="/executions", tags=["Executions"])

    @execution_router.get("", summary="List Executions")
    async def list_executions(request: Request):
        user = await _resolve_execution_user(request, context, scopes=["executions:read"])
        result = await service.list_executions()
        if user is not None and "admin" not in user.roles:
            result["items"] = [item for item in result["items"] if item.get("created_by") == user.id]
            result["count"] = len(result["items"])
        return result

    @execution_router.get("/active", summary="List Active Executions")
    async def list_active_executions(request: Request):
        user = await _resolve_execution_user(request, context, scopes=["executions:read"])
        result = await service.list_active_executions()
        if user is not None and "admin" not in user.roles:
            result["items"] = [item for item in result["items"] if item.get("created_by") == user.id]
        return result

    @execution_router.get("/runtime/revisions", summary="List Runtime Revisions")
    async def list_runtime_revisions(request: Request, include_invalidated: bool = False):
        await _require_runtime_admin(request, context, scopes=["executions:read"])
        return await service.list_runtime_revisions(include_invalidated=include_invalidated)

    @execution_router.get("/runtime/revisions/{revision_id}", summary="Get Runtime Revision")
    async def get_runtime_revision(revision_id: str, request: Request, include_invalidated: bool = False):
        await _require_runtime_admin(request, context, scopes=["executions:read"])
        try:
            return await service.get_runtime_revision(revision_id, include_invalidated=include_invalidated)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.get("/runtime/containers", summary="List Managed Runtime Containers")
    async def list_managed_runtime_containers(request: Request):
        await _require_runtime_admin(request, context, scopes=["executions:read"])
        try:
            return await service.list_managed_containers()
        except ContainerRuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    @execution_router.get("/runtime/metrics", summary="Get Runtime Operations Metrics")
    async def get_runtime_metrics(request: Request):
        await _require_runtime_admin(request, context, scopes=["executions:read"])
        return await service.get_runtime_metrics()

    @execution_router.get("/runtime/containers/{container_id}/logs", summary="Get Managed Container Logs")
    async def get_runtime_container_logs(container_id: str, request: Request, tail_lines: int = 200):
        await _require_runtime_admin(request, context, scopes=["executions:read"])
        try:
            return await service.get_container_logs(container_id=container_id, tail_lines=tail_lines)
        except (ExecutionNotFoundError, ContainerRuntimeError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.post("/runtime/reconcile", summary="Run Runtime Reconciliation")
    async def reconcile_runtime(request: Request):
        await _require_runtime_admin(request, context, scopes=["executions:write"])
        try:
            return await service.reconcile_runtime()
        except ContainerRuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    @execution_router.post("/runtime/repair-stale", summary="Repair Stale Executions")
    async def repair_stale_executions(request: Request):
        await _require_runtime_admin(request, context, scopes=["executions:write"])
        return await service.repair_stale_executions()

    @execution_router.post("", summary="Create Execution")
    async def create_execution(request: CreateExecutionRequest, http_request: Request):
        current_user = await resolve_current_user(http_request, context, required_scopes=["workflows:run"])
        await _require_workflow_run_access_for_request(
            request.workflow_id,
            http_request,
            context,
            workflow_definition=request.workflow_definition,
            current_user=current_user,
        )
        try:
            return await service.create_execution(
                workflow_id=request.workflow_id,
                goal_id=request.goal_id,
                input_payload=request.input,
                trigger=request.trigger,
                context_pack_id=request.context_pack_id,
                runtime_adapter_id=request.runtime_adapter_id,
                execution_host=request.execution_host,
                workflow_definition=request.workflow_definition,
                model_profiles=request.model_profiles,
                current_user=current_user,
            )
        except (GoalNotFoundError, WorkflowNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @execution_router.post("/{execution_id}/start", summary="Start Execution")
    async def start_execution(execution_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:write"])
        try:
            return await service.queue_start(execution_id)
        except (ExecutionNotFoundError, WorkflowNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @execution_router.post("/{execution_id}/pause", summary="Pause Execution")
    async def pause_execution(execution_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:write"])
        try:
            return await service.pause(execution_id)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.post("/{execution_id}/resume", summary="Resume Execution")
    async def resume_execution(execution_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:write"])
        try:
            return await service.resume(execution_id)
        except (ExecutionNotFoundError, WorkflowNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @execution_router.post("/{execution_id}/tasks/{task_id}/retry", summary="Retry Failed Execution Task")
    async def retry_execution_task(execution_id: str, task_id: str, payload: TaskRetryRequest, request: Request):
        current_user = await _require_execution_access(
            execution_id, request, context, scopes=["executions:write"]
        )
        try:
            return await service.retry_task(
                execution_id,
                task_id,
                reason=payload.reason,
                actor=current_user.id if current_user else None,
            )
        except (ExecutionNotFoundError, WorkflowNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @execution_router.post("/{execution_id}/resume-from-checkpoint", summary="Resume Execution From Checkpoint")
    async def resume_execution_from_checkpoint(execution_id: str, payload: CheckpointResumeRequest, request: Request):
        current_user = await _require_execution_access(
            execution_id, request, context, scopes=["executions:write"]
        )
        try:
            return await service.resume_from_checkpoint(
                execution_id,
                reason=payload.reason,
                actor=current_user.id if current_user else None,
            )
        except (ExecutionNotFoundError, WorkflowNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @execution_router.post("/{execution_id}/cancel", summary="Cancel Execution")
    async def cancel_execution(execution_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:write"])
        try:
            return await service.cancel(execution_id)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.post("/{execution_id}/approve", summary="Approve Waiting Tool Call")
    async def approve_execution(execution_id: str, payload: ApprovalRequest, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:write"])
        try:
            return await service.approve(execution_id, payload.tool_id, payload.reason)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.post("/{execution_id}/reject", summary="Reject Waiting Tool Call")
    async def reject_execution(execution_id: str, payload: ApprovalRequest, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:write"])
        try:
            return await service.reject(execution_id, payload.tool_id, payload.reason)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}", summary="Get Execution By Id")
    async def get_execution(execution_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        try:
            return await service.get_execution(execution_id)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.put("/{execution_id}", summary="Update Execution")
    async def update_execution(execution_id: str, patch: dict[str, Any], request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:write"])
        try:
            return await service.update_execution(execution_id, patch)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}/events", summary="List Execution Events")
    async def list_execution_events(
            execution_id: str,
            request: Request,
            after_sequence: int = 0,
            event_type: list[str] | None = Query(default=None),
            event_types: str | None = Query(default=None),
    ):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        try:
            requested_event_types = list(event_type or [])
            if event_types:
                requested_event_types.append(event_types)
            return await service.list_execution_events(execution_id, after_sequence, requested_event_types)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}/usage", summary="Get Execution Token Usage")
    async def get_execution_usage(execution_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        try:
            return await service.get_execution_usage(execution_id)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}/context-usage", summary="Get Execution Context Usage")
    async def get_execution_context_usage(execution_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        try:
            return await service.get_execution_context_usage(execution_id)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}/artifacts", summary="List Execution Artifacts")
    async def list_execution_artifacts(execution_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        try:
            return await service.list_execution_artifacts(execution_id)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}/approvals", summary="List Execution Approval Requests")
    async def list_execution_approvals(execution_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        try:
            return await service.list_execution_approvals(execution_id)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.post("/{execution_id}/waits", summary="Create Durable Execution Wait")
    async def create_execution_wait(
            execution_id: str,
            payload: CreateExecutionWaitRequest,
            request: Request,
    ):
        await _require_execution_access(execution_id, request, context, scopes=["executions:write"])
        try:
            wait, created = await wait_service.create_wait(
                execution_id=execution_id,
                kind=payload.kind,
                idempotency_key=payload.idempotency_key,
                correlation_key=payload.correlation_key,
                checkpoint=payload.checkpoint,
                request_payload=payload.request_payload,
                policy=payload.policy,
                wake_at=payload.wake_at,
                deadline_at=payload.deadline_at,
                metadata=payload.metadata,
            )
            return {"wait": wait.model_dump(mode="json"), "created": created}
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ExecutionWaitConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}/waits", summary="List Durable Execution Waits")
    async def list_execution_waits(
            execution_id: str,
            request: Request,
            wait_status: ExecutionWaitStatus | None = Query(default=None, alias="status"),
    ):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        try:
            waits = await wait_service.list_waits(execution_id, status=wait_status)
            return {"items": [wait.model_dump(mode="json") for wait in waits], "count": len(waits)}
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}/waits/{wait_id}", summary="Get Durable Execution Wait")
    async def get_execution_wait(execution_id: str, wait_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        try:
            wait = await wait_service.get_wait(execution_id, wait_id)
            return wait.model_dump(mode="json")
        except ExecutionWaitNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.post("/{execution_id}/waits/{wait_id}/resolve", summary="Resolve Durable Execution Wait")
    async def resolve_execution_wait(
            execution_id: str,
            wait_id: str,
            payload: ResolveExecutionWaitRequest,
            request: Request,
    ):
        current_user = await _require_execution_access(
            execution_id,
            request,
            context,
            scopes=["executions:write"],
        )
        try:
            result = await wait_service.resolve_wait(
                execution_id=execution_id,
                wait_id=wait_id,
                resolution_key=payload.resolution_key,
                resolution_payload=payload.resolution_payload,
                status=payload.status,
                resolved_by=current_user.id if current_user else None,
                resume=payload.resume,
            )
            return {
                "wait": result["wait"].model_dump(mode="json"),
                "claimed": result["claimed"],
                "resumed": result["resumed"],
                "execution": (
                    result["execution"].model_dump(mode="json") if result["execution"] is not None else None
                ),
            }
        except (ExecutionNotFoundError, ExecutionWaitNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ExecutionWaitConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}/artifacts/images/stream", summary="Stream Execution Image Artifacts")
    async def stream_execution_images(
            execution_id: str,
            request: Request,
            poll_interval: float = Query(0.2, description="Polling interval in seconds"),
            max_duration: int = Query(300, description="Maximum streaming duration in seconds"),
    ):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        boundary = f"image-boundary-{uuid.uuid4().hex}"
        try:
            return StreamingResponse(
                service.stream_execution_images(execution_id, poll_interval, max_duration, boundary),
                media_type=f"multipart/x-mixed-replace; boundary={boundary}",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                    "Connection": "keep-alive",
                },
            )
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}/hitl/stream", summary="Stream Execution HITL Output")
    async def stream_execution_hitl_output(execution_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        try:
            return StreamingResponse(
                service.stream_execution_hitl_output(request, execution_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.post("/{execution_id}/hitl/reply", summary="Reply To Execution HITL Prompt")
    async def reply_execution_hitl(execution_id: str, request: Request):
        await _require_execution_access(execution_id, request, context, scopes=["executions:write"])
        data = await request.json()
        if not data or "reply" not in data:
            return JSONResponse({"error": "Invalid request. 'reply' is required"}, status_code=422)
        try:
            return JSONResponse(await service.publish_execution_hitl_reply(execution_id, data["reply"]),
                                status_code=200)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}/runtime/logs", summary="Get Execution Runtime Logs")
    async def get_execution_runtime_logs(execution_id: str, request: Request, tail_lines: int = 200):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        try:
            return await service.get_container_logs(execution_id=execution_id, tail_lines=tail_lines)
        except (ExecutionNotFoundError, ContainerRuntimeError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @execution_router.get("/{execution_id}/stream", summary="Stream Execution Events")
    async def stream_execution_events(execution_id: str, request: Request, after_sequence: int = 0):
        await _require_execution_access(execution_id, request, context, scopes=["executions:read"])
        try:
            return StreamingResponse(
                service.stream_execution_events(execution_id, request, after_sequence),
                media_type="text/event-stream",
            )
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/execution-waits/events/{correlation_key}", summary="Wake Event-Correlated Execution Waits")
    async def wake_event_execution_waits(
            correlation_key: str,
            payload: WakeExecutionEventRequest,
            request: Request,
    ):
        current_user = await _require_runtime_admin(request, context, scopes=["executions:write"])
        try:
            return await wait_service.wake_event(
                correlation_key=correlation_key,
                event_id=payload.event_id,
                event_payload=payload.event_payload,
                resolved_by=current_user.id if current_user else "event_trigger",
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    router.include_router(execution_router)

    workflow_execution_router = APIRouter(prefix="/workflows", tags=["Workflow Executions"])

    @workflow_execution_router.post("/{workflow_id}/executions", summary="Create Workflow Execution")
    async def create_workflow_execution(workflow_id: str, request: WorkflowExecutionRequest, http_request: Request):
        current_user = await resolve_current_user(http_request, context, required_scopes=["workflows:run"])
        await _require_workflow_run_access_for_request(
            workflow_id,
            http_request,
            context,
            workflow_definition=request.workflow_definition,
            current_user=current_user,
        )
        try:
            return await service.create_execution(
                workflow_id=workflow_id,
                goal_id=request.goal_id,
                input_payload=request.input,
                trigger=request.trigger,
                context_pack_id=request.context_pack_id,
                runtime_adapter_id=request.runtime_adapter_id,
                execution_host=request.execution_host,
                workflow_definition=request.workflow_definition,
                model_profiles=request.model_profiles,
                current_user=current_user,
            )
        except (GoalNotFoundError, WorkflowNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @workflow_execution_router.post("/{workflow_id}/executions/start", summary="Create And Start Workflow Execution")
    async def start_workflow_execution(workflow_id: str, request: WorkflowExecutionRequest, http_request: Request):
        current_user = await resolve_current_user(http_request, context, required_scopes=["workflows:run"])
        await _require_workflow_run_access_for_request(
            workflow_id,
            http_request,
            context,
            workflow_definition=request.workflow_definition,
            current_user=current_user,
        )
        try:
            execution = await service.create_execution(
                workflow_id=workflow_id,
                goal_id=request.goal_id,
                input_payload=request.input,
                trigger=request.trigger,
                context_pack_id=request.context_pack_id,
                runtime_adapter_id=request.runtime_adapter_id,
                execution_host=request.execution_host,
                workflow_definition=request.workflow_definition,
                model_profiles=request.model_profiles,
                current_user=current_user,
            )
            queued = await service.queue_start(execution["id"])
            return {
                "execution": queued,
                "process_id": queued["id"],
                "status": queued.get("status", "queued"),
            }
        except (ExecutionNotFoundError, GoalNotFoundError, WorkflowNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    router.include_router(workflow_execution_router)
    return router
