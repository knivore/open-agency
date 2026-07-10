"""Server-sent-events endpoint for projected graph visualization deltas."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from starlette.responses import StreamingResponse
from time import monotonic
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user_if_present
from app.core.config import get_settings
from app.graph.delta import graph_projection_event_to_delta
from app.runtime.streaming.stream_serialization import format_sse_message, utc_timestamp

GRAPH_DELTA_EVENT_NAME = "graph_delta"
GRAPH_STREAM_CONNECTED_EVENT_NAME = "graph_stream.connected"
GRAPH_STREAM_HEARTBEAT_EVENT_NAME = "graph_stream.heartbeat"


def _split_csv(values: str | None) -> set[str]:
    if not values:
        return set()
    return {value.strip() for value in values.split(",") if value.strip()}


@dataclass(frozen=True, slots=True)
class GraphDeltaStreamFilter:
    execution_id: str | None = None
    workflow_id: str | None = None
    aggregate_type: str | None = None
    event_types: set[str] | None = None

    def matches(self, event) -> bool:
        if self.aggregate_type and event.aggregate_type != self.aggregate_type:
            return False
        if self.event_types and event.event_type not in self.event_types:
            return False
        payload = event.payload if isinstance(event.payload, dict) else {}
        if self.execution_id:
            if not (
                    payload.get("execution_id") == self.execution_id
                    or event.aggregate_id == self.execution_id
                    or event.aggregate_id.startswith(f"{self.execution_id}:")
            ):
                return False
        if self.workflow_id:
            if not (payload.get("workflow_id") == self.workflow_id or event.aggregate_id == self.workflow_id):
                return False
        return True


def graph_stream_connected_payload(*, last_event_id: str | None = None) -> dict:
    payload = {
        "type": GRAPH_STREAM_CONNECTED_EVENT_NAME,
        "timestamp": utc_timestamp(),
    }
    if last_event_id:
        payload["lastEventId"] = last_event_id
    return payload


def graph_stream_heartbeat_payload() -> dict:
    return {
        "type": GRAPH_STREAM_HEARTBEAT_EVENT_NAME,
        "timestamp": utc_timestamp(),
    }


async def graph_delta_sse_stream(
        request: Request,
        *,
        event_repository,
        heartbeat_seconds: float = 15.0,
        poll_seconds: float = 1.0,
        retry_ms: int = 3000,
        last_event_id: str | None = None,
        limit: int = 50,
        event_filter: GraphDeltaStreamFilter | None = None,
) -> AsyncIterator[str]:
    checkpoint_event_id = last_event_id
    last_heartbeat_at = monotonic()
    active_filter = event_filter or GraphDeltaStreamFilter()

    yield format_sse_message(
        event_name=GRAPH_STREAM_CONNECTED_EVENT_NAME,
        data=graph_stream_connected_payload(last_event_id=last_event_id),
        retry_ms=retry_ms,
    )

    while True:
        if await request.is_disconnected():
            break

        events = await event_repository.list_events(
            status="projected",
            after_event_id=checkpoint_event_id,
            limit=limit,
        )
        if events:
            for event in events:
                checkpoint_event_id = event.event_id
                if not active_filter.matches(event):
                    continue
                yield format_sse_message(
                    event_name=GRAPH_DELTA_EVENT_NAME,
                    event_id=event.event_id,
                    data=jsonable_encoder(graph_projection_event_to_delta(event)),
                )
            last_heartbeat_at = monotonic()
            continue

        now = monotonic()
        if now - last_heartbeat_at >= heartbeat_seconds:
            yield format_sse_message(
                event_name=GRAPH_STREAM_HEARTBEAT_EVENT_NAME,
                data=graph_stream_heartbeat_payload(),
            )
            last_heartbeat_at = now

        await asyncio.sleep(min(poll_seconds, heartbeat_seconds))


def create_graph_stream_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/graph/stream", tags=["Graph Stream"])

    @router.get("/deltas", summary="Stream Projected Graph Deltas")
    async def stream_graph_deltas(
            request: Request,
            heartbeat_seconds: float = Query(15.0, ge=1.0, le=60.0),
            poll_seconds: float = Query(1.0, ge=0.25, le=10.0),
            retry_ms: int = Query(3000, ge=500, le=30000),
            after: str | None = None,
            limit: int = Query(50, ge=1, le=500),
            execution_id: str | None = None,
            workflow_id: str | None = None,
            aggregate_type: str | None = None,
            event_types: str | None = Query(default=None, description="Comma-separated graph projection event types."),
    ):
        current_user = await resolve_current_user_if_present(
            request,
            context,
            required_scopes=["executions:read"],
        )
        if current_user is None and get_settings().app_env != "development":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Graph stream identity is required",
            )
        repo = getattr(context, "graph_projection_event_repo", None)
        if repo is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Graph projection repository unavailable",
            )
        last_event_id = request.headers.get("last-event-id") or after
        return StreamingResponse(
            graph_delta_sse_stream(
                request,
                event_repository=repo,
                heartbeat_seconds=heartbeat_seconds,
                poll_seconds=poll_seconds,
                retry_ms=retry_ms,
                last_event_id=last_event_id,
                limit=limit,
                event_filter=GraphDeltaStreamFilter(
                    execution_id=execution_id,
                    workflow_id=workflow_id,
                    aggregate_type=aggregate_type,
                    event_types=_split_csv(event_types),
                ),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
