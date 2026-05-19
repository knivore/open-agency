from __future__ import annotations

import asyncio
import os
import unittest

from app.runtime import channels


class RuntimeChannelFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._previous_backend = os.environ.get("AGENCY_CHANNEL_BACKEND")
        os.environ["AGENCY_CHANNEL_BACKEND"] = "inmemory"
        channels._backend_mode = None

    def tearDown(self) -> None:
        if self._previous_backend is None:
            os.environ.pop("AGENCY_CHANNEL_BACKEND", None)
        else:
            os.environ["AGENCY_CHANNEL_BACKEND"] = self._previous_backend
        channels._backend_mode = None

    async def test_sync_publish_reaches_async_subscriber(self) -> None:
        sync_client = channels.create_sync_redis_client()
        async_client = channels.create_async_redis_client()
        pubsub = async_client.pubsub()
        await pubsub.subscribe("channel-1")

        messages: list[str] = []

        async def consume_one() -> None:
            async for payload in pubsub.listen():
                messages.append(str(payload["data"]))
                break

        consumer = asyncio.create_task(consume_one())
        await asyncio.sleep(0)
        sync_client.publish("channel-1", "hello")
        await asyncio.wait_for(consumer, timeout=1)
        self.assertEqual(messages, ["hello"])
        await pubsub.unsubscribe("channel-1")
