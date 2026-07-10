"""Agent-facing service helpers that sit above repositories and execution storage."""

from __future__ import annotations

from dataclasses import dataclass

from app.api.context import ApiContext


@dataclass(slots=True)
class AgentService:
    """Read agent-related runtime views that do not belong in catalog CRUD routes."""

    context: ApiContext

    async def list_agent_executions(self, agent_id: str) -> dict[str, list[dict]]:
        items = await self.context.execution_store.list_executions_by_agent(agent_id)
        return {"items": [item.model_dump(mode="json") for item in items]}
