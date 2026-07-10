"""Durable repository for ambient-home pending actions and audit records."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from typing import Protocol

from app.db.models.ambient_actions import AmbientActionAuditORM, AmbientPendingActionORM
from app.db.session import get_session_maker, is_database_configured
from app.domain import AmbientActionAuditRecord, PendingAmbientAction


class AmbientActionRepository(Protocol):
    async def save_pending_action(self, action: PendingAmbientAction) -> PendingAmbientAction: ...

    async def get_pending_action(self, action_id: str) -> PendingAmbientAction | None: ...

    async def list_pending_actions(self) -> list[PendingAmbientAction]: ...

    async def append_audit_record(self, record: AmbientActionAuditRecord) -> AmbientActionAuditRecord: ...

    async def list_audit_records(self) -> list[AmbientActionAuditRecord]: ...


class InMemoryAmbientActionRepository:
    def __init__(self) -> None:
        self._actions: dict[str, PendingAmbientAction] = {}
        self._audit_records: dict[str, AmbientActionAuditRecord] = {}

    async def save_pending_action(self, action: PendingAmbientAction) -> PendingAmbientAction:
        self._actions[action.id] = action
        return action

    async def get_pending_action(self, action_id: str) -> PendingAmbientAction | None:
        return self._actions.get(action_id)

    async def list_pending_actions(self) -> list[PendingAmbientAction]:
        return sorted(self._actions.values(), key=lambda item: item.created_at, reverse=True)

    async def append_audit_record(self, record: AmbientActionAuditRecord) -> AmbientActionAuditRecord:
        self._audit_records[record.id] = record
        return record

    async def list_audit_records(self) -> list[AmbientActionAuditRecord]:
        return sorted(self._audit_records.values(), key=lambda item: item.created_at, reverse=True)


class SQLAmbientActionRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def save_pending_action(self, action: PendingAmbientAction) -> PendingAmbientAction:
        async with self.session_factory() as session:
            existing = await session.get(AmbientPendingActionORM, action.id)
            if existing is None:
                entity = AmbientPendingActionORM(
                    id=action.id,
                    status=action.status,
                    action_type=action.action_type,
                    summary=action.summary,
                    risk_level=action.risk_level,
                    audit_category=action.audit_category,
                    confirmation_required=action.confirmation_required,
                    expires_at=_ensure_utc(action.expires_at),
                    executed_at=_ensure_optional_utc(action.executed_at),
                    resolved_at=_ensure_optional_utc(action.resolved_at),
                    action_payload_json=dict(action.action_payload),
                    result_payload_json=dict(action.result_payload) if isinstance(action.result_payload,
                                                                                  dict) else action.result_payload,
                    metadata_json=dict(action.metadata),
                )
                session.add(entity)
            else:
                entity = existing
                entity.status = action.status
                entity.action_type = action.action_type
                entity.summary = action.summary
                entity.risk_level = action.risk_level
                entity.audit_category = action.audit_category
                entity.confirmation_required = action.confirmation_required
                entity.expires_at = _ensure_utc(action.expires_at)
                entity.executed_at = _ensure_optional_utc(action.executed_at)
                entity.resolved_at = _ensure_optional_utc(action.resolved_at)
                entity.action_payload_json = dict(action.action_payload)
                entity.result_payload_json = (
                    dict(action.result_payload) if isinstance(action.result_payload, dict) else action.result_payload
                )
                entity.metadata_json = dict(action.metadata)
            await session.commit()
            await session.refresh(entity)
            return _action_from_orm(entity)

    async def get_pending_action(self, action_id: str) -> PendingAmbientAction | None:
        async with self.session_factory() as session:
            entity = await session.get(AmbientPendingActionORM, action_id)
            return _action_from_orm(entity) if entity is not None else None

    async def list_pending_actions(self) -> list[PendingAmbientAction]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(AmbientPendingActionORM).order_by(AmbientPendingActionORM.created_at.desc())
            )
            return [_action_from_orm(item) for item in result.scalars().all()]

    async def append_audit_record(self, record: AmbientActionAuditRecord) -> AmbientActionAuditRecord:
        async with self.session_factory() as session:
            entity = AmbientActionAuditORM(
                id=record.id,
                action_id=record.action_id,
                event_type=record.event_type,
                summary=record.summary,
                risk_level=record.risk_level,
                audit_category=record.audit_category,
                created_at=_ensure_utc(record.created_at),
                metadata_json=dict(record.metadata),
            )
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
            return _audit_from_orm(entity)

    async def list_audit_records(self) -> list[AmbientActionAuditRecord]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(AmbientActionAuditORM).order_by(AmbientActionAuditORM.created_at.desc())
            )
            return [_audit_from_orm(item) for item in result.scalars().all()]


class ResilientAmbientActionRepository:
    """Prefer SQL when available, but fall back to in-memory storage until migrations exist.

    This keeps local tests and partially migrated dev environments usable while the
    feature lands, without forcing every code path to special-case table readiness.
    """

    def __init__(self, primary: AmbientActionRepository, fallback: InMemoryAmbientActionRepository):
        self.primary = primary
        self.fallback = fallback
        self._using_fallback = False

    async def save_pending_action(self, action: PendingAmbientAction) -> PendingAmbientAction:
        if self._using_fallback:
            return await self.fallback.save_pending_action(action)
        try:
            return await self.primary.save_pending_action(action)
        except SQLAlchemyError:
            self._using_fallback = True
            return await self.fallback.save_pending_action(action)

    async def get_pending_action(self, action_id: str) -> PendingAmbientAction | None:
        if self._using_fallback:
            return await self.fallback.get_pending_action(action_id)
        try:
            return await self.primary.get_pending_action(action_id)
        except SQLAlchemyError:
            self._using_fallback = True
            return await self.fallback.get_pending_action(action_id)

    async def list_pending_actions(self) -> list[PendingAmbientAction]:
        if self._using_fallback:
            return await self.fallback.list_pending_actions()
        try:
            return await self.primary.list_pending_actions()
        except SQLAlchemyError:
            self._using_fallback = True
            return await self.fallback.list_pending_actions()

    async def append_audit_record(self, record: AmbientActionAuditRecord) -> AmbientActionAuditRecord:
        if self._using_fallback:
            return await self.fallback.append_audit_record(record)
        try:
            return await self.primary.append_audit_record(record)
        except SQLAlchemyError:
            self._using_fallback = True
            return await self.fallback.append_audit_record(record)

    async def list_audit_records(self) -> list[AmbientActionAuditRecord]:
        if self._using_fallback:
            return await self.fallback.list_audit_records()
        try:
            return await self.primary.list_audit_records()
        except SQLAlchemyError:
            self._using_fallback = True
            return await self.fallback.list_audit_records()


def _action_from_orm(entity: AmbientPendingActionORM) -> PendingAmbientAction:
    return PendingAmbientAction(
        id=entity.id,
        status=entity.status,
        action_type=entity.action_type,
        summary=entity.summary,
        risk_level=entity.risk_level,
        audit_category=entity.audit_category,
        confirmation_required=entity.confirmation_required,
        created_at=_ensure_utc(entity.created_at),
        expires_at=_ensure_utc(entity.expires_at),
        executed_at=_ensure_optional_utc(entity.executed_at),
        resolved_at=_ensure_optional_utc(entity.resolved_at),
        action_payload=dict(entity.action_payload_json or {}),
        result_payload=dict(entity.result_payload_json) if isinstance(entity.result_payload_json,
                                                                      dict) else entity.result_payload_json,
        metadata=dict(entity.metadata_json or {}),
    )


def _audit_from_orm(entity: AmbientActionAuditORM) -> AmbientActionAuditRecord:
    return AmbientActionAuditRecord(
        id=entity.id,
        action_id=entity.action_id,
        event_type=entity.event_type,
        summary=entity.summary,
        risk_level=entity.risk_level,
        audit_category=entity.audit_category,
        created_at=_ensure_utc(entity.created_at),
        metadata=dict(entity.metadata_json or {}),
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ensure_optional_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _ensure_utc(value)


@lru_cache(maxsize=1)
def get_ambient_action_repository() -> AmbientActionRepository:
    if is_database_configured():
        session_factory = get_session_maker()
        if session_factory is not None:
            fallback = InMemoryAmbientActionRepository()
            return ResilientAmbientActionRepository(SQLAmbientActionRepository(session_factory), fallback)
    return InMemoryAmbientActionRepository()
