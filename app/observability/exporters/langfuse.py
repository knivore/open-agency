from __future__ import annotations

from app.domain import ExecutionEvent


class LangfuseExporter:
    def __init__(self):
        self.client = None
        self.enabled = False
        try:  # pragma: no cover
            from langfuse import Langfuse

            self.client = Langfuse()
            self.enabled = True
        except Exception:
            try:
                from langfuse import get_client

                self.client = get_client()
                self.enabled = True
            except Exception:
                self.enabled = False

    def export_event(self, event: ExecutionEvent) -> None:
        if not self.enabled or self.client is None:
            return
        try:  # pragma: no cover - depends on optional langfuse package/network configuration
            if hasattr(self.client, "event"):
                self.client.event(
                    id=event.id,
                    name=event.event_type.value,
                    trace_id=event.trace_id or event.execution_id,
                    input=event.payload,
                    metadata={
                        **event.metadata,
                        "execution_id": event.execution_id,
                        "workflow_id": event.workflow_id,
                        "agent_id": event.agent_id,
                        "tool_call_id": event.tool_call_id,
                        "model_request_id": event.model_request_id,
                        "sequence": event.sequence,
                        "redacted_fields": event.redacted_fields,
                    },
                )
            elif hasattr(self.client, "create_event"):
                self.client.create_event(
                    id=event.id,
                    name=event.event_type.value,
                    trace_id=event.trace_id or event.execution_id,
                    input=event.payload,
                    metadata=event.metadata,
                )
        except Exception:
            return
