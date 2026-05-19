from __future__ import annotations

from typing import Protocol

from app.domain import ExecutionEvent


class BaseEventExporter(Protocol):
    def export_event(self, event: ExecutionEvent) -> None: ...
