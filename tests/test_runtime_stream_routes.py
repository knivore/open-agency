from __future__ import annotations

import asyncio
import json
import os
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.streaming.runtime_sse import create_runtime_sse_router, runtime_event_sse_stream
from app.api.websocket.runtime_ws import create_runtime_websocket_router
from app.runtime.streaming import (
    RUNTIME_STREAM_AUTH_ENV,
    RuntimeEventFilter,
    RuntimeEventBus,
    RuntimeEventType,
    RuntimeStreamEvent,
    set_default_runtime_event_bus,
)


class _ConnectedRequest:
    headers: dict[str, str] = {}
    query_params: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


def _parse_sse_payload(chunk: str) -> tuple[str | None, dict]:
    event_name = None
    data_lines: list[str] = []
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event_name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))
    return event_name, json.loads("\n".join(data_lines))


class RuntimeStreamRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        set_default_runtime_event_bus(None)
        os.environ.pop(RUNTIME_STREAM_AUTH_ENV, None)

    async def test_sse_stream_emits_connected_and_live_event(self):
        bus = RuntimeEventBus()
        stream = runtime_event_sse_stream(
            _ConnectedRequest(),
            bus=bus,
            heartbeat_seconds=1,
            retry_ms=1000,
            last_event_id="evt:previous",
        )

        connected_chunk = await anext(stream)
        connected_event, connected_payload = _parse_sse_payload(connected_chunk)
        self.assertEqual(connected_event, "runtime_stream.connected")
        self.assertEqual(connected_payload["lastEventId"], "evt:previous")

        event = RuntimeStreamEvent(id="evt:live", type=RuntimeEventType.TASK_STARTED)
        await bus.publish(event)
        live_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        event_name, payload = _parse_sse_payload(live_chunk)
        self.assertEqual(event_name, "runtime_event")
        self.assertEqual(payload["id"], "evt:live")
        self.assertEqual(await bus.subscriber_count(), 0)

    async def test_sse_stream_filters_by_workflow_and_agent(self):
        bus = RuntimeEventBus()
        stream = runtime_event_sse_stream(
            _ConnectedRequest(),
            bus=bus,
            heartbeat_seconds=1,
            event_filter=RuntimeEventFilter.from_query(workflow_id="workflow:keep", agent_id="agent:keep"),
        )
        await anext(stream)

        await bus.publish(
            RuntimeStreamEvent(
                id="evt:drop",
                type=RuntimeEventType.TASK_STARTED,
                workflow={"id": "workflow:drop"},
                actor={"id": "agent:keep"},
            )
        )
        await bus.publish(
            RuntimeStreamEvent(
                id="evt:keep",
                type=RuntimeEventType.TASK_STARTED,
                workflow={"id": "workflow:keep"},
                actor={"id": "agent:keep"},
            )
        )
        live_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        event_name, payload = _parse_sse_payload(live_chunk)
        self.assertEqual(event_name, "runtime_event")
        self.assertEqual(payload["id"], "evt:keep")

    async def test_sse_stream_emits_heartbeat(self):
        bus = RuntimeEventBus()
        stream = runtime_event_sse_stream(_ConnectedRequest(), bus=bus, heartbeat_seconds=0.01)

        await anext(stream)
        heartbeat_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        event_name, payload = _parse_sse_payload(heartbeat_chunk)
        self.assertEqual(event_name, "runtime_stream.heartbeat")
        self.assertEqual(payload["type"], "runtime_stream.heartbeat")

    async def test_sse_route_rejects_bad_stream_key(self):
        os.environ[RUNTIME_STREAM_AUTH_ENV] = "secret"
        app = FastAPI()
        app.include_router(create_runtime_sse_router())

        with TestClient(app) as client:
            response = client.get("/api/runtime/events/stream", headers={"x-runtime-stream-key": "bad"})

        self.assertEqual(response.status_code, 403)

    async def test_websocket_connects_and_receives_live_event(self):
        bus = RuntimeEventBus()
        set_default_runtime_event_bus(bus)
        app = FastAPI()
        app.include_router(create_runtime_websocket_router())

        with TestClient(app) as client:
            with client.websocket_connect("/ws/runtime/events?heartbeat_seconds=1&after=evt:previous") as websocket:
                connected = websocket.receive_json()
                self.assertEqual(connected["type"], "runtime_stream.connected")
                self.assertEqual(connected["lastEventId"], "evt:previous")

                await bus.publish(RuntimeStreamEvent(id="evt:ws", type=RuntimeEventType.LOG_RECEIVED))
                payload = websocket.receive_json()

        self.assertEqual(payload["id"], "evt:ws")
        self.assertEqual(payload["type"], "log_received")

    async def test_websocket_filters_by_workflow_and_agent(self):
        bus = RuntimeEventBus()
        set_default_runtime_event_bus(bus)
        app = FastAPI()
        app.include_router(create_runtime_websocket_router())

        with TestClient(app) as client:
            with client.websocket_connect(
                    "/ws/runtime/events?heartbeat_seconds=1&workflow_id=workflow:keep&agent_id=agent:keep"
            ) as websocket:
                connected = websocket.receive_json()
                self.assertEqual(connected["type"], "runtime_stream.connected")

                await bus.publish(
                    RuntimeStreamEvent(
                        id="evt:ws-drop",
                        type=RuntimeEventType.TASK_STARTED,
                        workflow={"id": "workflow:keep"},
                        actor={"id": "agent:drop"},
                    )
                )
                await bus.publish(
                    RuntimeStreamEvent(
                        id="evt:ws-keep",
                        type=RuntimeEventType.TASK_STARTED,
                        workflow={"id": "workflow:keep"},
                        actor={"id": "agent:keep"},
                    )
                )
                payload = websocket.receive_json()

        self.assertEqual(payload["id"], "evt:ws-keep")


if __name__ == "__main__":
    unittest.main()
