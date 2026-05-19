from __future__ import annotations

from app.runtime.adapters.base import RuntimeAdapterUnavailableError, RuntimeAdapterUnsupportedError


class CrewAIRuntimeAdapterError(RuntimeError):
    pass


class CrewAIUnavailableError(RuntimeAdapterUnavailableError, CrewAIRuntimeAdapterError):
    pass


class CrewAIUnsupportedOperationError(RuntimeAdapterUnsupportedError, CrewAIRuntimeAdapterError):
    pass
