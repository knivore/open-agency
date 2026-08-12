"""Coordinate safe tunnel reload requests between the API and local launcher."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from app.services.tunnel_preferences import TunnelPreference

TunnelRuntimeState = Literal["idle", "requested", "applying", "ready", "failed"]


class TunnelRuntimeControl(BaseModel):
    request_id: str | None = None
    state: TunnelRuntimeState = "idle"
    provider: str | None = None
    requested_at: datetime | None = None
    updated_at: datetime | None = None
    supervisor_updated_at: datetime | None = None
    message: str | None = None

    @property
    def supervisor_available(self) -> bool:
        return bool(
            self.supervisor_updated_at
            and datetime.now(timezone.utc) - self.supervisor_updated_at < timedelta(seconds=10)
        )


def resolve_tunnel_runtime_control_path() -> Path:
    configured = os.getenv("AGENCY_TUNNEL_RUNTIME_CONTROL_PATH")
    if configured:
        return Path(configured).expanduser()

    workspace = os.getenv("AGENCY_BACKEND_WORKSPACE")
    if workspace:
        return Path(workspace).expanduser() / ".agency" / "tunnel-runtime-control.json"

    return Path.cwd() / ".agency" / "tunnel-runtime-control.json"


class TunnelRuntimeControlService:
    """Persist declarative requests; the host launcher performs the actual process work."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or resolve_tunnel_runtime_control_path()

    def status(self) -> TunnelRuntimeControl:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return TunnelRuntimeControl.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValueError):
            return TunnelRuntimeControl()

    def request_apply(self, preference: TunnelPreference) -> TunnelRuntimeControl:
        now = datetime.now(timezone.utc)
        control = TunnelRuntimeControl(
            request_id=uuid4().hex,
            state="requested",
            provider=preference.provider,
            requested_at=now,
            updated_at=now,
            message="Waiting for the local launcher to reload the public tunnel.",
        )
        self._write(control)
        return control

    def clear(self) -> None:
        self._write(TunnelRuntimeControl())

    def _write(self, control: TunnelRuntimeControl) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(control.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
