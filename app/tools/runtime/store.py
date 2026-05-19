from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from pydantic import Field

from app.core.config import get_settings
from app.core.time import utc_now
from app.domain import DomainModel
from app.tools.contracts.models import ToolRunResponse


DEFAULT_TOOL_RUN_STORE_PATH = Path(".data/executions/tool_runs.jsonl")


class ToolRunRecord(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tool_name: str
    tool_version: str
    actor: str | None = None
    input_json: dict[str, Any]
    output_json: dict[str, Any]
    policy_verdict_json: dict[str, Any] | None = None
    verdict: str
    dry_run: bool
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    input_hash: str
    output_hash: str
    signature: str | None = None


class JsonlToolRunStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or get_settings().tool_run_store_path)
        self._lock = Lock()

    def append(
            self,
            *,
            tool_name: str,
            tool_version: str,
            actor: str | None,
            input_payload: dict[str, Any],
            output: ToolRunResponse,
    ) -> ToolRunRecord:
        output_payload = output.model_dump(mode="json")
        record = ToolRunRecord(
            tool_name=tool_name,
            tool_version=tool_version,
            actor=actor,
            input_json=input_payload,
            output_json=output_payload,
            policy_verdict_json=(
                output.policyVerdict.model_dump(mode="json") if output.policyVerdict is not None else None
            ),
            verdict=output.verdict,
            dry_run=output.dryRun,
            input_hash=_hash_payload(input_payload),
            output_hash=_hash_payload(output_payload),
            signature=output.signature,
        )
        line = json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
        return record

    def list_records(self) -> list[ToolRunRecord]:
        if not self.path.exists():
            return []
        records: list[ToolRunRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    records.append(ToolRunRecord.model_validate(json.loads(stripped)))
        return records


def _hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
