from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .runtime_event_models import AGENCY_RUNTIME_EVENT_SCHEMA_VERSION, RuntimeStreamEvent
from .stream_safety import safe_runtime_event_payload, safe_runtime_events_payload

RUNTIME_STREAM_AUTH_ENV = "AGENCY_RUNTIME_STREAM_API_KEY"
RUNTIME_STREAM_EVENT_NAME = "runtime_event"
RUNTIME_STREAM_CONNECTED_EVENT_NAME = "runtime_stream.connected"
RUNTIME_STREAM_HEARTBEAT_EVENT_NAME = "runtime_stream.heartbeat"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def runtime_stream_connected_payload(*, last_event_id: str | None = None) -> dict[str, Any]:
    payload = {
        "type": RUNTIME_STREAM_CONNECTED_EVENT_NAME,
        "schemaVersion": AGENCY_RUNTIME_EVENT_SCHEMA_VERSION,
        "timestamp": utc_timestamp(),
    }
    if last_event_id:
        payload["lastEventId"] = last_event_id
    return payload


def runtime_stream_heartbeat_payload() -> dict[str, Any]:
    return {
        "type": RUNTIME_STREAM_HEARTBEAT_EVENT_NAME,
        "timestamp": utc_timestamp(),
    }


def format_sse_message(
        *,
        event_name: str,
        data: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        event_id: str | None = None,
        retry_ms: int | None = None,
) -> str:
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {event_id}")
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    lines.append(f"event: {event_name}")
    serialized = json.dumps(data, separators=(",", ":"))
    for line in serialized.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def format_runtime_event_sse(event: RuntimeStreamEvent) -> str:
    return format_sse_message(
        event_id=event.id,
        event_name=RUNTIME_STREAM_EVENT_NAME,
        data=safe_runtime_event_payload(event),
    )


def format_runtime_events_sse(events: list[RuntimeStreamEvent]) -> str:
    return format_sse_message(
        event_id=events[-1].id if events else None,
        event_name=RUNTIME_STREAM_EVENT_NAME,
        data=safe_runtime_events_payload(events),
    )
