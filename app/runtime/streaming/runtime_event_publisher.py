from __future__ import annotations

from typing import Optional

from .event_bus import RuntimeEventBus, RuntimeEventPublishResult, get_default_runtime_event_bus
from .runtime_event_models import RuntimeStreamEvent


class RuntimeEventPublisher:
    def __init__(self, event_bus: Optional[RuntimeEventBus] = None) -> None:
        self.event_bus = event_bus or get_default_runtime_event_bus()

    async def publish(self, event: RuntimeStreamEvent) -> RuntimeEventPublishResult:
        return await self.event_bus.publish(event)
