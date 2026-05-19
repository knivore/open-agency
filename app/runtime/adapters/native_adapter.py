from __future__ import annotations

from app.domain import Execution, WorkflowDefinition
from app.runtime.adapters.base import BaseRuntimeAdapter, RuntimeAdapterCapability, RuntimeAdapterStatus
from app.runtime.native.engine import ExecutionEngine


class NativeRuntimeAdapter(BaseRuntimeAdapter):
    adapter_name = "native"

    def __init__(self, engine: ExecutionEngine):
        self.engine = engine

    def get_status(self) -> RuntimeAdapterStatus:
        return RuntimeAdapterStatus(
            adapter_name=self.adapter_name,
            available=True,
            capabilities=(
                RuntimeAdapterCapability.START,
                RuntimeAdapterCapability.OBSERVE,
                RuntimeAdapterCapability.PAUSE,
                RuntimeAdapterCapability.RESUME,
                RuntimeAdapterCapability.CANCEL,
            ),
        )

    async def supports(self, workflow_definition: WorkflowDefinition) -> bool:
        return True

    async def prepare_execution(self, execution: Execution) -> Execution:
        return await self.engine.prepare_execution(execution)

    async def start_execution(self, execution_id: str) -> Execution:
        return await self.engine.start_execution(execution_id)

    async def pause_execution(self, execution_id: str) -> Execution:
        return await self.engine.pause_execution(execution_id)

    async def resume_execution(self, execution_id: str) -> Execution:
        return await self.engine.resume_execution(execution_id)

    async def cancel_execution(self, execution_id: str) -> Execution:
        return await self.engine.cancel_execution(execution_id)

    async def get_execution_state(self, execution_id: str):
        return await self.engine.get_execution_state(execution_id)
