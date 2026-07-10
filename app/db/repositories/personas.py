"""Repositories for Persona Factory source-of-truth records."""

from __future__ import annotations

from sqlalchemy import select
from typing import Any, Generic, TypeVar

from app.db.models import (
    PersonaDistillationItemORM,
    PersonaDistillationRunORM,
    PersonaORM,
    PersonaSourceORM,
    PersonaVersionORM,
)
from app.domain import (
    PersonaDefinition,
    PersonaDistillationItem,
    PersonaDistillationRun,
    PersonaSource,
    PersonaStatus,
    PersonaVersion,
)
from .catalog import InMemoryCatalogRepository

T = TypeVar("T")


class InMemoryPersonaRepository(InMemoryCatalogRepository[PersonaDefinition]):
    def __init__(self):
        super().__init__(PersonaDefinition)

    async def find_by_slug(self, slug: str) -> PersonaDefinition | None:
        normalized = slug.strip().lower()
        for item in await self.list(include_deleted=True):
            if item.slug == normalized:
                return item
        return None

    async def list(self, *, include_deleted: bool = False) -> list[PersonaDefinition]:
        items = await super().list(include_deleted=include_deleted)
        if include_deleted:
            return sorted(items, key=lambda item: (item.updated_at, item.name), reverse=True)
        return sorted(
            [item for item in items if item.status != PersonaStatus.ARCHIVED],
            key=lambda item: (item.updated_at, item.name),
            reverse=True,
        )

    async def soft_delete(self, item_id: str) -> bool:
        updated = await self.update(item_id, {"status": PersonaStatus.ARCHIVED.value})
        return updated is not None


class InMemoryPersonaVersionRepository(InMemoryCatalogRepository[PersonaVersion]):
    def __init__(self):
        super().__init__(PersonaVersion)

    async def list_by_persona(self, persona_id: str) -> list[PersonaVersion]:
        items = [item for item in await self.list(include_deleted=True) if item.persona_id == persona_id]
        return sorted(items, key=lambda item: (item.created_at, item.version), reverse=True)


class InMemoryPersonaSourceRepository(InMemoryCatalogRepository[PersonaSource]):
    def __init__(self):
        super().__init__(PersonaSource)

    async def list_by_persona(self, persona_id: str) -> list[PersonaSource]:
        items = [item for item in await self.list(include_deleted=True) if item.persona_id == persona_id]
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)


class InMemoryPersonaDistillationRunRepository(InMemoryCatalogRepository[PersonaDistillationRun]):
    def __init__(self):
        super().__init__(PersonaDistillationRun)

    async def list_by_persona(self, persona_id: str) -> list[PersonaDistillationRun]:
        items = [item for item in await self.list(include_deleted=True) if item.persona_id == persona_id]
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)


class InMemoryPersonaDistillationItemRepository(InMemoryCatalogRepository[PersonaDistillationItem]):
    def __init__(self):
        super().__init__(PersonaDistillationItem)

    async def list_by_run(self, run_id: str) -> list[PersonaDistillationItem]:
        items = [item for item in await self.list(include_deleted=True) if item.run_id == run_id]
        return sorted(items, key=lambda item: (item.created_at, item.id))

    async def list_by_persona(self, persona_id: str) -> list[PersonaDistillationItem]:
        items = [item for item in await self.list(include_deleted=True) if item.persona_id == persona_id]
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)

    async def list_by_source_memory(self, source_memory_id: str) -> list[PersonaDistillationItem]:
        items = [
            item
            for item in await self.list(include_deleted=True)
            if item.source_memory_id == source_memory_id
        ]
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)


class _SQLPersonaRepositoryBase(Generic[T]):
    orm_cls: type
    domain_cls: type

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def _to_domain(self, orm):
        raise NotImplementedError

    def _to_orm(self, item: T):
        raise NotImplementedError

    async def create(self, item: T) -> T:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
            return self._to_domain(entity)

    async def save(self, item: T) -> T:
        async with self.session_factory() as session:
            existing = await session.get(self.orm_cls, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                entity = existing
                source = self._to_orm(item)
                for field in self._fields():
                    setattr(entity, field, getattr(source, field))
            await session.commit()
            await session.refresh(entity)
            return self._to_domain(entity)

    async def get(self, item_id: str, *, include_deleted: bool = False) -> T | None:
        async with self.session_factory() as session:
            item = await session.get(self.orm_cls, item_id)
            if item is None:
                return None
            domain = self._to_domain(item)
            if not include_deleted and getattr(domain, "status", None) == PersonaStatus.ARCHIVED:
                return None
            return domain

    async def list(self, *, include_deleted: bool = False) -> list[T]:
        async with self.session_factory() as session:
            stmt = select(self.orm_cls)
            result = await session.execute(stmt.order_by(self.orm_cls.updated_at.desc(), self.orm_cls.id.desc()))
            items = [self._to_domain(item) for item in result.scalars().all()]
            if include_deleted:
                return items
            return [item for item in items if getattr(item, "status", None) != PersonaStatus.ARCHIVED]

    async def update(self, item_id: str, patch: dict[str, Any]) -> T | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(self.domain_cls.model_validate(merged))

    def _fields(self) -> tuple[str, ...]:
        raise NotImplementedError


class SQLPersonaRepository(_SQLPersonaRepositoryBase[PersonaDefinition]):
    orm_cls = PersonaORM
    domain_cls = PersonaDefinition

    def _fields(self) -> tuple[str, ...]:
        return (
            "slug",
            "name",
            "description",
            "status",
            "created_by_user_id",
            "workspace_id",
            "current_version_id",
            "published_agent_id",
            "published_workflow_id",
            "metadata_json",
        )

    def _to_domain(self, orm: PersonaORM) -> PersonaDefinition:
        return PersonaDefinition.model_validate(
            {
                "id": orm.id,
                "slug": orm.slug,
                "name": orm.name,
                "description": orm.description,
                "status": orm.status,
                "created_by_user_id": orm.created_by_user_id,
                "workspace_id": orm.workspace_id,
                "current_version_id": orm.current_version_id,
                "published_agent_id": orm.published_agent_id,
                "published_workflow_id": orm.published_workflow_id,
                "metadata": orm.metadata_json or {},
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
            }
        )

    def _to_orm(self, item: PersonaDefinition) -> PersonaORM:
        return PersonaORM(
            id=item.id,
            slug=item.slug,
            name=item.name,
            description=item.description,
            status=item.status.value,
            created_by_user_id=item.created_by_user_id,
            workspace_id=item.workspace_id,
            current_version_id=item.current_version_id,
            published_agent_id=item.published_agent_id,
            published_workflow_id=item.published_workflow_id,
            metadata_json=item.metadata,
        )

    async def find_by_slug(self, slug: str) -> PersonaDefinition | None:
        async with self.session_factory() as session:
            result = await session.execute(select(PersonaORM).where(PersonaORM.slug == slug.strip().lower()))
            item = result.scalars().first()
            return self._to_domain(item) if item is not None else None

    async def soft_delete(self, item_id: str) -> bool:
        updated = await self.update(item_id, {"status": PersonaStatus.ARCHIVED.value})
        return updated is not None


class SQLPersonaVersionRepository(_SQLPersonaRepositoryBase[PersonaVersion]):
    orm_cls = PersonaVersionORM
    domain_cls = PersonaVersion

    def _fields(self) -> tuple[str, ...]:
        return (
            "persona_id",
            "version",
            "status",
            "package_json",
            "generated_from_run_id",
            "approved_by_user_id",
            "published_at",
        )

    def _to_domain(self, orm: PersonaVersionORM) -> PersonaVersion:
        return PersonaVersion.model_validate(
            {
                "id": orm.id,
                "persona_id": orm.persona_id,
                "version": orm.version,
                "status": orm.status,
                "package": orm.package_json or {},
                "generated_from_run_id": orm.generated_from_run_id,
                "approved_by_user_id": orm.approved_by_user_id,
                "published_at": orm.published_at,
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
            }
        )

    def _to_orm(self, item: PersonaVersion) -> PersonaVersionORM:
        return PersonaVersionORM(
            id=item.id,
            persona_id=item.persona_id,
            version=item.version,
            status=item.status.value,
            package_json=item.package,
            generated_from_run_id=item.generated_from_run_id,
            approved_by_user_id=item.approved_by_user_id,
            published_at=item.published_at,
        )

    async def list_by_persona(self, persona_id: str) -> list[PersonaVersion]:
        items = [item for item in await self.list(include_deleted=True) if item.persona_id == persona_id]
        return sorted(items, key=lambda item: (item.created_at, item.version), reverse=True)


class SQLPersonaSourceRepository(_SQLPersonaRepositoryBase[PersonaSource]):
    orm_cls = PersonaSourceORM
    domain_cls = PersonaSource

    def _fields(self) -> tuple[str, ...]:
        return (
            "persona_id",
            "source_type",
            "source_id",
            "filename",
            "content_sha256",
            "storage_uri",
            "metadata_json",
        )

    def _to_domain(self, orm: PersonaSourceORM) -> PersonaSource:
        return PersonaSource.model_validate(
            {
                "id": orm.id,
                "persona_id": orm.persona_id,
                "source_type": orm.source_type,
                "source_id": orm.source_id,
                "filename": orm.filename,
                "content_sha256": orm.content_sha256,
                "storage_uri": orm.storage_uri,
                "metadata": orm.metadata_json or {},
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
            }
        )

    def _to_orm(self, item: PersonaSource) -> PersonaSourceORM:
        return PersonaSourceORM(
            id=item.id,
            persona_id=item.persona_id,
            source_type=item.source_type.value,
            source_id=item.source_id,
            filename=item.filename,
            content_sha256=item.content_sha256,
            storage_uri=item.storage_uri,
            metadata_json=item.metadata,
        )

    async def list_by_persona(self, persona_id: str) -> list[PersonaSource]:
        items = [item for item in await self.list(include_deleted=True) if item.persona_id == persona_id]
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)


class SQLPersonaDistillationRunRepository(_SQLPersonaRepositoryBase[PersonaDistillationRun]):
    orm_cls = PersonaDistillationRunORM
    domain_cls = PersonaDistillationRun

    def _fields(self) -> tuple[str, ...]:
        return (
            "persona_id",
            "status",
            "distillation_mode",
            "llm_model_source",
            "model_profile_id",
            "llm_model_provider",
            "llm_model",
            "resolved_model_provider",
            "resolved_model",
            "resolved_model_profile_id",
            "input_source_ids_json",
            "output_package_json",
            "distillation_metrics_json",
            "warnings_json",
            "errors_json",
            "completed_at",
        )

    def _to_domain(self, orm: PersonaDistillationRunORM) -> PersonaDistillationRun:
        return PersonaDistillationRun.model_validate(
            {
                "id": orm.id,
                "persona_id": orm.persona_id,
                "status": orm.status,
                "distillation_mode": orm.distillation_mode or "deterministic",
                "llm_model_source": orm.llm_model_source,
                "model_profile_id": orm.model_profile_id,
                "llm_model_provider": orm.llm_model_provider,
                "llm_model": orm.llm_model,
                "resolved_model_provider": orm.resolved_model_provider,
                "resolved_model": orm.resolved_model,
                "resolved_model_profile_id": orm.resolved_model_profile_id,
                "input_source_ids": orm.input_source_ids_json or [],
                "output_package": orm.output_package_json or {},
                "distillation_metrics": orm.distillation_metrics_json or {},
                "warnings": orm.warnings_json or [],
                "errors": orm.errors_json or [],
                "completed_at": orm.completed_at,
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
            }
        )

    def _to_orm(self, item: PersonaDistillationRun) -> PersonaDistillationRunORM:
        return PersonaDistillationRunORM(
            id=item.id,
            persona_id=item.persona_id,
            status=item.status.value,
            distillation_mode=item.distillation_mode.value,
            llm_model_source=item.llm_model_source.value if item.llm_model_source else None,
            model_profile_id=item.model_profile_id,
            llm_model_provider=item.llm_model_provider,
            llm_model=item.llm_model,
            resolved_model_provider=item.resolved_model_provider,
            resolved_model=item.resolved_model,
            resolved_model_profile_id=item.resolved_model_profile_id,
            input_source_ids_json=item.input_source_ids,
            output_package_json=item.output_package,
            distillation_metrics_json=item.distillation_metrics,
            warnings_json=item.warnings,
            errors_json=item.errors,
            completed_at=item.completed_at,
        )

    async def list_by_persona(self, persona_id: str) -> list[PersonaDistillationRun]:
        items = [item for item in await self.list(include_deleted=True) if item.persona_id == persona_id]
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)


class SQLPersonaDistillationItemRepository(_SQLPersonaRepositoryBase[PersonaDistillationItem]):
    orm_cls = PersonaDistillationItemORM
    domain_cls = PersonaDistillationItem

    def _fields(self) -> tuple[str, ...]:
        return (
            "run_id",
            "persona_id",
            "source_memory_id",
            "item_type",
            "memory_layer",
            "title",
            "content",
            "structured_payload_json",
            "confidence",
            "needs_review",
            "review_status",
            "metadata_json",
        )

    def _to_domain(self, orm: PersonaDistillationItemORM) -> PersonaDistillationItem:
        return PersonaDistillationItem.model_validate(
            {
                "id": orm.id,
                "run_id": orm.run_id,
                "persona_id": orm.persona_id,
                "source_memory_id": orm.source_memory_id,
                "item_type": orm.item_type,
                "memory_layer": orm.memory_layer,
                "title": orm.title,
                "content": orm.content,
                "structured_payload": orm.structured_payload_json or {},
                "confidence": orm.confidence,
                "needs_review": orm.needs_review,
                "review_status": orm.review_status,
                "metadata": orm.metadata_json or {},
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
            }
        )

    def _to_orm(self, item: PersonaDistillationItem) -> PersonaDistillationItemORM:
        return PersonaDistillationItemORM(
            id=item.id,
            run_id=item.run_id,
            persona_id=item.persona_id,
            source_memory_id=item.source_memory_id,
            item_type=item.item_type.value,
            memory_layer=item.memory_layer.value,
            title=item.title,
            content=item.content,
            structured_payload_json=item.structured_payload,
            confidence=item.confidence,
            needs_review=item.needs_review,
            review_status=item.review_status.value,
            metadata_json=item.metadata,
        )

    async def list_by_run(self, run_id: str) -> list[PersonaDistillationItem]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(PersonaDistillationItemORM)
                .where(PersonaDistillationItemORM.run_id == run_id)
                .order_by(PersonaDistillationItemORM.created_at.asc(), PersonaDistillationItemORM.id.asc())
            )
            return [self._to_domain(item) for item in result.scalars().all()]

    async def list_by_persona(self, persona_id: str) -> list[PersonaDistillationItem]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(PersonaDistillationItemORM)
                .where(PersonaDistillationItemORM.persona_id == persona_id)
                .order_by(PersonaDistillationItemORM.created_at.desc(), PersonaDistillationItemORM.id.desc())
            )
            return [self._to_domain(item) for item in result.scalars().all()]

    async def list_by_source_memory(self, source_memory_id: str) -> list[PersonaDistillationItem]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(PersonaDistillationItemORM)
                .where(PersonaDistillationItemORM.source_memory_id == source_memory_id)
                .order_by(PersonaDistillationItemORM.created_at.desc(), PersonaDistillationItemORM.id.desc())
            )
            return [self._to_domain(item) for item in result.scalars().all()]


__all__ = [
    "InMemoryPersonaDistillationItemRepository",
    "InMemoryPersonaDistillationRunRepository",
    "InMemoryPersonaRepository",
    "InMemoryPersonaSourceRepository",
    "InMemoryPersonaVersionRepository",
    "SQLPersonaDistillationItemRepository",
    "SQLPersonaDistillationRunRepository",
    "SQLPersonaRepository",
    "SQLPersonaSourceRepository",
    "SQLPersonaVersionRepository",
]
