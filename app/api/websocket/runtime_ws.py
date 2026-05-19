from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Query, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.runtime.streaming import get_default_runtime_event_bus
from app.runtime.streaming.stream_serialization import (
    RUNTIME_STREAM_AUTH_ENV,
    runtime_stream_connected_payload,
    runtime_stream_heartbeat_payload,
)
from app.runtime.streaming.stream_safety import (
    DEFAULT_RUNTIME_STREAM_MAX_EVENTS_PER_SECOND,
    RuntimeStreamRateLimiter,
    collect_batch,
    safe_runtime_events_payload,
    should_drop_event_for_lag,
)
from app.runtime.streaming.stream_filters import RuntimeEventFilter


def authorize_runtime_websocket(websocket: WebSocket) -> bool:
    expected = os.getenv(RUNTIME_STREAM_AUTH_ENV)
    if not expected:
        return True
    provided = websocket.headers.get("x-runtime-stream-key") or websocket.query_params.get("stream_key")
    return provided == expected


def create_runtime_websocket_router() -> APIRouter:
    router = APIRouter(tags=["Runtime Events"])

    @router.websocket("/ws/runtime/events")
    async def runtime_events_websocket(
            websocket: WebSocket,
            heartbeat_seconds: float = Query(15.0, ge=1.0, le=60.0),
            after: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
    ):
        if not authorize_runtime_websocket(websocket):
            await websocket.close(code=1008)
            return

        await websocket.accept()
        runtime_bus = get_default_runtime_event_bus()
        queue = await runtime_bus.subscribe()
        rate_limiter = RuntimeStreamRateLimiter(DEFAULT_RUNTIME_STREAM_MAX_EVENTS_PER_SECOND)
        event_filter = RuntimeEventFilter.from_query(workflow_id=workflow_id, agent_id=agent_id)
        try:
            last_event_id = websocket.headers.get("last-event-id") or after
            await websocket.send_json(runtime_stream_connected_payload(last_event_id=last_event_id))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                except TimeoutError:
                    await rate_limiter.wait()
                    await websocket.send_json(runtime_stream_heartbeat_payload())
                    continue
                if should_drop_event_for_lag(event, queue):
                    continue
                if not event_filter.matches(event):
                    continue
                events = collect_batch(queue, event, event_filter=event_filter.matches)
                await rate_limiter.wait()
                await websocket.send_json(safe_runtime_events_payload(events))
        except WebSocketDisconnect:
            return
        finally:
            await runtime_bus.unsubscribe(queue)

    return router
