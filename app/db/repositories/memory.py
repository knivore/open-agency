"""Memory repository implementations for in-memory and SQL-backed storage."""

from __future__ import annotations

from datetime import date
from sqlalchemy import cast, delete, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from typing import Any

from app.db.models import MemoryRecordORM
from app.domain import MemoryRecord
from .catalog import InMemoryCatalogRepository


class InMemoryMemoryRepository(InMemoryCatalogRepository[MemoryRecord]):
    def __init__(self):
        super().__init__(MemoryRecord)

    async def list(self, *, include_deleted: bool = False) -> list[MemoryRecord]:
        items = await super().list(include_deleted=include_deleted)
        return sorted(items, key=lambda item: (item.updated_at, item.id), reverse=True)

    async def query(
            self,
            *,
            scopes: list[str] | None = None,
            user_id: str | None = None,
            workspace_id: str | None = None,
            conversation_id: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            source: str | None = None,
            memory_types: list[str] | None = None,
            tags: list[str] | None = None,
            statuses: list[str] | None = None,
            source_conversation_id: str | None = None,
            source_execution_id: str | None = None,
            summary_date_from: date | None = None,
            summary_date_to: date | None = None,
            text: str | None = None,
            limit: int = 20,
    ) -> list[MemoryRecord]:
        needle = text.strip().lower() if isinstance(text, str) and text.strip() else None
        records = []
        for item in await self.list():
            if scopes and item.scope.value not in scopes:
                continue
            if user_id is not None and item.created_by_user_id != user_id:
                continue
            if workspace_id is not None and item.workspace_id != workspace_id:
                continue
            if conversation_id is not None and item.conversation_id != conversation_id:
                continue
            if workflow_id is not None and item.workflow_id != workflow_id:
                continue
            if agent_id is not None and item.agent_id != agent_id:
                continue
            if source is not None and item.source != source:
                continue
            if memory_types and (item.memory_type.value if item.memory_type is not None else None) not in memory_types:
                continue
            if tags and not all(tag in item.tags for tag in tags):
                continue
            if statuses and item.status.value not in statuses:
                continue
            if source_conversation_id is not None and item.source_conversation_id != source_conversation_id:
                continue
            if source_execution_id is not None and item.source_execution_id != source_execution_id:
                continue
            if summary_date_from is not None and (
                    item.summary_date is None or item.summary_date < summary_date_from
            ):
                continue
            if summary_date_to is not None and (
                    item.summary_date is None or item.summary_date > summary_date_to
            ):
                continue
            if needle and needle not in item.content.lower() and needle not in (item.summary or "").lower():
                continue
            records.append(item)
            if len(records) >= max(limit, 0):
                break
        return records


class SQLMemoryRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def _to_domain(self, orm: MemoryRecordORM) -> MemoryRecord:
        embedding = orm.embedding_vector or orm.embedding_json
        return MemoryRecord.model_validate(
            {
                "id": orm.id,
                "scope": orm.scope,
                "content": orm.content,
                "summary": orm.summary,
                "tags": orm.tags_json or [],
                "sensitive": orm.sensitive,
                "created_by_user_id": orm.created_by_user_id,
                "workspace_id": orm.workspace_id,
                "conversation_id": orm.conversation_id,
                "workflow_id": orm.workflow_id,
                "agent_id": orm.agent_id,
                "source": orm.source,
                "memory_type": orm.memory_type,
                "status": orm.status,
                "importance": orm.importance,
                "summary_date": orm.summary_date,
                "archived_window_start": orm.archived_window_start,
                "archived_window_end": orm.archived_window_end,
                "source_conversation_id": orm.source_conversation_id,
                "source_execution_id": orm.source_execution_id,
                "supersedes_memory_id": orm.supersedes_memory_id,
                "metadata": orm.metadata_json or {},
                "embedding": embedding,
                "embedding_model_profile_id": orm.embedding_model_profile_id,
                "embedding_model": orm.embedding_model,
                "embedding_dimensions": orm.embedding_dimensions,
                "embedded_at": orm.embedded_at,
                "last_used_at": orm.last_used_at,
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
            }
        )

    def _to_orm(self, item: MemoryRecord) -> MemoryRecordORM:
        return MemoryRecordORM(
            id=item.id,
            scope=item.scope.value,
            content=item.content,
            summary=item.summary,
            tags_json=item.tags,
            sensitive=item.sensitive,
            created_by_user_id=item.created_by_user_id,
            workspace_id=item.workspace_id,
            conversation_id=item.conversation_id,
            workflow_id=item.workflow_id,
            agent_id=item.agent_id,
            source=item.source,
            memory_type=item.memory_type.value if item.memory_type is not None else None,
            status=item.status.value,
            importance=item.importance,
            summary_date=item.summary_date,
            archived_window_start=item.archived_window_start,
            archived_window_end=item.archived_window_end,
            source_conversation_id=item.source_conversation_id,
            source_execution_id=item.source_execution_id,
            supersedes_memory_id=item.supersedes_memory_id,
            metadata_json=item.metadata,
            embedding_json=item.embedding,
            embedding_vector=item.embedding,
            embedding_model_profile_id=item.embedding_model_profile_id,
            embedding_model=item.embedding_model,
            embedding_dimensions=item.embedding_dimensions,
            embedded_at=item.embedded_at,
            last_used_at=item.last_used_at,
        )

    async def create(self, item: MemoryRecord) -> MemoryRecord:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
            return self._to_domain(entity)

    async def save(self, item: MemoryRecord) -> MemoryRecord:
        async with self.session_factory() as session:
            existing = await session.get(MemoryRecordORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "scope",
                        "content",
                        "summary",
                        "tags_json",
                        "sensitive",
                        "created_by_user_id",
                        "workspace_id",
                        "conversation_id",
                        "workflow_id",
                        "agent_id",
                        "source",
                        "memory_type",
                        "status",
                        "importance",
                        "summary_date",
                        "archived_window_start",
                        "archived_window_end",
                        "source_conversation_id",
                        "source_execution_id",
                        "supersedes_memory_id",
                        "metadata_json",
                        "embedding_json",
                        "embedding_vector",
                        "embedding_model_profile_id",
                        "embedding_model",
                        "embedding_dimensions",
                        "embedded_at",
                        "last_used_at",
                ):
                    setattr(entity, field, getattr(source, field))
            await session.commit()
            await session.refresh(entity)
            return self._to_domain(entity)

    async def get(self, item_id: str, *, include_deleted: bool = False) -> MemoryRecord | None:
        async with self.session_factory() as session:
            item = await session.get(MemoryRecordORM, item_id)
            return self._to_domain(item) if item is not None else None

    async def list(self, *, include_deleted: bool = False) -> list[MemoryRecord]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(MemoryRecordORM).order_by(MemoryRecordORM.updated_at.desc(), MemoryRecordORM.id.desc())
            )
            return [self._to_domain(item) for item in result.scalars().all()]

    async def update(self, item_id: str, patch: dict[str, Any]) -> MemoryRecord | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(MemoryRecord.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(delete(MemoryRecordORM).where(MemoryRecordORM.id == item_id))
            await session.commit()
            return result.rowcount > 0

    async def query(
            self,
            *,
            scopes: list[str] | None = None,
            user_id: str | None = None,
            workspace_id: str | None = None,
            conversation_id: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            source: str | None = None,
            memory_types: list[str] | None = None,
            tags: list[str] | None = None,
            statuses: list[str] | None = None,
            source_conversation_id: str | None = None,
            source_execution_id: str | None = None,
            summary_date_from: date | None = None,
            summary_date_to: date | None = None,
            text: str | None = None,
            limit: int = 20,
    ) -> list[MemoryRecord]:
        async with self.session_factory() as session:
            bind = session.get_bind()
            filter_tags_in_python = bool(tags)
            stmt = select(MemoryRecordORM)
            if scopes:
                stmt = stmt.where(MemoryRecordORM.scope.in_(scopes))
            if user_id is not None:
                stmt = stmt.where(MemoryRecordORM.created_by_user_id == user_id)
            if workspace_id is not None:
                stmt = stmt.where(MemoryRecordORM.workspace_id == workspace_id)
            if conversation_id is not None:
                stmt = stmt.where(MemoryRecordORM.conversation_id == conversation_id)
            if workflow_id is not None:
                stmt = stmt.where(MemoryRecordORM.workflow_id == workflow_id)
            if agent_id is not None:
                stmt = stmt.where(MemoryRecordORM.agent_id == agent_id)
            if source is not None:
                stmt = stmt.where(MemoryRecordORM.source == source)
            if memory_types:
                stmt = stmt.where(MemoryRecordORM.memory_type.in_(memory_types))
            if tags and bind is not None and bind.dialect.name == "postgresql":
                stmt = stmt.where(cast(MemoryRecordORM.tags_json, JSONB).contains(tags))
                filter_tags_in_python = False
            if statuses:
                stmt = stmt.where(MemoryRecordORM.status.in_(statuses))
            if source_conversation_id is not None:
                stmt = stmt.where(MemoryRecordORM.source_conversation_id == source_conversation_id)
            if source_execution_id is not None:
                stmt = stmt.where(MemoryRecordORM.source_execution_id == source_execution_id)
            if summary_date_from is not None:
                stmt = stmt.where(MemoryRecordORM.summary_date >= summary_date_from)
            if summary_date_to is not None:
                stmt = stmt.where(MemoryRecordORM.summary_date <= summary_date_to)
            if text and text.strip():
                pattern = f"%{text.strip()}%"
                stmt = stmt.where(or_(MemoryRecordORM.content.ilike(pattern), MemoryRecordORM.summary.ilike(pattern)))
            stmt = stmt.order_by(MemoryRecordORM.updated_at.desc(), MemoryRecordORM.id.desc())
            if not filter_tags_in_python:
                stmt = stmt.limit(max(limit, 0))
            result = await session.execute(stmt)
            records = [self._to_domain(item) for item in result.scalars().all()]
            if filter_tags_in_python and tags:
                records = [item for item in records if all(tag in item.tags for tag in tags)]
                records = records[:max(limit, 0)]
            return records

    async def query_by_embedding(
            self,
            *,
            embedding: list[float],
            scopes: list[str] | None = None,
            user_id: str | None = None,
            workspace_id: str | None = None,
            conversation_id: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            source: str | None = None,
            memory_types: list[str] | None = None,
            tags: list[str] | None = None,
            statuses: list[str] | None = None,
            source_conversation_id: str | None = None,
            source_execution_id: str | None = None,
            summary_date_from: date | None = None,
            summary_date_to: date | None = None,
            limit: int = 20,
    ) -> list[MemoryRecord]:
        async with self.session_factory() as session:
            bind = session.get_bind()
            if bind is None or bind.dialect.name != "postgresql":
                return []
            query_vector = self._format_vector(embedding)
            stmt = select(MemoryRecordORM).where(MemoryRecordORM.embedding_vector.is_not(None))
            if scopes:
                stmt = stmt.where(MemoryRecordORM.scope.in_(scopes))
            if user_id is not None:
                stmt = stmt.where(MemoryRecordORM.created_by_user_id == user_id)
            if workspace_id is not None:
                stmt = stmt.where(MemoryRecordORM.workspace_id == workspace_id)
            if conversation_id is not None:
                stmt = stmt.where(MemoryRecordORM.conversation_id == conversation_id)
            if workflow_id is not None:
                stmt = stmt.where(MemoryRecordORM.workflow_id == workflow_id)
            if agent_id is not None:
                stmt = stmt.where(MemoryRecordORM.agent_id == agent_id)
            if source is not None:
                stmt = stmt.where(MemoryRecordORM.source == source)
            if memory_types:
                stmt = stmt.where(MemoryRecordORM.memory_type.in_(memory_types))
            if tags:
                stmt = stmt.where(cast(MemoryRecordORM.tags_json, JSONB).contains(tags))
            if statuses:
                stmt = stmt.where(MemoryRecordORM.status.in_(statuses))
            if source_conversation_id is not None:
                stmt = stmt.where(MemoryRecordORM.source_conversation_id == source_conversation_id)
            if source_execution_id is not None:
                stmt = stmt.where(MemoryRecordORM.source_execution_id == source_execution_id)
            if summary_date_from is not None:
                stmt = stmt.where(MemoryRecordORM.summary_date >= summary_date_from)
            if summary_date_to is not None:
                stmt = stmt.where(MemoryRecordORM.summary_date <= summary_date_to)
            stmt = stmt.order_by(text("embedding_vector <=> :query_vector")).params(
                query_vector=query_vector,
            ).limit(max(limit, 0))
            result = await session.execute(stmt)
            return [self._to_domain(item) for item in result.scalars().all()]

    @staticmethod
    def _format_vector(embedding: list[float]) -> str:
        return "[" + ",".join(str(float(item)) for item in embedding) + "]"
