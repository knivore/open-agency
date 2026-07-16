"""Server-sent-events endpoint for runtime event streams."""

from __future__ import annotations

import asyncio
import hmac
import os
from collections.abc import AsyncIterator
from fastapi import APIRouter, HTTPException, Query, Request, status
from starlette.responses import StreamingResponse
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import request_has_identity, resolve_current_user
from app.runtime.streaming.event_bus import RuntimeEventBus, get_default_runtime_event_bus
from app.runtime.streaming.stream_filters import RuntimeEventFilter
from app.runtime.streaming.stream_safety import (
    DEFAULT_RUNTIME_STREAM_MAX_EVENTS_PER_SECOND,
    RuntimeStreamRateLimiter,
    collect_batch,
    should_drop_event_for_lag,
)
from app.runtime.streaming.stream_serialization import (
    RUNTIME_STREAM_AUTH_ENV,
    RUNTIME_STREAM_CONNECTED_EVENT_NAME,
    RUNTIME_STREAM_HEARTBEAT_EVENT_NAME,
    format_runtime_events_sse,
    format_sse_message,
    runtime_stream_connected_payload,
    runtime_stream_heartbeat_payload,
)
from app.core.config import get_settings


def authorize_runtime_sse_request(request: Request) -> None:
    expected = os.getenv(RUNTIME_STREAM_AUTH_ENV) or get_settings().agency_internal_api_key
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Runtime stream authentication is not configured")
    provided = request.headers.get("x-runtime-stream-key")
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Runtime stream auth failed")


async def runtime_event_sse_stream(
        request: Request,
        *,
        bus: Optional[RuntimeEventBus] = None,
        heartbeat_seconds: float = 15.0,
        retry_ms: int = 3000,
        last_event_id: str | None = None,
        event_filter: RuntimeEventFilter | None = None,
) -> AsyncIterator[str]:
    runtime_bus = bus or get_default_runtime_event_bus()
    queue = await runtime_bus.subscribe()
    rate_limiter = RuntimeStreamRateLimiter(DEFAULT_RUNTIME_STREAM_MAX_EVENTS_PER_SECOND)
    active_filter = event_filter or RuntimeEventFilter()
    try:
        yield format_sse_message(
            event_name=RUNTIME_STREAM_CONNECTED_EVENT_NAME,
            data=runtime_stream_connected_payload(last_event_id=last_event_id),
            retry_ms=retry_ms,
        )
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                await rate_limiter.wait()
                yield format_sse_message(
                    event_name=RUNTIME_STREAM_HEARTBEAT_EVENT_NAME,
                    data=runtime_stream_heartbeat_payload(),
                )
                continue
            if should_drop_event_for_lag(event, queue):
                continue
            if not active_filter.matches(event):
                continue
            events = collect_batch(queue, event, event_filter=active_filter.matches)
            await rate_limiter.wait()
            yield format_runtime_events_sse(events)
    finally:
        await runtime_bus.unsubscribe(queue)


def create_runtime_sse_router(context: ApiContext | None = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/api/runtime/events", tags=["Runtime Events"])

    @router.get("/stream", summary="Stream Runtime Events")
    async def stream_runtime_events(
            request: Request,
            heartbeat_seconds: float = Query(15.0, ge=1.0, le=60.0),
            retry_ms: int = Query(3000, ge=500, le=30000),
            after: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
    ):
        authorize_runtime_sse_request(request)
        if get_settings().app_env != "test" or request_has_identity(request):
            current_user = await resolve_current_user(request, context, required_scopes=["executions:read"])
            if "admin" not in current_user.roles:
                # This stream spans executions and workflows, so a tenant-level
                # principal cannot be authorized by an optional query filter.
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role is required")
        last_event_id = request.headers.get("last-event-id") or after
        event_filter = RuntimeEventFilter.from_query(workflow_id=workflow_id, agent_id=agent_id)
        return StreamingResponse(
            runtime_event_sse_stream(
                request,
                heartbeat_seconds=heartbeat_seconds,
                retry_ms=retry_ms,
                last_event_id=last_event_id,
                event_filter=event_filter,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
