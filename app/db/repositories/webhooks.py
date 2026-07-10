"""Repositories for persisted outbound webhook delivery attempts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import OutboundWebhookAttemptORM
from app.domain import OutboundWebhookAttempt


def _attempt_to_orm(attempt: OutboundWebhookAttempt) -> OutboundWebhookAttemptORM:
    return OutboundWebhookAttemptORM(
        id=attempt.id,
        event_id=attempt.event_id,
        target=attempt.target,
        url_hash=attempt.url_hash,
        idempotency_key=attempt.idempotency_key,
        request_payload_sha256=attempt.request_payload_sha256,
        response_status=attempt.response_status,
        response_body_preview=attempt.response_body_preview,
        attempt_no=attempt.attempt_no,
        status=attempt.status,
        error_message=attempt.error_message,
        created_at=attempt.created_at,
    )


def _attempt_from_orm(orm: OutboundWebhookAttemptORM) -> OutboundWebhookAttempt:
    return OutboundWebhookAttempt.model_validate(
        {
            "id": orm.id,
            "event_id": orm.event_id,
            "target": orm.target,
            "url_hash": orm.url_hash,
            "idempotency_key": orm.idempotency_key,
            "request_payload_sha256": orm.request_payload_sha256,
            "response_status": orm.response_status,
            "response_body_preview": orm.response_body_preview,
            "attempt_no": orm.attempt_no,
            "status": orm.status,
            "error_message": orm.error_message,
            "created_at": orm.created_at,
        }
    )


class InMemoryOutboundWebhookAttemptRepository:
    def __init__(self):
        self._items: list[OutboundWebhookAttempt] = []

    async def create_attempt(self, attempt: OutboundWebhookAttempt) -> OutboundWebhookAttempt:
        self._items.append(attempt)
        return attempt

    async def list_attempts(self, *, event_id: str | None = None, target: str | None = None) -> list[
        OutboundWebhookAttempt
    ]:
        items = list(self._items)
        if event_id is not None:
            items = [item for item in items if item.event_id == event_id]
        if target is not None:
            items = [item for item in items if item.target == target]
        return items


class SQLOutboundWebhookAttemptRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def create_attempt(self, attempt: OutboundWebhookAttempt) -> OutboundWebhookAttempt:
        async with self.session_factory() as session:
            entity = _attempt_to_orm(attempt)
            session.add(entity)
            await session.commit()
            return _attempt_from_orm(entity)

    async def list_attempts(self, *, event_id: str | None = None, target: str | None = None) -> list[
        OutboundWebhookAttempt
    ]:
        async with self.session_factory() as session:
            stmt = select(OutboundWebhookAttemptORM).order_by(OutboundWebhookAttemptORM.created_at.asc())
            if event_id is not None:
                stmt = stmt.where(OutboundWebhookAttemptORM.event_id == event_id)
            if target is not None:
                stmt = stmt.where(OutboundWebhookAttemptORM.target == target)
            result = await session.execute(stmt)
            return [_attempt_from_orm(item) for item in result.scalars().all()]
