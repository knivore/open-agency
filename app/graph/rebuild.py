"""Neo4j graph rebuild orchestration from the graph projection outbox."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.graph.neo4j_projection import NEO4J_PROJECTED_LABELS, Neo4jGraphProjector


@dataclass(slots=True)
class GraphRebuildResult:
    dry_run: bool = False
    cleared: bool = False
    reset_events: int = 0
    processed: int = 0
    failed: int = 0
    checkpoint_event_id: str | None = None
    errors: list[str] = field(default_factory=list)
    projected_labels: list[str] = field(default_factory=lambda: list(NEO4J_PROJECTED_LABELS))

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "cleared": self.cleared,
            "reset_events": self.reset_events,
            "processed": self.processed,
            "failed": self.failed,
            "checkpoint_event_id": self.checkpoint_event_id,
            "errors": self.errors,
            "projected_labels": self.projected_labels,
        }


class Neo4jGraphRebuilder:
    """Rebuild Neo4j by replaying the durable graph projection outbox."""

    def __init__(
            self,
            event_repository,
            projector: Neo4jGraphProjector,
            *,
            batch_size: int = 100,
            projected_labels: list[str] | None = None,
    ):
        self.event_repository = event_repository
        self.projector = projector
        self.batch_size = max(batch_size, 1)
        self.projected_labels = projected_labels or list(NEO4J_PROJECTED_LABELS)

    async def rebuild(
            self,
            *,
            clear: bool = False,
            ensure_schema: bool = True,
            dry_run: bool = False,
    ) -> GraphRebuildResult:
        if dry_run:
            summary = await self.event_repository.status_summary()
            return GraphRebuildResult(
                dry_run=True,
                reset_events=sum(
                    int(summary.get(key, 0) or 0)
                    for key in ("pending_count", "projected_count", "failed_count")
                ),
                projected_labels=list(self.projected_labels),
            )

        result = GraphRebuildResult(projected_labels=list(self.projected_labels))
        if ensure_schema:
            await self.projector.ensure_schema()
        if clear:
            await self.projector.clear_projection(labels=list(self.projected_labels))
            result.cleared = True
        result.reset_events = await self.event_repository.reset_for_replay()

        while True:
            batch_result = await self.projector.project_pending(self.event_repository, limit=self.batch_size)
            result.processed += batch_result.processed
            result.failed += batch_result.failed
            result.errors.extend(batch_result.errors)
            if batch_result.checkpoint_event_id is not None:
                result.checkpoint_event_id = batch_result.checkpoint_event_id
            if batch_result.processed == 0 and batch_result.failed == 0:
                break
            if batch_result.failed > 0:
                break
        return result


__all__ = ["GraphRebuildResult", "Neo4jGraphRebuilder"]
