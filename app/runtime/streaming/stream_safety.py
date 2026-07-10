"""Runtime stream rate limiting, batching, and payload safety helpers."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from .runtime_event_models import RuntimeEventLevel, RuntimeEventType, RuntimeStreamEvent

DEFAULT_RUNTIME_STREAM_MAX_EVENT_BYTES = 64 * 1024
DEFAULT_RUNTIME_STREAM_MAX_LOG_CHARS = 4_000
DEFAULT_RUNTIME_STREAM_MAX_EVENTS_PER_SECOND = 30
DEFAULT_RUNTIME_STREAM_MAX_BATCH_SIZE = 25


class RuntimeStreamRateLimiter:
    """Simple async limiter that spaces outbound stream events."""

    def __init__(self, max_events_per_second: int = DEFAULT_RUNTIME_STREAM_MAX_EVENTS_PER_SECOND) -> None:
        self.min_interval = 1 / max(1, max_events_per_second)
        self._next_allowed_at = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        if self._next_allowed_at > now:
            await asyncio.sleep(self._next_allowed_at - now)
            now = time.monotonic()
        self._next_allowed_at = now + self.min_interval


def is_debug_event(event: RuntimeStreamEvent) -> bool:
    return event.level == RuntimeEventLevel.DEBUG


def should_drop_event_for_lag(event: RuntimeStreamEvent, queue: asyncio.Queue[RuntimeStreamEvent]) -> bool:
    return is_debug_event(event) and queue.full()


def collect_batch(
        queue: asyncio.Queue[RuntimeStreamEvent],
        first_event: RuntimeStreamEvent,
        *,
        max_batch_size: int = DEFAULT_RUNTIME_STREAM_MAX_BATCH_SIZE,
        event_filter: Callable[[RuntimeStreamEvent], bool] | None = None,
) -> list[RuntimeStreamEvent]:
    events = [first_event]
    if first_event.type != RuntimeEventType.LOG_RECEIVED:
        return events

    while len(events) < max_batch_size:
        try:
            next_event = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if should_drop_event_for_lag(next_event, queue):
            continue
        if event_filter is not None and not event_filter(next_event):
            continue
        if next_event.type != RuntimeEventType.LOG_RECEIVED:
            events.append(next_event)
            break
        events.append(next_event)
    return events


def _truncate_text(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    suffix = "...[truncated]"
    return value[: max(0, max_chars - len(suffix))] + suffix, True


def safe_runtime_event_payload(
        event: RuntimeStreamEvent,
        *,
        max_event_bytes: int = DEFAULT_RUNTIME_STREAM_MAX_EVENT_BYTES,
        max_log_chars: int = DEFAULT_RUNTIME_STREAM_MAX_LOG_CHARS,
) -> dict[str, Any]:
    payload = event.to_external_event()
    metadata = dict(payload.get("metadata") or {})

    if payload.get("type") == RuntimeEventType.LOG_RECEIVED.value and isinstance(payload.get("message"), str):
        payload["message"], truncated = _truncate_text(payload["message"], max_log_chars)
        if truncated:
            metadata["logTruncated"] = True

    serialized = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    if len(serialized) > max_event_bytes:
        metadata.update(
            {
                "payloadTruncated": True,
                "originalByteLength": len(serialized),
            }
        )
        payload["metadata"] = metadata
        if isinstance(payload.get("message"), str):
            payload["message"], _ = _truncate_text(payload["message"], min(max_log_chars, 512))
        # Metadata is the least important field for keeping the stream safe.
        serialized = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        if len(serialized) > max_event_bytes:
            payload["metadata"] = {
                "payloadTruncated": True,
                "originalByteLength": len(serialized),
            }
            payload["message"] = "[payload truncated]"
    elif metadata:
        payload["metadata"] = metadata

    return payload


def safe_runtime_events_payload(events: list[RuntimeStreamEvent]) -> dict[str, Any] | list[dict[str, Any]]:
    payloads = [safe_runtime_event_payload(event) for event in events]
    if len(payloads) == 1:
        return payloads[0]
    return payloads
