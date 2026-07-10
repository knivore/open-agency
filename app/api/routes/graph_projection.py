"""Graph projection outbox status and replay controls."""

from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user_if_present
from app.core.config import get_settings
from app.graph.projection import GraphProjectionWorker


class GraphProjectionReplayRequest(BaseModel):
    event_ids: list[str] | None = Field(default=None)
    run: bool = False


def _age_seconds(value: object | None) -> float | None:
    if not isinstance(value, datetime):
        return None
    timestamp = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - timestamp).total_seconds(), 0.0)


def create_graph_projection_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/graph/projection", tags=["Graph Projection"])

    @router.get("/status", summary="Get Graph Projection Outbox Status")
    async def get_graph_projection_status(request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["executions:read"])
        repo = getattr(context, "graph_projection_event_repo", None)
        if repo is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="Graph projection repository unavailable")
        summary = await repo.status_summary()
        oldest_pending_at = summary.get("oldest_pending_at")
        return {
            "enabled": get_settings().graph_projection_enabled,
            **summary,
            "oldest_pending_age_seconds": _age_seconds(oldest_pending_at),
            "last_projected_age_seconds": _age_seconds(summary.get("last_projected_at")),
            "latest_event_age_seconds": _age_seconds(summary.get("latest_event_at")),
            "last_projected_execution_event_age_seconds": _age_seconds(
                summary.get("last_projected_execution_event_at")
            ),
            "last_projected_memory_event_age_seconds": _age_seconds(summary.get("last_projected_memory_event_at")),
        }

    @router.post("/replay", summary="Reset Graph Projection Events For Replay")
    async def replay_graph_projection(payload: GraphProjectionReplayRequest, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["executions:write"])
        repo = getattr(context, "graph_projection_event_repo", None)
        if repo is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="Graph projection repository unavailable")
        worker = GraphProjectionWorker(repo)
        result = await worker.replay(event_ids=payload.event_ids, run=payload.run)
        return {
            "reset": True,
            "processed": result.processed,
            "failed": result.failed,
            "checkpoint_event_id": result.checkpoint_event_id,
            "errors": result.errors,
        }

    return router


__all__ = ["create_graph_projection_router"]
