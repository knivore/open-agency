from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any

from app.domain import ExecutionEvent


class A2AMessageCreate(BaseModel):
    role: str = "user"
    content: Any
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifact: dict[str, Any] | None = None


def execution_event_to_a2a_message(event: ExecutionEvent) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {"value": event.payload}
    return {
        "id": event.id,
        "task_id": event.execution_id,
        "role": event.actor or "system",
        "content": payload.get("content", payload),
        "event_type": event.event_type.value,
        "timestamp": event.timestamp.isoformat(),
        "sequence": event.sequence,
        "metadata": event.metadata,
    }
