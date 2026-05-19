from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.domain import ExecutionArtifact, ExecutionEvent, ExecutionEventType, ToolDefinition
from app.runtime.native.errors import ToolExecutionError
from .artifacts import execution_artifact_to_a2a_artifact
from .messages import execution_event_to_a2a_message
from .tasks import execution_to_a2a_task


class A2AAdapter:
    def __init__(self, *, execution_store):
        self.execution_store = execution_store

    async def append_message(self, execution_id: str, message: dict[str, Any]) -> ExecutionEvent:
        existing = await self.execution_store.list_events(execution_id)
        sequence = max((event.sequence for event in existing), default=0) + 1
        event = ExecutionEvent(
            execution_id=execution_id,
            event_type=ExecutionEventType.AGENT_MESSAGE_CREATED,
            actor=message.get("role", "user"),
            payload={"content": message.get("content"), "metadata": message.get("metadata", {})},
            sequence=sequence,
        )
        await self.execution_store.save_event(event)
        return event

    async def append_artifact(self, execution_id: str, artifact: dict[str, Any]) -> ExecutionArtifact:
        execution_artifact = ExecutionArtifact(
            execution_id=execution_id,
            name=artifact.get("name", "artifact"),
            artifact_type=artifact.get("type", "generic"),
            uri=artifact.get("uri", f"a2a://{execution_id}/artifact"),
            media_type=artifact.get("media_type"),
            size_bytes=artifact.get("size_bytes"),
            metadata=artifact.get("metadata", {}),
        )
        await self.execution_store.save_artifact(execution_artifact)
        return execution_artifact

    async def call_remote_agent(self, tool: ToolDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
        stub_response = tool.implementation.config.get("stub_response")
        if stub_response is not None:
            return stub_response

        target = tool.implementation.target
        parsed = urlparse(target)
        if parsed.hostname and tool.security.allowlisted_domains and parsed.hostname not in tool.security.allowlisted_domains:
            raise ToolExecutionError(f"A2A remote host '{parsed.hostname}' is not allowlisted for tool '{tool.id}'")
        message = {
            "role": arguments.get("role", "user"),
            "content": arguments.get("content", arguments),
            "metadata": arguments.get("metadata", {}),
        }
        payload = {
            "input": arguments.get("input", {}),
            "trigger": {"created_by": "a2a_remote_agent"},
            "message": message,
        }
        request = Request(
            url=target.rstrip("/") + "/tasks",
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=tool.implementation.config.get("timeout", 30)) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
            return body if isinstance(body, dict) else {"result": body}

    async def get_task(self, execution_id: str) -> dict[str, Any] | None:
        execution = await self.execution_store.get_execution(execution_id)
        return None if execution is None else execution_to_a2a_task(execution)

    async def get_messages(self, execution_id: str) -> list[dict[str, Any]]:
        return [execution_event_to_a2a_message(event) for event in await self.execution_store.list_events(execution_id)]

    async def get_artifacts(self, execution_id: str) -> list[dict[str, Any]]:
        return [execution_artifact_to_a2a_artifact(artifact) for artifact in
                await self.execution_store.list_artifacts(execution_id)]
