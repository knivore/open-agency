from __future__ import annotations

import asyncio
import os
import queue
import redis
import redis.asyncio as redis_async
import threading
from collections import defaultdict
from typing import Any

_CHANNEL_BACKEND_ENV = "AGENCY_CHANNEL_BACKEND"
_AUTO = "auto"
_REDIS = "redis"
_INMEMORY = "inmemory"


class _InMemoryChannelBroker:
    def __init__(self) -> None:
        self._sync_subscribers: dict[str, list[queue.Queue[str]]] = defaultdict(list)
        self._async_subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]]] = defaultdict(
            list)
        self._lock = threading.Lock()

    def register_sync(self, channel: str, subscriber: queue.Queue[str]) -> None:
        with self._lock:
            self._sync_subscribers[channel].append(subscriber)

    def unregister_sync(self, channel: str, subscriber: queue.Queue[str]) -> None:
        with self._lock:
            subscribers = self._sync_subscribers.get(channel, [])
            if subscriber in subscribers:
                subscribers.remove(subscriber)
            if not subscribers:
                self._sync_subscribers.pop(channel, None)

    def register_async(self, channel: str, subscriber: asyncio.Queue[str], loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._async_subscribers[channel].append((loop, subscriber))

    def unregister_async(self, channel: str, subscriber: asyncio.Queue[str]) -> None:
        with self._lock:
            subscribers = self._async_subscribers.get(channel, [])
            self._async_subscribers[channel] = [(loop, item) for loop, item in subscribers if item is not subscriber]
            if not self._async_subscribers[channel]:
                self._async_subscribers.pop(channel, None)

    def publish(self, channel: str, message: str) -> int:
        with self._lock:
            sync_subscribers = list(self._sync_subscribers.get(channel, []))
            async_subscribers = list(self._async_subscribers.get(channel, []))
        for subscriber in sync_subscribers:
            subscriber.put_nowait(message)
        for loop, subscriber in async_subscribers:
            loop.call_soon_threadsafe(subscriber.put_nowait, message)
        return len(sync_subscribers) + len(async_subscribers)


_BROKER = _InMemoryChannelBroker()
_backend_mode: str | None = None
_backend_lock = threading.Lock()


def _resolve_backend_mode() -> str:
    global _backend_mode
    if _backend_mode is not None:
        return _backend_mode
    with _backend_lock:
        if _backend_mode is not None:
            return _backend_mode
        explicit = os.getenv(_CHANNEL_BACKEND_ENV, _AUTO).strip().lower()
        if explicit in {_REDIS, _INMEMORY}:
            _backend_mode = explicit
            return _backend_mode
        client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=0,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        try:
            client.ping()
            _backend_mode = _REDIS
        except redis.RedisError:
            _backend_mode = _INMEMORY
        finally:
            try:
                client.close()
            except Exception:
                pass
        return _backend_mode


class _InMemorySyncPubSub:
    def __init__(self, broker: _InMemoryChannelBroker) -> None:
        self._broker = broker
        self._subscriptions: dict[str, queue.Queue[str]] = {}

    def subscribe(self, channel: str) -> None:
        subscriber = queue.Queue[str]()
        self._subscriptions[channel] = subscriber
        self._broker.register_sync(channel, subscriber)

    def get_message(self) -> dict[str, Any] | None:
        for channel, subscriber in list(self._subscriptions.items()):
            try:
                message = subscriber.get_nowait()
            except queue.Empty:
                continue
            return {"type": "message", "channel": channel, "data": message}
        return None

    def close(self) -> None:
        for channel, subscriber in list(self._subscriptions.items()):
            self._broker.unregister_sync(channel, subscriber)
        self._subscriptions.clear()


class _InMemorySyncRedisClient:
    def __init__(self, broker: _InMemoryChannelBroker) -> None:
        self._broker = broker

    def publish(self, channel: str, message: str) -> int:
        return self._broker.publish(channel, message)

    def pubsub(self) -> _InMemorySyncPubSub:
        return _InMemorySyncPubSub(self._broker)


class _InMemoryAsyncPubSub:
    def __init__(self, broker: _InMemoryChannelBroker) -> None:
        self._broker = broker
        self._subscriptions: dict[str, asyncio.Queue[str]] = {}

    async def subscribe(self, channel: str) -> None:
        subscriber: asyncio.Queue[str] = asyncio.Queue()
        self._subscriptions[channel] = subscriber
        self._broker.register_async(channel, subscriber, asyncio.get_running_loop())

    async def listen(self):
        while self._subscriptions:
            tasks = [asyncio.create_task(subscriber.get()) for subscriber in self._subscriptions.values()]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for pending_task in pending:
                    pending_task.cancel()
                for completed in done:
                    yield {"type": "message", "data": completed.result()}
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()

    async def unsubscribe(self, channel: str) -> None:
        subscriber = self._subscriptions.pop(channel, None)
        if subscriber is not None:
            self._broker.unregister_async(channel, subscriber)


class _InMemoryAsyncRedisClient:
    def __init__(self, broker: _InMemoryChannelBroker) -> None:
        self._broker = broker

    async def publish(self, channel: str, message: str) -> int:
        return self._broker.publish(channel, message)

    def pubsub(self) -> _InMemoryAsyncPubSub:
        return _InMemoryAsyncPubSub(self._broker)


def create_sync_redis_client() -> redis.Redis | _InMemorySyncRedisClient:
    if _resolve_backend_mode() == _INMEMORY:
        return _InMemorySyncRedisClient(_BROKER)
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=0,
        decode_responses=True,
    )


def create_async_redis_client() -> redis_async.Redis | _InMemoryAsyncRedisClient:
    if _resolve_backend_mode() == _INMEMORY:
        return _InMemoryAsyncRedisClient(_BROKER)
    return redis_async.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=0,
        decode_responses=True,
    )


def agent_output_channel(process_id: str | int) -> str:
    return f"agent_output_channel_{process_id}"


def human_reply_channel(process_id: str | int) -> str:
    return f"human_reply_channel_{process_id}"


__all__ = [
    "agent_output_channel",
    "create_async_redis_client",
    "create_sync_redis_client",
    "human_reply_channel",
]
