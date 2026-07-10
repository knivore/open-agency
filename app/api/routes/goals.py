"""Durable goal APIs for long-running autonomous work."""

from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from typing import Any

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user_if_present
from app.domain import GoalStatus
from app.services.goals import GoalNotFoundError, GoalService, GoalTransitionError


class CreateGoalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    objective: str
    status: GoalStatus = GoalStatus.CREATED
    priority: str = "normal"
    owner_actor: str | None = Field(default=None, validation_alias=AliasChoices("owner_actor", "ownerActor"))
    parent_goal_id: str | None = Field(default=None, validation_alias=AliasChoices("parent_goal_id", "parentGoalId"))
    success_criteria: list[dict[str, Any]] = Field(
        default_factory=list,
        validation_alias=AliasChoices("success_criteria", "successCriteria"),
    )
    constraints: dict[str, Any] = Field(default_factory=dict)
    deadline_at: datetime | None = Field(default=None, validation_alias=AliasChoices("deadline_at", "deadlineAt"))
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateGoalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    objective: str | None = None
    status: GoalStatus | None = None
    priority: str | None = None
    owner_actor: str | None = Field(default=None, validation_alias=AliasChoices("owner_actor", "ownerActor"))
    parent_goal_id: str | None = Field(default=None, validation_alias=AliasChoices("parent_goal_id", "parentGoalId"))
    success_criteria: list[dict[str, Any]] | None = Field(
        default=None,
        validation_alias=AliasChoices("success_criteria", "successCriteria"),
    )
    constraints: dict[str, Any] | None = None
    execution_ids: list[str] | None = Field(default=None,
                                            validation_alias=AliasChoices("execution_ids", "executionIds"))
    evidence: list[dict[str, Any]] | None = None
    evaluation: dict[str, Any] | None = None
    deadline_at: datetime | None = Field(default=None, validation_alias=AliasChoices("deadline_at", "deadlineAt"))
    completed_at: datetime | None = Field(default=None, validation_alias=AliasChoices("completed_at", "completedAt"))
    metadata: dict[str, Any] | None = None


class CancelGoalRequest(BaseModel):
    reason: str | None = None


class CompleteGoalRequest(BaseModel):
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    evaluation: dict[str, Any] | None = None


class AttachGoalEvidenceRequest(BaseModel):
    evidence: list[dict[str, Any]]


class EvaluateGoalRequest(BaseModel):
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    persist: bool = True


class PlanGoalRequest(BaseModel):
    plan: dict[str, Any] | None = None
    reason: str = "initial_plan"


class ReplanGoalRequest(BaseModel):
    plan: dict[str, Any] | None = None
    reason: str


class RecordSupervisorDecisionRequest(BaseModel):
    decision: dict[str, Any]


class StoreGoalSummaryMemoryRequest(BaseModel):
    reason: str = "goal_summary"


class OperatorGoalActionRequest(BaseModel):
    action: str
    reason: str | None = None
    autonomy: str | None = None
    owner_actor: str | None = Field(default=None, validation_alias=AliasChoices("owner_actor", "ownerActor"))
    success_criteria: list[dict[str, Any]] | None = Field(
        default=None,
        validation_alias=AliasChoices("success_criteria", "successCriteria"),
    )
    metadata: dict[str, Any] | None = None


def create_goals_router(context: ApiContext | None = None) -> APIRouter:
    context = context or get_default_api_context()
    service = GoalService(context)
    router = APIRouter(prefix="/goals", tags=["Goals"])

    @router.get("", summary="List Goals")
    async def list_goals(
            request: Request,
            status_filter: str | None = Query(default=None, alias="status"),
            parent_goal_id: str | None = Query(default=None, alias="parent_goal_id"),
            active_only: bool = False,
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:read"])
        try:
            return await service.list_goals(
                status=status_filter,
                parent_goal_id=parent_goal_id,
                active_only=active_only,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("", summary="Create Goal", status_code=status.HTTP_201_CREATED)
    async def create_goal(payload: CreateGoalRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        data = payload.model_dump(mode="json", exclude_none=True)
        data.setdefault("owner_actor", current_user.id if current_user else None)
        try:
            return (await service.create_goal(data)).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.get("/operator-view", summary="Get Goals Operator View")
    async def get_goals_operator_view(
            request: Request,
            status_filter: str | None = Query(default=None, alias="status"),
            parent_goal_id: str | None = Query(default=None, alias="parent_goal_id"),
            active_only: bool = False,
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:read"])
        try:
            return await service.operator_goal_view(
                status=status_filter,
                parent_goal_id=parent_goal_id,
                active_only=active_only,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.get("/{goal_id}/operator-detail", summary="Get Goal Operator Detail")
    async def get_goal_operator_detail(goal_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:read"])
        try:
            return await service.operator_goal_detail(goal_id)
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/{goal_id}/operator-actions", summary="Apply Goal Operator Action")
    async def apply_goal_operator_action(goal_id: str, payload: OperatorGoalActionRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        try:
            return (
                await service.apply_operator_action(
                    goal_id,
                    payload.model_dump(mode="json", exclude_none=True),
                    actor=current_user.id if current_user else None,
                )
            ).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except GoalTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.get("/{goal_id}", summary="Get Goal")
    async def get_goal(goal_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:read"])
        try:
            return (await service.get_goal(goal_id)).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.patch("/{goal_id}", summary="Update Goal")
    async def update_goal(goal_id: str, payload: UpdateGoalRequest, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        patch = payload.model_dump(mode="json", exclude_unset=True)
        try:
            return (await service.update_goal(goal_id, patch)).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("/{goal_id}/evidence", summary="Attach Goal Evidence")
    async def attach_goal_evidence(goal_id: str, payload: AttachGoalEvidenceRequest, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        try:
            return (await service.attach_evidence(goal_id, payload.evidence)).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("/{goal_id}/evaluate", summary="Evaluate Goal")
    async def evaluate_goal(goal_id: str, payload: EvaluateGoalRequest, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        try:
            return await service.evaluate_goal(
                goal_id,
                evidence=payload.evidence,
                persist=payload.persist,
            )
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("/{goal_id}/plan", summary="Plan Goal")
    async def plan_goal(goal_id: str, payload: PlanGoalRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        try:
            return (
                await service.plan_goal(
                    goal_id,
                    plan=payload.plan,
                    reason=payload.reason,
                    actor=current_user.id if current_user else None,
                )
            ).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except GoalTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("/{goal_id}/replan", summary="Replan Goal")
    async def replan_goal(goal_id: str, payload: ReplanGoalRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        try:
            return (
                await service.replan_goal(
                    goal_id,
                    plan=payload.plan,
                    reason=payload.reason,
                    actor=current_user.id if current_user else None,
                )
            ).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except GoalTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.get("/{goal_id}/supervisor-findings", summary="List Goal Supervisor Findings")
    async def list_goal_supervisor_findings(goal_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:read"])
        try:
            return await service.list_supervisor_findings(goal_id)
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/{goal_id}/supervisor-decisions", summary="Record Goal Supervisor Decision")
    async def record_goal_supervisor_decision(goal_id: str, payload: RecordSupervisorDecisionRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        try:
            return (
                await service.record_supervisor_decision(
                    goal_id,
                    payload.decision,
                    actor=current_user.id if current_user else None,
                )
            ).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("/{goal_id}/memory-summary", summary="Store Goal Summary Memory")
    async def store_goal_summary_memory(goal_id: str, payload: StoreGoalSummaryMemoryRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        try:
            return (
                await service.store_goal_summary_memory(
                    goal_id,
                    actor=current_user.id if current_user else None,
                    reason=payload.reason,
                )
            ).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("/{goal_id}/pause", summary="Pause Goal")
    async def pause_goal(goal_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        try:
            return (await service.pause_goal(goal_id)).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except GoalTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/{goal_id}/resume", summary="Resume Goal")
    async def resume_goal(goal_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        try:
            return (await service.resume_goal(goal_id)).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except GoalTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/{goal_id}/cancel", summary="Cancel Goal")
    async def cancel_goal(goal_id: str, payload: CancelGoalRequest, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        try:
            return (await service.cancel_goal(goal_id, reason=payload.reason)).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except GoalTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/{goal_id}/complete", summary="Complete Goal")
    async def complete_goal(goal_id: str, payload: CompleteGoalRequest, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["goals:write"])
        try:
            return (
                await service.complete_goal(
                    goal_id,
                    evidence=payload.evidence,
                    evaluation=payload.evaluation,
                )
            ).model_dump(mode="json")
        except GoalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except GoalTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    return router
