from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.api.context import ApiContext
from app.core.config import Settings, get_settings
from app.domain import ConnectorHealthHistoryPayload, ConnectorHealthRetentionStatusPayload, Execution, ExecutionEvent
from app.runtime.native.errors import ExecutionNotFoundError
from app.services.connector_retention import ConnectorRetentionService
from app.services.connectors import ConnectorService
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

    async def get_model_usage(
            self,
            *,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            execution_id: str | None = None,
            provider: str | None = None,
            model: str | None = None,
    ):
        return aggregate_model_usage(
            await self.list_all_events(),
            executions=await self.list_all_executions(),
            workflow_id=workflow_id,
            agent_id=agent_id,
            execution_id=execution_id,
            provider=provider,
            model=model,
        )

    async def get_connector_history(
            self,
            owner_user_id: str,
            *,
            limit: int = 20,
            offset: int = 0,
            status: str | None = None,
            started_after=None,
            started_before=None,
            provider: str | None = None,
    ) -> ConnectorHealthHistoryPayload:
        return await ConnectorService(self.context).list_all_history_for_owner(
            owner_user_id,
            limit=limit,
            offset=offset,
            status=status,
            started_after=started_after,
            started_before=started_before,
            provider=provider,
        )

    def get_connector_retention_status(self, settings: Settings | None = None) -> ConnectorHealthRetentionStatusPayload:
        return ConnectorRetentionService(self.context).get_status(settings or get_settings())

    def get_api_token_activity(
            self,
            owner_user_id: str,
            *,
            limit: int = 20,
            token_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self.context.runtime_operations.snapshot_dict()
        items = [
            action
            for action in reversed(snapshot["recent_actions"])
            if str(action.get("action", "")).startswith("api_token.")
               and action.get("owner_user_id") == owner_user_id
               and (token_id is None or action.get("token_id") == token_id)
        ]
        return {
            "items": items[:limit],
            "total": len(items),
        }
