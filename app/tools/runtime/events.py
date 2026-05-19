from __future__ import annotations

import asyncio
from typing import Any

from app.runtime.streaming import (
    RuntimeEventActor,
    RuntimeEventLevel,
    RuntimeEventPublisher,
    RuntimeEventType,
    RuntimeStreamEvent,
)


def publish_tool_runtime_event(
        *,
        lifecycle_type: str,
        tool_name: str,
        actor: str | None = None,
        verdict: str | None = None,
        metadata: dict[str, Any] | None = None,
) -> None:
    event = RuntimeStreamEvent(
        type=_runtime_event_type(lifecycle_type, verdict),
        actor=RuntimeEventActor(id=actor or "agent/runtime", role="tool-runner"),
        level=_runtime_event_level(verdict),
        message=_runtime_event_message(lifecycle_type, tool_name, verdict),
        metadata={
            "semanticType": lifecycle_type,
            "tool": tool_name,
            "verdict": verdict,
            **(metadata or {}),
        },
    )
    _publish_without_blocking(event)


def _runtime_event_type(lifecycle_type: str, verdict: str | None) -> RuntimeEventType:
    if lifecycle_type == "tool.run.started":
        return RuntimeEventType.TOOL_STARTED
    if lifecycle_type == "tool.policy.completed":
        return RuntimeEventType.TOOL_COMPLETED
    if verdict == "deny":
        return RuntimeEventType.TOOL_FAILED
    return RuntimeEventType.TOOL_COMPLETED


def _runtime_event_level(verdict: str | None) -> RuntimeEventLevel:
    if verdict == "deny":
        return RuntimeEventLevel.ERROR
    if verdict == "warn":
        return RuntimeEventLevel.WARNING
    if verdict == "ok":
        return RuntimeEventLevel.SUCCESS
    return RuntimeEventLevel.INFO


def _runtime_event_message(lifecycle_type: str, tool_name: str, verdict: str | None) -> str:
    if lifecycle_type == "tool.run.started":
        return f"{tool_name} run started"
    if lifecycle_type == "tool.policy.completed":
        return f"{tool_name} policy completed with verdict {verdict}"
    return f"{tool_name} run completed with verdict {verdict}"


def _publish_without_blocking(event: RuntimeStreamEvent) -> None:
    publisher = RuntimeEventPublisher()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(publisher.publish(event))
        return
    loop.create_task(publisher.publish(event))
