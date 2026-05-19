from __future__ import annotations

import json
import os
from pathlib import Path

from app.domain import ExecutionEvent


class JSONLExporter:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.getenv("OBSERVABILITY_JSONL_PATH", "logs/observability.jsonl"))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export_event(self, event: ExecutionEvent) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json")) + "\n")
