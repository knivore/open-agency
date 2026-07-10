"""Runtime stream filtering by workflow and agent identifiers."""

from __future__ import annotations

from dataclasses import dataclass, field

from .runtime_event_models import RuntimeStreamEvent


def _parse_filter_values(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


@dataclass(frozen=True, slots=True)
class RuntimeEventFilter:
    workflow_ids: set[str] = field(default_factory=set)
    agent_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_query(
            cls,
            *,
            workflow_id: str | None = None,
            agent_id: str | None = None,
    ) -> "RuntimeEventFilter":
        return cls(
            workflow_ids=_parse_filter_values(workflow_id),
            agent_ids=_parse_filter_values(agent_id),
        )

    def matches(self, event: RuntimeStreamEvent) -> bool:
        if self.workflow_ids:
            workflow_id = event.workflow.id if event.workflow else None
            if workflow_id not in self.workflow_ids:
                return False
        if self.agent_ids:
            agent_id = event.actor.id if event.actor else None
            if agent_id not in self.agent_ids:
                return False
        return True

    @property
    def is_empty(self) -> bool:
        return not self.workflow_ids and not self.agent_ids
