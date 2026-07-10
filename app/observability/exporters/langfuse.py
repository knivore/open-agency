from __future__ import annotations

import hashlib
from typing import Any

from app.domain import ExecutionEvent
from app.domain.events import ExecutionEventType

FAILURE_EVENT_TYPES = {
    ExecutionEventType.EXECUTION_FAILED,
    ExecutionEventType.TOOL_CALL_FAILED,
    ExecutionEventType.CONTAINER_FAILED,
    ExecutionEventType.RUNTIME_BUILD_FAILED,
}


class _PendingObservation:
    def __init__(self, *, input_payload: dict[str, Any], metadata: dict[str, Any]):
        self.input_payload = input_payload
        self.metadata = metadata


class LangfuseExporter:
    def __init__(self, client: Any | None = None, *, enabled: bool | None = None):
        self.client = client
        self.enabled = bool(client) if enabled is None else enabled
        self._pending_llm_requests: dict[str, _PendingObservation] = {}
        self._pending_tool_calls: dict[str, _PendingObservation] = {}
        if client is not None:
            return
        try:  # pragma: no cover
            from langfuse import get_client

            self.client = get_client()
            self.enabled = True
        except Exception:
            try:
                from langfuse import Langfuse

                self.client = Langfuse()
                self.enabled = True
            except Exception:
                self.enabled = False

    def export_event(self, event: ExecutionEvent) -> None:
        if not self.enabled or self.client is None:
            return
        try:  # pragma: no cover - depends on optional langfuse package/network configuration
            self._export_event(event)
        except Exception:
            return

    def _export_event(self, event: ExecutionEvent) -> None:
        metadata = self._metadata(event)
        if event.event_type == ExecutionEventType.LLM_REQUEST_CREATED and event.model_request_id:
            self._pending_llm_requests[event.model_request_id] = _PendingObservation(
                input_payload=event.payload,
                metadata=metadata,
            )
            self._create_observation(event, as_type="span", input_payload=event.payload, metadata=metadata)
            return
        if event.event_type == ExecutionEventType.LLM_RESPONSE_CREATED:
            pending = self._pending_llm_requests.pop(event.model_request_id or "", None)
            request_payload = pending.input_payload if pending else {}
            request_metadata = pending.metadata if pending else {}
            combined_metadata = {**request_metadata, **metadata}
            self._create_observation(
                event,
                as_type="generation",
                input_payload=request_payload.get("messages") or request_payload,
                output_payload={
                    "content": event.payload.get("content"),
                    "tool_calls": event.payload.get("tool_calls"),
                },
                metadata=combined_metadata,
                model=event.metrics.get("model_name") or event.payload.get("model_name"),
                usage_details=self._usage_details(event),
            )
            return
        if event.event_type == ExecutionEventType.TOOL_CALL_STARTED and event.tool_call_id:
            self._pending_tool_calls[event.tool_call_id] = _PendingObservation(
                input_payload=event.payload,
                metadata=metadata,
            )
            self._create_observation(event, as_type="tool", input_payload=event.payload, metadata=metadata)
            return
        if event.event_type in {ExecutionEventType.TOOL_CALL_COMPLETED, ExecutionEventType.TOOL_CALL_FAILED}:
            pending = self._pending_tool_calls.pop(event.tool_call_id or "", None)
            start_payload = pending.input_payload if pending else {}
            start_metadata = pending.metadata if pending else {}
            self._create_observation(
                event,
                as_type="tool",
                input_payload=start_payload.get("arguments") or start_payload,
                output_payload=event.payload.get(
                    "output") if event.event_type == ExecutionEventType.TOOL_CALL_COMPLETED else None,
                metadata={**start_metadata, **metadata},
                status_message=event.payload.get("error"),
            )
            return
        self._create_observation(event, as_type="span", input_payload=event.payload, metadata=metadata)

    def _create_observation(
            self,
            event: ExecutionEvent,
            *,
            as_type: str,
            input_payload: Any = None,
            output_payload: Any = None,
            metadata: dict[str, Any],
            model: str | None = None,
            usage_details: dict[str, int] | None = None,
            status_message: str | None = None,
    ) -> None:
        if hasattr(self.client, "start_observation"):
            kwargs = {
                "trace_context": {"trace_id": self._langfuse_trace_id(event)},
                "name": event.event_type.value,
                "as_type": as_type,
                "input": input_payload,
                "output": output_payload,
                "metadata": metadata,
                "level": "ERROR" if event.event_type in FAILURE_EVENT_TYPES else "DEFAULT",
                "status_message": status_message,
            }
            if model:
                kwargs["model"] = model
            if usage_details:
                kwargs["usage_details"] = usage_details
            try:
                observation = self.client.start_observation(**kwargs)
            except TypeError:
                kwargs.pop("trace_context", None)
                observation = self.client.start_observation(**kwargs)
            if hasattr(observation, "end"):
                observation.end()
            return
        self._legacy_event(event, input_payload, metadata)

    def _legacy_event(self, event: ExecutionEvent, input_payload: Any, metadata: dict[str, Any]) -> None:
        if hasattr(self.client, "event"):
            self.client.event(
                id=event.id,
                name=event.event_type.value,
                trace_id=event.trace_id or event.execution_id,
                input=input_payload,
                metadata=metadata,
            )
        elif hasattr(self.client, "create_event"):
            self.client.create_event(
                id=event.id,
                name=event.event_type.value,
                trace_id=event.trace_id or event.execution_id,
                input=input_payload,
                metadata=metadata,
            )

    def _metadata(self, event: ExecutionEvent) -> dict[str, Any]:
        return {
            **event.metadata,
            "execution_id": event.execution_id,
            "workflow_id": event.workflow_id,
            "agent_id": event.agent_id,
            "task_id": event.task_id,
            "tool_call_id": event.tool_call_id,
            "model_request_id": event.model_request_id,
            "actor_type": event.actor_type,
            "actor_id": event.actor,
            "sequence": event.sequence,
            "timestamp": event.timestamp.isoformat(),
            "agency_trace_id": event.trace_id,
            "redacted_fields": event.redacted_fields,
            "metrics": event.metrics,
        }

    def _langfuse_trace_id(self, event: ExecutionEvent) -> str:
        source = event.trace_id or event.execution_id
        normalized = "".join(char for char in source.lower() if char in "0123456789abcdef")
        if len(normalized) == 32 and normalized != "0" * 32:
            return normalized
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
        return digest if digest != "0" * 32 else "1" * 32

    def _usage_details(self, event: ExecutionEvent) -> dict[str, int]:
        usage = event.payload.get("usage", {}) if isinstance(event.payload.get("usage"), dict) else {}
        input_tokens = event.metrics.get("input_tokens") or usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        output_tokens = (
                event.metrics.get("output_tokens")
                or usage.get("output_tokens")
                or usage.get("completion_tokens")
                or 0
        )
        total_tokens = event.metrics.get("total_tokens") or usage.get("total_tokens") or input_tokens + output_tokens
        return {
            "input": int(input_tokens or 0),
            "output": int(output_tokens or 0),
            "total": int(total_tokens or 0),
        }
