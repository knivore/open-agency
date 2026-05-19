from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.domain import Execution, WorkflowDefinition


class BaseRuntimeAdapter(Protocol):
    adapter_name: str

    def get_status(self) -> "RuntimeAdapterStatus": ...

    async def supports(self, workflow_definition: WorkflowDefinition) -> bool: ...

    async def prepare_execution(self, execution: Execution) -> Execution: ...

    async def start_execution(self, execution_id: str) -> Execution: ...

    async def pause_execution(self, execution_id: str) -> Execution: ...

    async def resume_execution(self, execution_id: str) -> Execution: ...

    async def cancel_execution(self, execution_id: str) -> Execution: ...

    async def get_execution_state(self, execution_id: str): ...


class RuntimeAdapterCapability(str, Enum):
    START = "start"
    OBSERVE = "observe"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


@dataclass(frozen=True)
class RuntimeAdapterStatus:
    adapter_name: str
    available: bool
    detail: str | None = None
    capabilities: tuple[RuntimeAdapterCapability, ...] = ()


class RuntimeAdapterUnavailableError(RuntimeError):
    pass


class RuntimeAdapterUnsupportedError(RuntimeError):
    pass
