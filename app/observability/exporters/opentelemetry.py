from __future__ import annotations

from app.domain import ExecutionEvent

try:
    from opentelemetry import trace
except Exception:  # pragma: no cover
    trace = None


class OpenTelemetryExporter:
    def __init__(self):
        self.tracer = trace.get_tracer("agency.observability") if trace else None

    def export_event(self, event: ExecutionEvent) -> None:
        if self.tracer is None:
            return
        with self.tracer.start_as_current_span(event.event_type.value) as span:
            span.set_attribute("execution.id", event.execution_id)
            if event.workflow_id:
                span.set_attribute("workflow.id", event.workflow_id)
            if event.agent_id:
                span.set_attribute("agent.id", event.agent_id)
            if event.task_id:
                span.set_attribute("task.id", event.task_id)
            span.set_attribute("event.sequence", event.sequence)
