"""Workspace target helpers shared by runtime-facing repo edit flows."""

from __future__ import annotations

import os


def _workspace_target(env_name: str, default: str) -> str:
    configured = os.getenv(env_name)
    if configured and configured.strip():
        return configured.strip()
    return default


def default_repo_write_mounts() -> list[dict[str, str]]:
    # Keep approval payloads aligned with runtime mount targets so humans approve
    # the same container paths workers will actually receive.
    return [
        {
            "repo": "open-agency",
            "target": _workspace_target("AGENCY_BACKEND_WORKSPACE", "/workspace/open-agency"),
            "mode": "rw",
            "source_env": "AGENCY_BACKEND_HOST_WORKSPACE",
        },
        {
            "repo": "open-agency-fe",
            "target": _workspace_target("AGENCY_FRONTEND_WORKSPACE", "/workspace/open-agency-fe"),
            "mode": "rw",
            "source_env": "AGENCY_FRONTEND_HOST_WORKSPACE",
        },
    ]
