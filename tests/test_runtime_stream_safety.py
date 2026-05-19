from __future__ import annotations

import asyncio
import json
import unittest

from app.api.streaming.runtime_sse import runtime_event_sse_stream
from app.runtime.streaming import (
    DEFAULT_RUNTIME_EVENT_QUEUE_SIZE,
    RuntimeEventBus,
    RuntimeEventLevel,
    RuntimeEventType,
    RuntimeStreamEvent,
)
from app.runtime.streaming.stream_safety import (
    RuntimeStreamRateLimiter,
    safe_runtime_event_payload,
    should_drop_event_for_lag,
)


class _ConnectedRequest:
    headers: dict[str, str] = {}
    query_params: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


def _parse_sse_payload(chunk: str) -> tuple[str | None, object]:
    event_name = None
    data_lines: list[str] = []
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event_name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))
    return event_name, json.loads("\n".join(data_lines))


class RuntimeStreamSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_log_payloads_are_truncated(self):
        payload = safe_runtime_event_payload(
            RuntimeStreamEvent(
                id="evt:long-log",
                type=RuntimeEventType.LOG_RECEIVED,
                message="x" * 100,
            ),
            max_log_chars=20,
        )

        self.assertLessEqual(len(payload["message"]), 20)
        self.assertTrue(payload["metadata"]["logTruncated"])

    def test_large_payloads_are_capped(self):
        payload = safe_runtime_event_payload(
            RuntimeStreamEvent(
                id="evt:large",
                type=RuntimeEventType.LOG_RECEIVED,
                message="x" * 1_000,
                metadata={"large": "y" * 10_000},
            ),
            max_event_bytes=512,
            max_log_chars=128,
        )

        self.assertLessEqual(len(json.dumps(payload, separators=(",", ":")).encode("utf-8")), 512)
        self.assertTrue(payload["metadata"]["payloadTruncated"])

    async def test_sse_batches_noisy_logs(self):
        bus = RuntimeEventBus()
        stream = runtime_event_sse_stream(_ConnectedRequest(), bus=bus, heartbeat_seconds=1)
        await anext(stream)

        await bus.publish(RuntimeStreamEvent(id="evt:log-1", type=RuntimeEventType.LOG_RECEIVED, message="one"))
        await bus.publish(RuntimeStreamEvent(id="evt:log-2", type=RuntimeEventType.LOG_RECEIVED, message="two"))
        chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        event_name, payload = _parse_sse_payload(chunk)
        self.assertEqual(event_name, "runtime_event")
        self.assertIsInstance(payload, list)
        self.assertEqual([item["id"] for item in payload], ["evt:log-1", "evt:log-2"])

    async def test_debug_events_are_dropped_when_client_is_behind(self):
        queue: asyncio.Queue[RuntimeStreamEvent] = asyncio.Queue(maxsize=1)
        queue.put_nowait(RuntimeStreamEvent(id="evt:queued", type=RuntimeEventType.LOG_RECEIVED))
        debug_event = RuntimeStreamEvent(
            id="evt:debug",
            type=RuntimeEventType.LOG_RECEIVED,
            level=RuntimeEventLevel.DEBUG,
        )

        self.assertTrue(should_drop_event_for_lag(debug_event, queue))

    async def test_rate_limiter_defers_fast_repeated_sends(self):
        limiter = RuntimeStreamRateLimiter(max_events_per_second=1_000)

        await limiter.wait()
        before = asyncio.get_running_loop().time()
        await limiter.wait()
        elapsed = asyncio.get_running_loop().time() - before

        self.assertGreaterEqual(elapsed, 0.0005)

    async def test_runtime_event_bus_uses_bounded_queues(self):
        bus = RuntimeEventBus()
        subscriber = await bus.subscribe()

        self.assertEqual(subscriber.maxsize, DEFAULT_RUNTIME_EVENT_QUEUE_SIZE)


if __name__ == "__main__":
    unittest.main()
