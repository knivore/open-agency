from __future__ import annotations

import asyncio
import unittest

from app.runtime.streaming import (
    DEFAULT_RUNTIME_EVENT_QUEUE_SIZE,
    RuntimeEventBus,
    RuntimeEventPublisher,
    RuntimeEventType,
    RuntimeStreamEvent,
    get_default_runtime_event_bus,
    set_default_runtime_event_bus,
)


class FailingQueue(asyncio.Queue[RuntimeStreamEvent]):
    def put_nowait(self, item: RuntimeStreamEvent) -> None:
        raise RuntimeError("subscriber failed")


class RuntimeEventBusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        set_default_runtime_event_bus(None)

    async def test_publish_delivers_to_multiple_subscribers(self):
        bus = RuntimeEventBus()
        first = await bus.subscribe()
        second = await bus.subscribe()
        event = RuntimeStreamEvent(type=RuntimeEventType.TASK_STARTED)

        result = await bus.publish(event)

        self.assertEqual(result.subscribers, 2)
        self.assertEqual(result.delivered, 2)
        self.assertIs(await first.get(), event)
        self.assertIs(await second.get(), event)

    async def test_unsubscribe_stops_delivery(self):
        bus = RuntimeEventBus()
        subscriber = await bus.subscribe()
        await bus.unsubscribe(subscriber)

        result = await bus.publish(RuntimeStreamEvent(type=RuntimeEventType.TASK_COMPLETED))

        self.assertEqual(result.subscribers, 0)
        self.assertEqual(result.delivered, 0)
        self.assertEqual(await bus.subscriber_count(), 0)

    async def test_subscriber_queue_is_bounded(self):
        bus = RuntimeEventBus(max_queue_size=1)
        subscriber = await bus.subscribe()

        self.assertEqual(subscriber.maxsize, 1)
        self.assertEqual(DEFAULT_RUNTIME_EVENT_QUEUE_SIZE, 256)

    async def test_full_subscriber_queue_does_not_block_other_subscribers(self):
        bus = RuntimeEventBus(max_queue_size=1)
        slow = await bus.subscribe()
        fast = await bus.subscribe()
        queued = RuntimeStreamEvent(type=RuntimeEventType.TASK_STARTED)
        event = RuntimeStreamEvent(type=RuntimeEventType.TASK_PROGRESS)

        slow.put_nowait(queued)
        result = await bus.publish(event)

        self.assertEqual(result.subscribers, 2)
        self.assertEqual(result.delivered, 1)
        self.assertEqual(result.dropped, 1)
        self.assertIs(await fast.get(), event)
        self.assertIs(await slow.get(), queued)

    async def test_bad_subscriber_does_not_break_bus(self):
        bus = RuntimeEventBus()
        good = await bus.subscribe()
        async with bus._lock:
            bus._subscribers.add(FailingQueue())

        result = await bus.publish(RuntimeStreamEvent(type=RuntimeEventType.LOG_RECEIVED))

        self.assertEqual(result.subscribers, 2)
        self.assertEqual(result.delivered, 1)
        self.assertEqual(result.errors, 1)
        self.assertEqual((await good.get()).type, RuntimeEventType.LOG_RECEIVED)

    async def test_publisher_uses_default_bus(self):
        bus = RuntimeEventBus()
        set_default_runtime_event_bus(bus)
        subscriber = await get_default_runtime_event_bus().subscribe()
        publisher = RuntimeEventPublisher()

        result = await publisher.publish(RuntimeStreamEvent(type=RuntimeEventType.WORKFLOW_TRANSITIONED))

        self.assertEqual(result.delivered, 1)
        self.assertEqual((await subscriber.get()).type, RuntimeEventType.WORKFLOW_TRANSITIONED)


if __name__ == "__main__":
    unittest.main()
