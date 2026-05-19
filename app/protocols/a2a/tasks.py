from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any

from app.domain import Execution


class A2ATaskCreate(BaseModel):
    workflow_id: str | None = Field(default=None, alias="workflowId")
    agent_id: str | None = Field(default=None, alias="agentId")
    input: dict[str, Any] = Field(default_factory=dict)
    trigger: dict[str, Any] = Field(default_factory=dict)
    runtime_adapter_id: str | None = Field(default=None, alias="runtimeAdapterId")
    message: dict[str, Any] | None = None


def execution_to_a2a_task(execution: Execution) -> dict[str, Any]:
    return {
        "id": execution.id,
        "status": execution.status.value,
        "input": execution.input_payload,
        "output": execution.output_payload,
        "error": execution.error,
        "created_at": execution.created_at.isoformat(),
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
        "metadata": execution.metadata,
    }
