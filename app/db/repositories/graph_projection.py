"""Repositories for graph projection outbox events."""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.time import utc_now
from app.db.models import GraphProjectionEventORM
from app.domain import GraphProjectionEvent


def _event_to_orm(event: GraphProjectionEvent) -> GraphProjectionEventORM:
    return GraphProjectionEventORM(
        event_id=event.event_id,
        event_type=event.event_type,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        occurred_at=event.occurred_at,
        tenant_id=event.tenant_id,
        user_id=event.user_id,
        payload_json=event.payload,
        schema_version=event.schema_version,
        source=event.source,
        source_event_id=event.source_event_id,
        status=event.status,
        attempts=event.attempts,
        projected_at=event.projected_at,
        last_error=event.last_error,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


def _event_from_orm(orm: GraphProjectionEventORM) -> GraphProjectionEvent:
    return GraphProjectionEvent.model_validate(
        {
            "event_id": orm.event_id,
            "event_type": orm.event_type,
            "aggregate_type": orm.aggregate_type,
            "aggregate_id": orm.aggregate_id,
            "occurred_at": orm.occurred_at,
            "tenant_id": orm.tenant_id,
            "user_id": orm.user_id,
            "payload": orm.payload_json,
            "schema_version": orm.schema_version,
            "source": orm.source,
            "source_event_id": orm.source_event_id,
            "status": orm.status,
            "attempts": orm.attempts,
            "projected_at": orm.projected_at,
            "last_error": orm.last_error,
            "created_at": orm.created_at,
            "updated_at": orm.updated_at,
        }
    )


EXECUTION_AGGREGATE_TYPES = {"workflow_run", "step_run"}
MEMORY_AGGREGATE_TYPES = {"memory", "document_memory_collection", "workflow_memory_link"}


def _projection_lag_seconds(
        *,
        oldest_pending_at: datetime | None,
        latest_event_at: datetime | None,
        last_projected_event_at: datetime | None,
) -> float | None:
    now = utc_now()
    if oldest_pending_at is not None:
        return _seconds_between(now, oldest_pending_at)
    if latest_event_at is None:
        return None
    if last_projected_event_at is None:
        return _seconds_between(now, latest_event_at)
    if latest_event_at > last_projected_event_at:
        return _seconds_between(latest_event_at, last_projected_event_at)
    return 0.0


def _seconds_between(later: datetime, earlier: datetime) -> float:
    if later.tzinfo is None:
        later = later.replace(tzinfo=timezone.utc)
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=timezone.utc)
    return max((later - earlier).total_seconds(), 0.0)


def _health_status(*, pending_count: int, failed_count: int, projection_lag_seconds: float | None) -> str:
    if failed_count > 0:
        return "degraded"
    if pending_count > 0:
        return "lagging"
    if projection_lag_seconds is None:
        return "empty"
    return "healthy"


class InMemoryGraphProjectionEventRepository:
    def __init__(self):
        self._items: dict[str, GraphProjectionEvent] = {}

    async def append(self, event: GraphProjectionEvent) -> GraphProjectionEvent:
        if event.source_event_id:
            existing = next(
                (
                    item
                    for item in self._items.values()
                    if item.source == event.source and item.source_event_id == event.source_event_id
                ),
                None,
            )
            if existing is not None:
                return existing
        self._items[event.event_id] = event
        return event

    async def list_events(
            self,
            *,
            status: str | None = None,
            after_event_id: str | None = None,
            limit: int = 100,
    ) -> list[GraphProjectionEvent]:
        items = sorted(self._items.values(), key=lambda item: (item.occurred_at, item.event_id))
        if status is not None:
            items = [item for item in items if item.status == status]
        if after_event_id is not None:
            seen = False
            filtered: list[GraphProjectionEvent] = []
            for item in items:
                if seen:
                    filtered.append(item)
                elif item.event_id == after_event_id:
                    seen = True
            items = filtered
        return items[: max(limit, 0)]

    async def mark_projected(self, event_id: str) -> GraphProjectionEvent | None:
        event = self._items.get(event_id)
        if event is None:
            return None
        updated = event.model_copy(update={"status": "projected", "projected_at": utc_now(), "updated_at": utc_now()})
        self._items[event_id] = updated
        return updated

    async def mark_failed(self, event_id: str, error: str) -> GraphProjectionEvent | None:
        event = self._items.get(event_id)
        if event is None:
            return None
        updated = event.model_copy(
            update={
                "status": "failed",
                "attempts": event.attempts + 1,
                "last_error": error[:2000],
                "updated_at": utc_now(),
            }
        )
        self._items[event_id] = updated
        return updated

    async def reset_for_replay(self, *, event_ids: list[str] | None = None) -> int:
        selected = set(event_ids or [])
        count = 0
        for event_id, event in list(self._items.items()):
            if selected and event_id not in selected:
                continue
            self._items[event_id] = event.model_copy(
                update={"status": "pending", "projected_at": None, "last_error": None, "updated_at": utc_now()}
            )
            count += 1
        return count

    async def status_summary(self, *, recent_failure_limit: int = 5) -> dict:
        items = list(self._items.values())
        pending = [item for item in items if item.status == "pending"]
        failed = [item for item in items if item.status == "failed"]
        projected = [item for item in items if item.status == "projected"]
        recent_failures = sorted(failed, key=lambda item: (item.updated_at, item.event_id), reverse=True)
        oldest_pending = min((item.occurred_at for item in pending), default=None)
        last_projected = max((item.projected_at for item in projected if item.projected_at is not None), default=None)
        latest_event = max((item.occurred_at for item in items), default=None)
        last_projected_event = max((item.occurred_at for item in projected), default=None)
        last_projected_execution_event = max(
            (item.occurred_at for item in projected if item.aggregate_type in EXECUTION_AGGREGATE_TYPES),
            default=None,
        )
        last_projected_memory_event = max(
            (item.occurred_at for item in projected if item.aggregate_type in MEMORY_AGGREGATE_TYPES),
            default=None,
        )
        projection_lag = _projection_lag_seconds(
            oldest_pending_at=oldest_pending,
            latest_event_at=latest_event,
            last_projected_event_at=last_projected_event,
        )
        return {
            "health_status": _health_status(
                pending_count=len(pending),
                failed_count=len(failed),
                projection_lag_seconds=projection_lag,
            ),
            "total_count": len(items),
            "pending_count": len(pending),
            "failed_count": len(failed),
            "projected_count": len(projected),
            "projection_lag_seconds": projection_lag,
            "latest_event_at": latest_event,
            "last_projected_at": last_projected,
            "last_projected_event_at": last_projected_event,
            "last_projected_execution_event_at": last_projected_execution_event,
            "last_projected_memory_event_at": last_projected_memory_event,
            "oldest_pending_at": oldest_pending,
            "last_error": recent_failures[0].last_error if recent_failures else None,
            "recent_failures": [
                {
                    "event_id": item.event_id,
                    "event_type": item.event_type,
                    "aggregate_type": item.aggregate_type,
                    "aggregate_id": item.aggregate_id,
                    "attempts": item.attempts,
                    "last_error": item.last_error,
                    "updated_at": item.updated_at,
                }
                for item in recent_failures[: max(recent_failure_limit, 0)]
            ],
        }


class SQLGraphProjectionEventRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def append(self, event: GraphProjectionEvent) -> GraphProjectionEvent:
        async with self.session_factory() as session:
            entity = _event_to_orm(event)
            session.add(entity)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if event.source_event_id:
                    existing = await self.get_by_source_event(event.source, event.source_event_id)
                    if existing is not None:
                        return existing
                raise
            return _event_from_orm(entity)

    async def get_by_source_event(self, source: str, source_event_id: str) -> GraphProjectionEvent | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(GraphProjectionEventORM).where(
                    GraphProjectionEventORM.source == source,
                    GraphProjectionEventORM.source_event_id == source_event_id,
                )
            )
            entity = result.scalar_one_or_none()
            return None if entity is None else _event_from_orm(entity)

    async def list_events(
            self,
            *,
            status: str | None = None,
            after_event_id: str | None = None,
            limit: int = 100,
    ) -> list[GraphProjectionEvent]:
        async with self.session_factory() as session:
            stmt = select(GraphProjectionEventORM).order_by(
                GraphProjectionEventORM.occurred_at.asc(),
                GraphProjectionEventORM.event_id.asc(),
            )
            if status is not None:
                stmt = stmt.where(GraphProjectionEventORM.status == status)
            if after_event_id is not None:
                anchor = await session.get(GraphProjectionEventORM, after_event_id)
                if anchor is not None:
                    stmt = stmt.where(GraphProjectionEventORM.occurred_at >= anchor.occurred_at)
            result = await session.execute(stmt.limit(max(limit, 0)))
            items = [_event_from_orm(item) for item in result.scalars().all()]
            if after_event_id is not None:
                items = [item for item in items if item.event_id != after_event_id]
            return items

    async def mark_projected(self, event_id: str) -> GraphProjectionEvent | None:
        async with self.session_factory() as session:
            entity = await session.get(GraphProjectionEventORM, event_id)
            if entity is None:
                return None
            entity.status = "projected"
            entity.projected_at = utc_now()
            entity.updated_at = utc_now()
            await session.commit()
            return _event_from_orm(entity)

    async def mark_failed(self, event_id: str, error: str) -> GraphProjectionEvent | None:
        async with self.session_factory() as session:
            entity = await session.get(GraphProjectionEventORM, event_id)
            if entity is None:
                return None
            entity.status = "failed"
            entity.attempts += 1
            entity.last_error = error[:2000]
            entity.updated_at = utc_now()
            await session.commit()
            return _event_from_orm(entity)

    async def reset_for_replay(self, *, event_ids: list[str] | None = None) -> int:
        async with self.session_factory() as session:
            stmt = select(GraphProjectionEventORM)
            if event_ids:
                stmt = stmt.where(GraphProjectionEventORM.event_id.in_(event_ids))
            result = await session.execute(stmt)
            count = 0
            for entity in result.scalars().all():
                entity.status = "pending"
                entity.projected_at = None
                entity.last_error = None
                entity.updated_at = utc_now()
                count += 1
            await session.commit()
            return count

    async def status_summary(self, *, recent_failure_limit: int = 5) -> dict:
        async with self.session_factory() as session:
            pending_count = await session.scalar(
                select(func.count()).select_from(GraphProjectionEventORM).where(
                    GraphProjectionEventORM.status == "pending")
            )
            failed_count = await session.scalar(
                select(func.count()).select_from(GraphProjectionEventORM).where(
                    GraphProjectionEventORM.status == "failed")
            )
            projected_count = await session.scalar(
                select(func.count()).select_from(GraphProjectionEventORM).where(
                    GraphProjectionEventORM.status == "projected")
            )
            total_count = await session.scalar(select(func.count()).select_from(GraphProjectionEventORM))
            latest_event_at = await session.scalar(select(func.max(GraphProjectionEventORM.occurred_at)))
            last_projected_at = await session.scalar(select(func.max(GraphProjectionEventORM.projected_at)))
            last_projected_event_at = await session.scalar(
                select(func.max(GraphProjectionEventORM.occurred_at)).where(
                    GraphProjectionEventORM.status == "projected")
            )
            last_projected_execution_event_at = await session.scalar(
                select(func.max(GraphProjectionEventORM.occurred_at)).where(
                    GraphProjectionEventORM.status == "projected",
                    GraphProjectionEventORM.aggregate_type.in_(EXECUTION_AGGREGATE_TYPES),
                )
            )
            last_projected_memory_event_at = await session.scalar(
                select(func.max(GraphProjectionEventORM.occurred_at)).where(
                    GraphProjectionEventORM.status == "projected",
                    GraphProjectionEventORM.aggregate_type.in_(MEMORY_AGGREGATE_TYPES),
                )
            )
            oldest_pending_at = await session.scalar(
                select(func.min(GraphProjectionEventORM.occurred_at)).where(GraphProjectionEventORM.status == "pending")
            )
            failures_result = await session.execute(
                select(GraphProjectionEventORM)
                .where(GraphProjectionEventORM.status == "failed")
                .order_by(GraphProjectionEventORM.updated_at.desc(), GraphProjectionEventORM.event_id.desc())
                .limit(max(recent_failure_limit, 0))
            )
            failures = [_event_from_orm(item) for item in failures_result.scalars().all()]
            pending_count = int(pending_count or 0)
            failed_count = int(failed_count or 0)
            projected_count = int(projected_count or 0)
            projection_lag = _projection_lag_seconds(
                oldest_pending_at=oldest_pending_at,
                latest_event_at=latest_event_at,
                last_projected_event_at=last_projected_event_at,
            )
            return {
                "health_status": _health_status(
                    pending_count=pending_count,
                    failed_count=failed_count,
                    projection_lag_seconds=projection_lag,
                ),
                "total_count": int(total_count or 0),
                "pending_count": pending_count,
                "failed_count": failed_count,
                "projected_count": projected_count,
                "projection_lag_seconds": projection_lag,
                "latest_event_at": latest_event_at,
                "last_projected_at": last_projected_at,
                "last_projected_event_at": last_projected_event_at,
                "last_projected_execution_event_at": last_projected_execution_event_at,
                "last_projected_memory_event_at": last_projected_memory_event_at,
                "oldest_pending_at": oldest_pending_at,
                "last_error": failures[0].last_error if failures else None,
                "recent_failures": [
                    {
                        "event_id": item.event_id,
                        "event_type": item.event_type,
                        "aggregate_type": item.aggregate_type,
                        "aggregate_id": item.aggregate_id,
                        "attempts": item.attempts,
                        "last_error": item.last_error,
                        "updated_at": item.updated_at,
                    }
                    for item in failures
                ],
            }
