from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user_if_present
from app.scheduler.scheduler import ScheduleConcurrencyError
from app.services.schedules import ScheduleService
from ._crud import serializable_validation_errors


def create_schedules_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = ScheduleService(context)
    router = APIRouter(prefix="/schedules", tags=["Schedules"])

    @router.post("", summary="Create Schedule")
    async def create_schedule(payload: dict[str, Any], request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["schedules:write"])
        try:
            created = await service.create_schedule(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        return created.model_dump(mode="json")

    @router.get("", summary="List Schedules")
    async def list_schedules(request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["schedules:read"])
        items = await context.schedule_repo.list()
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.get("/{schedule_id}", summary="Get Schedule By Id")
    async def get_schedule(schedule_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["schedules:read"])
        item = await context.schedule_repo.get(schedule_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Schedule '{schedule_id}' not found")
        return item.model_dump(mode="json")

    @router.patch("/{schedule_id}", summary="Patch Schedule")
    async def patch_schedule(schedule_id: str, patch: dict[str, Any], request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["schedules:write"])
        try:
            item = await service.patch_schedule(schedule_id, patch)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Schedule '{schedule_id}' not found")
        return item.model_dump(mode="json")

    @router.put("/{schedule_id}", summary="Update Schedule")
    async def update_schedule(schedule_id: str, patch: dict[str, Any], request: Request):
        return await patch_schedule(schedule_id, patch, request)

    @router.post("/{schedule_id}/enable", summary="Enable Schedule")
    async def enable_schedule(schedule_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["schedules:write"])
        item = await service.enable_schedule(schedule_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Schedule '{schedule_id}' not found")
        return item.model_dump(mode="json")

    @router.post("/{schedule_id}/disable", summary="Disable Schedule")
    async def disable_schedule(schedule_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["schedules:write"])
        item = await service.disable_schedule(schedule_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Schedule '{schedule_id}' not found")
        return item.model_dump(mode="json")

    @router.post("/{schedule_id}/trigger-now", summary="Trigger Schedule Now")
    async def trigger_schedule_now(schedule_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["schedules:write"])
        try:
            result = await service.trigger_now(schedule_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ScheduleConcurrencyError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return {
            "schedule": result.schedule.model_dump(mode="json"),
            "execution_id": result.execution_id,
            "triggered_at": result.triggered_at.isoformat(),
            "metadata": result.metadata,
        }

    @router.post("/events/dispatch", summary="Dispatch Schedule Event")
    async def dispatch_schedule_event(payload: dict[str, Any], request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["schedules:write"])
        try:
            results = await service.dispatch_event(payload)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {
            "items": [
                {
                    "schedule": result.schedule.model_dump(mode="json"),
                    "execution_id": result.execution_id,
                    "triggered_at": result.triggered_at.isoformat(),
                    "metadata": result.metadata,
                }
                for result in results
            ],
            "count": len(results),
        }

    @router.delete("/{schedule_id}", summary="Soft Delete Schedule")
    async def delete_schedule(schedule_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["schedules:write"])
        deleted = await context.schedule_repo.soft_delete(schedule_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Schedule '{schedule_id}' not found")
        return {"deleted": True, "id": schedule_id}

    return router
