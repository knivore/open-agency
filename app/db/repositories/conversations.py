from __future__ import annotations

from sqlalchemy import select

from app.db.models import ConversationMessageORM, ConversationORM
from app.domain import ApprovalRequest, Conversation, ConversationMessage
from .catalog import InMemoryCatalogRepository
from .sql import SQLAlchemyRepository


class ConversationRepository(SQLAlchemyRepository[ConversationORM]):
    def __init__(self, session):
        super().__init__(session, ConversationORM)


class ConversationMessageRepository(SQLAlchemyRepository[ConversationMessageORM]):
    def __init__(self, session):
        super().__init__(session, ConversationMessageORM)

    async def list_by_conversation(self, conversation_id: str) -> list[ConversationMessageORM]:
        result = await self.session.execute(
            select(ConversationMessageORM)
            .where(ConversationMessageORM.conversation_id == conversation_id)
            .order_by(ConversationMessageORM.created_at.asc(), ConversationMessageORM.id.asc())
        )
        return list(result.scalars().all())

    async def find_by_external_message_id(
            self,
            conversation_id: str,
            external_message_id: str,
    ) -> ConversationMessageORM | None:
        result = await self.session.execute(
            select(ConversationMessageORM).where(
                ConversationMessageORM.conversation_id == conversation_id,
                ConversationMessageORM.external_message_id == external_message_id,
            )
        )
        return result.scalar_one_or_none()


class InMemoryConversationRepository(InMemoryCatalogRepository[Conversation]):
    def __init__(self):
        super().__init__(Conversation)

    async def list(self, *, include_deleted: bool = False) -> list[Conversation]:
        items = await super().list(include_deleted=include_deleted)
        return sorted(items, key=lambda item: (item.updated_at, item.id), reverse=True)


class InMemoryConversationMessageRepository(InMemoryCatalogRepository[ConversationMessage]):
    def __init__(self):
        super().__init__(ConversationMessage)

    async def list(self, *, include_deleted: bool = False) -> list[ConversationMessage]:
        items = await super().list(include_deleted=include_deleted)
        return sorted(items, key=lambda item: (item.created_at, item.id))

    async def list_by_conversation(self, conversation_id: str) -> list[ConversationMessage]:
        items = await self.list()
        return [item for item in items if item.conversation_id == conversation_id]

    async def list_recent_by_conversation(
            self,
            conversation_id: str,
            *,
            limit: int,
    ) -> list[ConversationMessage]:
        items = await self.list_by_conversation(conversation_id)
        return items[-limit:]

    async def find_by_external_message_id(
            self,
            conversation_id: str,
            external_message_id: str,
    ) -> ConversationMessage | None:
        for item in await self.list_by_conversation(conversation_id):
            if item.external_message_id == external_message_id:
                return item
        return None


class InMemoryConversationApprovalRequestRepository(InMemoryCatalogRepository[ApprovalRequest]):
    def __init__(self):
        super().__init__(ApprovalRequest)

    async def list(self, *, include_deleted: bool = False) -> list[ApprovalRequest]:
        items = await super().list(include_deleted=include_deleted)
        return sorted(items, key=lambda item: (item.created_at, item.id))

    async def list_by_conversation(self, conversation_id: str) -> list[ApprovalRequest]:
        items = await self.list()
        return [item for item in items if item.conversation_id == conversation_id]
