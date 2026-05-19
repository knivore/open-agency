from __future__ import annotations

import hashlib
import json

from app.core.time import utc_now
from app.tools.contracts.models import FileChanged, PolicyVerdict, ToolRunResponse


def build_tool_run_response(
        *,
        verdict: str,
        policy_verdict: PolicyVerdict | None,
        patch: str | None,
        result: dict | None = None,
        files_changed: list[FileChanged] | None = None,
        errors: list[str] | None = None,
        dry_run: bool = True,
        actor: str | None = None,
) -> ToolRunResponse:
    response = ToolRunResponse(
        verdict=verdict,  # type: ignore[arg-type]
        policyVerdict=policy_verdict,
        result=result,
        patch=patch,
        filesChanged=files_changed or [],
        errors=errors or [],
        dryRun=dry_run,
        timestamp=utc_now().isoformat(),
        actor=actor,
    )
    response.signature = sign_tool_run_payload(response.model_dump(mode="json", exclude={"signature"}))
    return response


def sign_tool_run_payload(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
