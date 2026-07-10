from __future__ import annotations

from sqlalchemy import select

from app.db.models import UploadedDocumentORM
from app.domain import UploadedDocument
from .catalog import InMemoryCatalogRepository


class InMemoryUploadedDocumentRepository(InMemoryCatalogRepository[UploadedDocument]):
    def __init__(self):
        super().__init__(UploadedDocument)

    async def list(self, *, include_deleted: bool = False) -> list[UploadedDocument]:
        items = await super().list(include_deleted=include_deleted)
        if not include_deleted:
            items = [item for item in items if item.status.value != "deleted"]
        return sorted(items, key=lambda item: (item.updated_at, item.id), reverse=True)

    async def get(self, item_id: str, *, include_deleted: bool = False) -> UploadedDocument | None:
        item = await super().get(item_id, include_deleted=include_deleted)
        if item is None:
            return None
        if not include_deleted and item.status.value == "deleted":
            return None
        return item

    async def list_by_conversation(self, conversation_id: str) -> list[UploadedDocument]:
        return [item for item in await self.list() if item.conversation_id == conversation_id]

    async def query(
            self,
            *,
            conversation_id: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            user_id: str | None = None,
            scope: str | None = None,
            upload_mode: str | None = None,
            limit: int = 50,
    ) -> list[UploadedDocument]:
        items = []
        for item in await self.list():
            if conversation_id is not None and item.conversation_id != conversation_id:
                continue
            if workflow_id is not None and item.workflow_id != workflow_id:
                continue
            if agent_id is not None and item.agent_id != agent_id:
                continue
            if user_id is not None and item.created_by_user_id != user_id:
                continue
            if scope is not None and item.scope != scope:
                continue
            if upload_mode is not None and item.upload_mode.value != upload_mode:
                continue
            items.append(item)
            if len(items) >= max(limit, 0):
                break
        return items


class SQLUploadedDocumentRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def _to_domain(self, orm: UploadedDocumentORM) -> UploadedDocument:
        return UploadedDocument.model_validate(
            {
                "id": orm.id,
                "filename": orm.filename,
                "content_type": orm.content_type,
                "storage_uri": orm.storage_uri,
                "extracted_text": orm.extracted_text,
                "content_sha256": orm.content_sha256,
                "text_characters": orm.text_characters,
                "estimated_tokens": orm.estimated_tokens,
                "upload_mode": orm.upload_mode,
                "scope": orm.scope,
                "created_by_user_id": orm.created_by_user_id,
                "workspace_id": orm.workspace_id,
                "conversation_id": orm.conversation_id,
                "workflow_id": orm.workflow_id,
                "agent_id": orm.agent_id,
                "status": orm.status,
                "metadata": orm.metadata_json or {},
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
            }
        )

    def _to_orm(self, item: UploadedDocument) -> UploadedDocumentORM:
        return UploadedDocumentORM(
            id=item.id,
            filename=item.filename,
            content_type=item.content_type,
            storage_uri=item.storage_uri,
            extracted_text=item.extracted_text,
            content_sha256=item.content_sha256,
            text_characters=item.text_characters,
            estimated_tokens=item.estimated_tokens,
            upload_mode=item.upload_mode.value,
            scope=item.scope,
            created_by_user_id=item.created_by_user_id,
            workspace_id=item.workspace_id,
            conversation_id=item.conversation_id,
            workflow_id=item.workflow_id,
            agent_id=item.agent_id,
            status=item.status.value,
            metadata_json=item.metadata,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    async def create(self, item: UploadedDocument) -> UploadedDocument:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
            return self._to_domain(entity)

    async def save(self, item: UploadedDocument) -> UploadedDocument:
        async with self.session_factory() as session:
            existing = await session.get(UploadedDocumentORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "filename",
                        "content_type",
                        "storage_uri",
                        "extracted_text",
                        "content_sha256",
                        "text_characters",
                        "estimated_tokens",
                        "upload_mode",
                        "scope",
                        "created_by_user_id",
                        "workspace_id",
                        "conversation_id",
                        "workflow_id",
                        "agent_id",
                        "status",
                        "metadata_json",
                        "updated_at",
                ):
                    setattr(entity, field, getattr(source, field))
            await session.commit()
            await session.refresh(entity)
            return self._to_domain(entity)

    async def get(self, item_id: str, *, include_deleted: bool = False) -> UploadedDocument | None:
        async with self.session_factory() as session:
            item = await session.get(UploadedDocumentORM, item_id)
            if item is None:
                return None
            document = self._to_domain(item)
            if not include_deleted and document.status.value == "deleted":
                return None
            return document

    async def list(self, *, include_deleted: bool = False) -> list[UploadedDocument]:
        async with self.session_factory() as session:
            stmt = select(UploadedDocumentORM).order_by(
                UploadedDocumentORM.updated_at.desc(),
                UploadedDocumentORM.id.desc(),
            )
            if not include_deleted:
                stmt = stmt.where(UploadedDocumentORM.status != "deleted")
            result = await session.execute(stmt)
            return [self._to_domain(item) for item in result.scalars().all()]

    async def list_by_conversation(self, conversation_id: str) -> list[UploadedDocument]:
        return await self.query(conversation_id=conversation_id)

    async def query(
            self,
            *,
            conversation_id: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            user_id: str | None = None,
            scope: str | None = None,
            upload_mode: str | None = None,
            limit: int = 50,
    ) -> list[UploadedDocument]:
        async with self.session_factory() as session:
            stmt = select(UploadedDocumentORM).where(UploadedDocumentORM.status != "deleted")
            if conversation_id is not None:
                stmt = stmt.where(UploadedDocumentORM.conversation_id == conversation_id)
            if workflow_id is not None:
                stmt = stmt.where(UploadedDocumentORM.workflow_id == workflow_id)
            if agent_id is not None:
                stmt = stmt.where(UploadedDocumentORM.agent_id == agent_id)
            if user_id is not None:
                stmt = stmt.where(UploadedDocumentORM.created_by_user_id == user_id)
            if scope is not None:
                stmt = stmt.where(UploadedDocumentORM.scope == scope)
            if upload_mode is not None:
                stmt = stmt.where(UploadedDocumentORM.upload_mode == upload_mode)
            stmt = stmt.order_by(UploadedDocumentORM.updated_at.desc(), UploadedDocumentORM.id.desc()).limit(limit)
            result = await session.execute(stmt)
            return [self._to_domain(item) for item in result.scalars().all()]
