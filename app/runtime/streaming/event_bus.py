"""In-process fan-out bus for runtime stream events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .runtime_event_models import RuntimeStreamEvent

DEFAULT_RUNTIME_EVENT_QUEUE_SIZE = 256


@dataclass(frozen=True, slots=True)
class RuntimeEventPublishResult:
    subscribers: int
    delivered: int
    dropped: int
    errors: int = 0


@dataclass(slots=True)
class RuntimeEventBus:
    """Publish runtime events to active SSE/WebSocket subscribers."""

    max_queue_size: int = DEFAULT_RUNTIME_EVENT_QUEUE_SIZE
    _subscribers: set[asyncio.Queue[RuntimeStreamEvent]] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def publish(self, event: RuntimeStreamEvent) -> RuntimeEventPublishResult:
        async with self._lock:
            subscribers = list(self._subscribers)

        delivered = 0
        dropped = 0
        errors = 0
        for queue in subscribers:
            try:
                queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                dropped += 1
            except Exception:
                errors += 1

        return RuntimeEventPublishResult(
            subscribers=len(subscribers),
            delivered=delivered,
            dropped=dropped,
            errors=errors,
        )

    async def subscribe(self, *, max_queue_size: int | None = None) -> asyncio.Queue[RuntimeStreamEvent]:
        queue: asyncio.Queue[RuntimeStreamEvent] = asyncio.Queue(maxsize=max_queue_size or self.max_queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[RuntimeStreamEvent]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def subscriber_count(self) -> int:
        async with self._lock:
            return len(self._subscribers)


_default_runtime_event_bus: RuntimeEventBus | None = None


def get_default_runtime_event_bus() -> RuntimeEventBus:
    global _default_runtime_event_bus
    if _default_runtime_event_bus is None:
        _default_runtime_event_bus = RuntimeEventBus()
    return _default_runtime_event_bus


def set_default_runtime_event_bus(event_bus: RuntimeEventBus | None) -> None:
    global _default_runtime_event_bus
    _default_runtime_event_bus = event_bus
