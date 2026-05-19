from __future__ import annotations

from importlib.util import find_spec

from app.runtime.adapters.base import RuntimeAdapterCapability, RuntimeAdapterStatus
from .errors import CrewAIUnavailableError


def is_crewai_installed() -> bool:
    return find_spec("crewai") is not None


def get_crewai_status() -> RuntimeAdapterStatus:
    installed = is_crewai_installed()
    return RuntimeAdapterStatus(
        adapter_name="crewai",
        available=installed,
        detail=None if installed else "CrewAI is not installed",
        capabilities=(
            RuntimeAdapterCapability.START,
            RuntimeAdapterCapability.OBSERVE,
        ),
    )


def ensure_crewai_available() -> None:
    status = get_crewai_status()
    if not status.available:
        raise CrewAIUnavailableError(status.detail or "CrewAI is unavailable")
