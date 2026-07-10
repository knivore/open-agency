from __future__ import annotations

import os
from typing import Iterable

from app.domain import ExecutionEvent
from .exporters.jsonl import JSONLExporter
from .exporters.langfuse import LangfuseExporter
from .exporters.opentelemetry import OpenTelemetryExporter
from .redaction import Redactor


class EventBus:
    def __init__(self, *, exporters: Iterable[object] | None = None, redact_secrets: bool | None = None):
        self.redactor = Redactor(enabled=(os.getenv("OBSERVABILITY_REDACT_SECRETS",
                                                    "true").lower() == "true") if redact_secrets is None else redact_secrets)
        self.exporters = list(exporters or self._build_exporters())

    def _build_exporters(self) -> list[object]:
        configured = [item.strip() for item in os.getenv("OBSERVABILITY_EXPORTERS", "jsonl").split(",") if item.strip()]
        exporters: list[object] = []
        for name in configured:
            if name == "jsonl":
                exporters.append(JSONLExporter())
            elif name == "opentelemetry":
                exporters.append(OpenTelemetryExporter())
            elif name == "langfuse":
                exporters.append(LangfuseExporter())
        return exporters

    def publish(self, event: ExecutionEvent) -> ExecutionEvent:
        payload, fields = self.redactor.redact_value(event.payload)
        metrics, metric_fields = self.redactor.redact_value(event.metrics)
        metadata, meta_fields = self.redactor.redact_value(event.metadata)
        if fields or metric_fields or meta_fields:
            event.payload = payload
            event.metrics = metrics
            event.metadata = metadata
            event.redacted_fields = sorted(set([*event.redacted_fields, *fields, *metric_fields, *meta_fields]))
        for exporter in self.exporters:
            exporter.export_event(event)
        return event


_default_event_bus: EventBus | None = None


def get_default_event_bus() -> EventBus:
    global _default_event_bus
    if _default_event_bus is None:
        _default_event_bus = EventBus()
    return _default_event_bus


def set_default_event_bus(event_bus: EventBus | None) -> None:
    global _default_event_bus
    _default_event_bus = event_bus
