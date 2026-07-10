"""No-op graph projection worker and replay helpers.

The worker intentionally validates and checkpoints outbox events without writing
to Neo4j yet. This gives the graph migration a stable, replayable event boundary
before adding an external graph database dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.domain import GraphProjectionEvent

logger = logging.getLogger(__name__)


@dataclass
class ProjectionBatchResult:
    processed: int = 0
    failed: int = 0
    checkpoint_event_id: str | None = None
    errors: list[str] = field(default_factory=list)


class GraphProjectionWorker:
    def __init__(self, event_repository, *, batch_size: int = 100):
        self.event_repository = event_repository
        self.batch_size = batch_size
        self.checkpoint_event_id: str | None = None

    async def run_once(self) -> ProjectionBatchResult:
        events = await self.event_repository.list_events(status="pending", limit=self.batch_size)
        result = ProjectionBatchResult(checkpoint_event_id=self.checkpoint_event_id)
        for event in events:
            try:
                self._validate_event(event)
                logger.info(
                    "Validated graph projection event",
                    extra={
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "aggregate_type": event.aggregate_type,
                        "aggregate_id": event.aggregate_id,
                    },
                )
                await self.event_repository.mark_projected(event.event_id)
                self.checkpoint_event_id = event.event_id
                result.checkpoint_event_id = event.event_id
                result.processed += 1
            except Exception as exc:
                message = str(exc)
                await self.event_repository.mark_failed(event.event_id, message)
                result.failed += 1
                result.errors.append(f"{event.event_id}: {message}")
        return result

    async def replay(
            self,
            *,
            event_ids: list[str] | None = None,
            run: bool = False,
    ) -> ProjectionBatchResult:
        await self.event_repository.reset_for_replay(event_ids=event_ids)
        if not run:
            return ProjectionBatchResult(checkpoint_event_id=self.checkpoint_event_id)
        return await self.run_once()

    def _validate_event(self, event: GraphProjectionEvent) -> None:
        GraphProjectionEvent.model_validate(event.model_dump(mode="json"))
