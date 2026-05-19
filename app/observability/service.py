from __future__ import annotations

from dataclasses import dataclass

from app.api.context import ApiContext
from app.domain import Execution, ExecutionEvent
from app.runtime.native.errors import ExecutionNotFoundError
from .metrics import aggregate_agent_metrics, aggregate_model_usage, aggregate_workflow_metrics, build_timeline


@dataclass(slots=True)
class ObservabilityService:
    context: ApiContext

    async def list_all_executions(self) -> list[Execution]:
        if hasattr(self.context.execution_store, "list_all_executions"):
            return await self.context.execution_store.list_all_executions()
        if hasattr(self.context.execution_store, "_executions"):
            return list(self.context.execution_store._executions.values())
        if hasattr(self.context.execution_store, "_db"):
            items = []
            cursor = self.context.execution_store._executions.find({})
            async for record in cursor:
                record.pop("_id", None)
                items.append(Execution.model_validate(record))
            return items
        return []

    async def list_all_events(self) -> list[ExecutionEvent]:
        if hasattr(self.context.execution_store, "list_all_events"):
            return await self.context.execution_store.list_all_events()
        if hasattr(self.context.execution_store, "_events"):
            events = []
            for item in self.context.execution_store._events.values():
                events.extend(item)
            return events
        if hasattr(self.context.execution_store, "_db"):
            items = []
            cursor = self.context.execution_store._events.find({})
            async for record in cursor:
                record.pop("_id", None)
                items.append(ExecutionEvent.model_validate(record))
            return items
        return []

    async def get_execution_timeline(self, execution_id: str):
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        events = await self.context.execution_store.list_events(execution_id)
        return build_timeline(execution, events)

    async def get_agent_metrics(self, agent_id: str):
        return aggregate_agent_metrics(await self.list_all_executions(), await self.list_all_events(),
                                       agent_id=agent_id)

    async def get_workflow_metrics(self, workflow_id: str):
        return aggregate_workflow_metrics(await self.list_all_executions(), await self.list_all_events(),
                                          workflow_id=workflow_id)

    async def get_model_usage(self):
        return aggregate_model_usage(await self.list_all_events())
