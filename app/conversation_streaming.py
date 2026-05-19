from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ConversationEventBroker:
    _subscriptions: dict[str, set[asyncio.Queue[dict[str, Any]]]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def publish(self, conversation_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            subscribers = list(self._subscriptions.get(conversation_id, set()))
        for queue in subscribers:
            await queue.put(event)

    async def subscribe(self, conversation_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._subscriptions.setdefault(conversation_id, set()).add(queue)
        return queue

    async def unsubscribe(self, conversation_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            subscribers = self._subscriptions.get(conversation_id)
            if not subscribers:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._subscriptions.pop(conversation_id, None)
