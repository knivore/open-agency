from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from typing import Any, Protocol, TypeVar

from app.core.time import ensure_utc, utc_now
from app.db.models import (
    AgentORM,
    ChannelIdentityMappingORM,
    ConversationApprovalRequestORM,
    ConversationMessageORM,
    ConversationORM,
    CredentialORM,
    MainAgentProfileORM,
    MCPServerORM,
    ModelProfileORM,
    ModelProviderORM,
    RuntimeRevisionORM,
    RuntimeAdapterORM,
    ScheduleFireClaimORM,
    ScheduleORM,
    ToolORM,
    WorkflowORM,
    WorkflowVersionORM,
)
from app.domain import (
    AgentDefinition,
    ApprovalRequest,
    ChannelIdentityMapping,
    Conversation,
    ConversationMessage,
    CredentialDefinition,
    MainAgentProfile,
    MCPServerDefinition,
    ModelProfileDefinition,
    ModelProviderDefinition,
    RuntimeRevision,
    RuntimeAdapterDefinition,
    ScheduleDefinition,
    ToolDefinition,
    WorkflowDefinition,
)

T = TypeVar("T")


class DomainRepository(Protocol):
    async def create(self, item: T) -> T: ...

    async def save(self, item: T) -> T: ...

    async def list(self, *, include_deleted: bool = False) -> list[T]: ...

    async def get(self, item_id: str, *, include_deleted: bool = False) -> T | None: ...

    async def update(self, item_id: str, patch: dict[str, Any]) -> T | None: ...

    async def soft_delete(self, item_id: str) -> bool: ...


class SQLDomainRepositoryBase:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def _commit_and_refresh(self, session: AsyncSession, entity):
        await session.commit()
        await session.refresh(entity)
        return entity


class SQLAgentRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: AgentORM) -> AgentDefinition:
        return AgentDefinition.model_validate(
            {
                "id": orm.id,
                "name": orm.name,
                "display_name": orm.display_name,
                "description": orm.description,
                "instructions": orm.instructions,
                "role": orm.role,
                "backstory": orm.backstory,
                "model_profile_id": orm.model_profile_id,
                "tool_ids": orm.tool_ids_json,
                "handoff_agent_ids": orm.handoff_agent_ids_json,
                "guardrails": orm.guardrails_json,
                "memory": orm.memory_json,
                "framework_hints": orm.framework_hints_json,
                "metadata": {"enabled": orm.enabled},
            }
        )

    def _to_orm(self, item: AgentDefinition) -> AgentORM:
        return AgentORM(
            id=item.id,
            name=item.name,
            display_name=item.display_name,
            description=item.description,
            instructions=item.instructions,
            role=item.role,
            backstory=item.backstory,
            model_profile_id=item.model_profile_id,
            tool_ids_json=item.tool_ids,
            handoff_agent_ids_json=item.handoff_agent_ids,
            guardrails_json=[guardrail.model_dump(mode="json") for guardrail in item.guardrails],
            memory_json=item.memory.model_dump(mode="json"),
            framework_hints_json=item.framework_hints.model_dump(mode="json"),
            enabled=item.metadata.get("enabled", True),
        )

    async def create(self, item: AgentDefinition) -> AgentDefinition:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: AgentDefinition) -> AgentDefinition:
        async with self.session_factory() as session:
            existing = await session.get(AgentORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                entity = existing
                source = self._to_orm(item)
                for field in (
                        "name",
                        "display_name",
                        "description",
                        "instructions",
                        "role",
                        "backstory",
                        "model_profile_id",
                        "tool_ids_json",
                        "handoff_agent_ids_json",
                        "guardrails_json",
                        "memory_json",
                        "framework_hints_json",
                        "enabled",
                ):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[AgentDefinition]:
        async with self.session_factory() as session:
            stmt = select(AgentORM)
            if not include_deleted:
                stmt = stmt.where(AgentORM.enabled.is_(True))
            result = await session.execute(stmt.order_by(AgentORM.name.asc()))
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *, include_deleted: bool = False) -> AgentDefinition | None:
        async with self.session_factory() as session:
            item = await session.get(AgentORM, item_id)
            if item is None:
                return None
            if not include_deleted and not item.enabled:
                return None
            return self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> AgentDefinition | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        metadata = dict(current.metadata)
        if "enabled" in patch:
            metadata["enabled"] = patch["enabled"]
        merged.update({k: v for k, v in patch.items() if k != "enabled"})
        merged["metadata"] = metadata
        return await self.save(AgentDefinition.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        updated = await self.update(item_id, {"enabled": False})
        return updated is not None


class SQLToolRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: ToolORM) -> ToolDefinition:
        return ToolDefinition.model_validate(
            {
                "id": orm.id,
                "name": orm.name,
                "description": orm.description,
                "tool_type": orm.tool_type,
                "input_schema": orm.input_schema_json,
                "output_schema": orm.output_schema_json,
                "implementation": orm.implementation_json,
                "security": orm.security_json,
                "mcp_exposure": orm.mcp_json,
                "framework_hints": {},
            }
        )

    def _to_orm(self, item: ToolDefinition) -> ToolORM:
        return ToolORM(
            id=item.id,
            name=item.name,
            description=item.description,
            tool_type=item.tool_type.value,
            input_schema_json=item.input_schema,
            output_schema_json=item.output_schema,
            implementation_json=item.implementation.model_dump(mode="json"),
            security_json=item.security.model_dump(mode="json"),
            mcp_json=item.mcp_exposure.model_dump(mode="json"),
            enabled=True,
        )

    async def create(self, item: ToolDefinition) -> ToolDefinition:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: ToolDefinition) -> ToolDefinition:
        async with self.session_factory() as session:
            existing = await session.get(ToolORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "name",
                        "description",
                        "tool_type",
                        "input_schema_json",
                        "output_schema_json",
                        "implementation_json",
                        "security_json",
                        "mcp_json",
                ):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[ToolDefinition]:
        async with self.session_factory() as session:
            stmt = select(ToolORM)
            if not include_deleted:
                stmt = stmt.where(ToolORM.enabled.is_(True))
            result = await session.execute(stmt.order_by(ToolORM.name.asc()))
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *, include_deleted: bool = False) -> ToolDefinition | None:
        async with self.session_factory() as session:
            item = await session.get(ToolORM, item_id)
            if item is None:
                return None
            if not include_deleted and not item.enabled:
                return None
            return self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> ToolDefinition | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(ToolDefinition.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        async with self.session_factory() as session:
            item = await session.get(ToolORM, item_id)
            if item is None:
                return False
            item.enabled = False
            await session.commit()
            return True


class SQLConversationRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: ConversationORM) -> Conversation:
        return Conversation.model_validate(
            {
                "id": orm.id,
                "title": orm.title,
                "status": orm.status,
                "created_by_user_id": orm.created_by_user_id,
                "main_agent_profile_id": orm.main_agent_profile_id,
                "channel_type": orm.channel_type,
                "channel_thread_id": orm.channel_thread_id,
                "channel_user_id": orm.channel_user_id,
                "channel_display_name": orm.channel_display_name,
                "workspace_id": orm.workspace_id,
                "metadata": orm.metadata_json or {},
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
            }
        )

    def _to_orm(self, item: Conversation) -> ConversationORM:
        return ConversationORM(
            id=item.id,
            title=item.title,
            status=item.status.value,
            created_by_user_id=item.created_by_user_id,
            main_agent_profile_id=item.main_agent_profile_id,
            channel_type=item.channel_type.value,
            channel_thread_id=item.channel_thread_id,
            channel_user_id=item.channel_user_id,
            channel_display_name=item.channel_display_name,
            workspace_id=item.workspace_id,
            metadata_json=item.metadata,
        )

    async def create(self, item: Conversation) -> Conversation:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: Conversation) -> Conversation:
        async with self.session_factory() as session:
            existing = await session.get(ConversationORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "title",
                        "status",
                        "created_by_user_id",
                        "main_agent_profile_id",
                        "channel_type",
                        "channel_thread_id",
                        "channel_user_id",
                        "channel_display_name",
                        "workspace_id",
                        "metadata_json",
                ):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[Conversation]:
        async with self.session_factory() as session:
            stmt = select(ConversationORM).order_by(ConversationORM.updated_at.desc(), ConversationORM.id.desc())
            result = await session.execute(stmt)
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *, include_deleted: bool = False) -> Conversation | None:
        async with self.session_factory() as session:
            item = await session.get(ConversationORM, item_id)
            if item is None:
                return None
            return self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> Conversation | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(Conversation.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        deleted = await self.update(item_id, {"status": "archived"})
        return deleted is not None


class SQLChannelIdentityMappingRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: ChannelIdentityMappingORM) -> ChannelIdentityMapping:
        return ChannelIdentityMapping.model_validate(
            {
                "id": orm.id,
                "channel_type": orm.channel_type,
                "channel_user_id": orm.channel_user_id,
                "internal_user_id": orm.internal_user_id,
                "channel_display_name": orm.channel_display_name,
                "trusted": orm.trusted,
                "metadata": orm.metadata_json or {},
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
            }
        )

    def _to_orm(self, item: ChannelIdentityMapping) -> ChannelIdentityMappingORM:
        return ChannelIdentityMappingORM(
            id=item.id,
            channel_type=item.channel_type.value,
            channel_user_id=item.channel_user_id,
            internal_user_id=item.internal_user_id,
            channel_display_name=item.channel_display_name,
            trusted=item.trusted,
            metadata_json=item.metadata,
        )

    async def create(self, item: ChannelIdentityMapping) -> ChannelIdentityMapping:
        return await self.save(item)

    async def save(self, item: ChannelIdentityMapping) -> ChannelIdentityMapping:
        async with self.session_factory() as session:
            existing = await session.get(ChannelIdentityMappingORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "channel_type",
                        "channel_user_id",
                        "internal_user_id",
                        "channel_display_name",
                        "trusted",
                        "metadata_json",
                ):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[ChannelIdentityMapping]:  # noqa: ARG002
        async with self.session_factory() as session:
            result = await session.execute(select(ChannelIdentityMappingORM))
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *,
                  include_deleted: bool = False) -> ChannelIdentityMapping | None:  # noqa: ARG002
        async with self.session_factory() as session:
            item = await session.get(ChannelIdentityMappingORM, item_id)
            return self._to_domain(item) if item is not None else None

    async def find_by_channel_identity(self, channel_type: str, channel_user_id: str) -> ChannelIdentityMapping | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ChannelIdentityMappingORM).where(
                    ChannelIdentityMappingORM.channel_type == channel_type,
                    ChannelIdentityMappingORM.channel_user_id == channel_user_id,
                )
            )
            item = result.scalar_one_or_none()
            return self._to_domain(item) if item is not None else None

    async def update(self, item_id: str, patch: dict[str, Any]) -> ChannelIdentityMapping | None:
        current = await self.get(item_id)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(ChannelIdentityMapping.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(
                delete(ChannelIdentityMappingORM).where(ChannelIdentityMappingORM.id == item_id))
            await session.commit()
            return result.rowcount > 0


class SQLConversationMessageRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: ConversationMessageORM) -> ConversationMessage:
        return ConversationMessage.model_validate(
            {
                "id": orm.id,
                "conversation_id": orm.conversation_id,
                "role": orm.role,
                "message_type": orm.message_type,
                "content": orm.content_json or {},
                "plain_text": orm.plain_text,
                "external_message_id": orm.external_message_id,
                "execution_id": orm.execution_id,
                "approval_request_id": orm.approval_request_id,
                "tool_call_id": orm.tool_call_id,
                "metadata": orm.metadata_json or {},
                "created_at": orm.created_at,
            }
        )

    def _to_orm(self, item: ConversationMessage) -> ConversationMessageORM:
        return ConversationMessageORM(
            id=item.id,
            conversation_id=item.conversation_id,
            role=item.role.value,
            message_type=item.message_type.value,
            content_json=item.content,
            plain_text=item.plain_text,
            external_message_id=item.external_message_id,
            execution_id=item.execution_id,
            approval_request_id=item.approval_request_id,
            tool_call_id=item.tool_call_id,
            metadata_json=item.metadata,
            created_at=item.created_at,
        )

    async def create(self, item: ConversationMessage) -> ConversationMessage:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            await session.execute(
                select(ConversationORM).where(ConversationORM.id == item.conversation_id).with_for_update()
            )
            conversation = await session.get(ConversationORM, item.conversation_id)
            if conversation is not None:
                conversation.updated_at = item.created_at
                await session.commit()
                await session.refresh(conversation)
            return self._to_domain(entity)

    async def save(self, item: ConversationMessage) -> ConversationMessage:
        async with self.session_factory() as session:
            existing = await session.get(ConversationMessageORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "conversation_id",
                        "role",
                        "message_type",
                        "content_json",
                        "plain_text",
                        "external_message_id",
                        "execution_id",
                        "approval_request_id",
                        "tool_call_id",
                        "metadata_json",
                        "created_at",
                ):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[ConversationMessage]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ConversationMessageORM).order_by(
                    ConversationMessageORM.created_at.asc(),
                    ConversationMessageORM.id.asc(),
                )
            )
            return [self._to_domain(item) for item in result.scalars().all()]

    async def list_by_conversation(self, conversation_id: str) -> list[ConversationMessage]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ConversationMessageORM)
                .where(ConversationMessageORM.conversation_id == conversation_id)
                .order_by(ConversationMessageORM.created_at.asc(), ConversationMessageORM.id.asc())
            )
            return [self._to_domain(item) for item in result.scalars().all()]

    async def find_by_external_message_id(
            self,
            conversation_id: str,
            external_message_id: str,
    ) -> ConversationMessage | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ConversationMessageORM).where(
                    ConversationMessageORM.conversation_id == conversation_id,
                    ConversationMessageORM.external_message_id == external_message_id,
                )
            )
            item = result.scalar_one_or_none()
            return self._to_domain(item) if item is not None else None

    async def get(self, item_id: str, *, include_deleted: bool = False) -> ConversationMessage | None:
        async with self.session_factory() as session:
            item = await session.get(ConversationMessageORM, item_id)
            if item is None:
                return None
            return self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> ConversationMessage | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(ConversationMessage.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(delete(ConversationMessageORM).where(ConversationMessageORM.id == item_id))
            await session.commit()
            return result.rowcount > 0


class SQLConversationApprovalRequestRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: ConversationApprovalRequestORM) -> ApprovalRequest:
        return ApprovalRequest.model_validate(
            {
                "id": orm.id,
                "approval_type": orm.approval_type,
                "status": orm.status,
                "target_type": orm.target_type,
                "target_id": orm.target_id,
                "requested_by_agent_id": orm.requested_by_agent_id,
                "requested_by_profile_id": orm.requested_by_profile_id,
                "conversation_id": orm.conversation_id,
                "origin_message_id": orm.origin_message_id,
                "summary": orm.summary,
                "diff_summary": orm.diff_summary,
                "proposed_payload": orm.proposed_payload_json,
                "decision_reason": orm.decision_reason,
                "approved_by_user_id": orm.approved_by_user_id,
                "metadata": orm.metadata_json or {},
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
            }
        )

    def _to_orm(self, item: ApprovalRequest) -> ConversationApprovalRequestORM:
        return ConversationApprovalRequestORM(
            id=item.id,
            approval_type=item.approval_type.value,
            status=item.status.value,
            target_type=item.target_type.value,
            target_id=item.target_id,
            requested_by_agent_id=item.requested_by_agent_id,
            requested_by_profile_id=item.requested_by_profile_id,
            conversation_id=item.conversation_id,
            origin_message_id=item.origin_message_id,
            summary=item.summary,
            diff_summary=item.diff_summary,
            proposed_payload_json=item.proposed_payload,
            decision_reason=item.decision_reason,
            approved_by_user_id=item.approved_by_user_id,
            metadata_json=item.metadata,
        )

    async def create(self, item: ApprovalRequest) -> ApprovalRequest:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: ApprovalRequest) -> ApprovalRequest:
        async with self.session_factory() as session:
            existing = await session.get(ConversationApprovalRequestORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "approval_type",
                        "status",
                        "target_type",
                        "target_id",
                        "requested_by_agent_id",
                        "requested_by_profile_id",
                        "conversation_id",
                        "origin_message_id",
                        "summary",
                        "diff_summary",
                        "proposed_payload_json",
                        "decision_reason",
                        "approved_by_user_id",
                        "metadata_json",
                ):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[ApprovalRequest]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ConversationApprovalRequestORM).order_by(
                    ConversationApprovalRequestORM.created_at.asc(),
                    ConversationApprovalRequestORM.id.asc(),
                )
            )
            return [self._to_domain(item) for item in result.scalars().all()]

    async def list_by_conversation(self, conversation_id: str) -> list[ApprovalRequest]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ConversationApprovalRequestORM)
                .where(ConversationApprovalRequestORM.conversation_id == conversation_id)
                .order_by(
                    ConversationApprovalRequestORM.created_at.asc(),
                    ConversationApprovalRequestORM.id.asc(),
                )
            )
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *, include_deleted: bool = False) -> ApprovalRequest | None:
        async with self.session_factory() as session:
            item = await session.get(ConversationApprovalRequestORM, item_id)
            if item is None:
                return None
            return self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> ApprovalRequest | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(ApprovalRequest.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        return False


class SQLMainAgentProfileRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: MainAgentProfileORM) -> MainAgentProfile:
        return MainAgentProfile.model_validate(
            {
                "id": orm.id,
                "name": orm.name,
                "description": orm.description,
                "agent_id": orm.agent_id,
                "default_workflow_id": orm.default_workflow_id,
                "default_model_profile_id": orm.default_model_profile_id,
                "enabled": orm.enabled,
                "policy": orm.policy_json or {},
                "metadata": orm.metadata_json or {},
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
            }
        )

    def _to_orm(self, item: MainAgentProfile) -> MainAgentProfileORM:
        return MainAgentProfileORM(
            id=item.id,
            name=item.name,
            description=item.description,
            agent_id=item.agent_id,
            default_workflow_id=item.default_workflow_id,
            default_model_profile_id=item.default_model_profile_id,
            enabled=item.enabled,
            policy_json=item.policy,
            metadata_json=item.metadata,
        )

    async def create(self, item: MainAgentProfile) -> MainAgentProfile:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: MainAgentProfile) -> MainAgentProfile:
        async with self.session_factory() as session:
            existing = await session.get(MainAgentProfileORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "name",
                        "description",
                        "agent_id",
                        "default_workflow_id",
                        "default_model_profile_id",
                        "enabled",
                        "policy_json",
                        "metadata_json",
                ):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[MainAgentProfile]:
        async with self.session_factory() as session:
            stmt = select(MainAgentProfileORM)
            if not include_deleted:
                stmt = stmt.where(MainAgentProfileORM.enabled.is_(True))
            result = await session.execute(stmt.order_by(MainAgentProfileORM.name.asc()))
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *, include_deleted: bool = False) -> MainAgentProfile | None:
        async with self.session_factory() as session:
            item = await session.get(MainAgentProfileORM, item_id)
            if item is None:
                return None
            if not include_deleted and not item.enabled:
                return None
            return self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> MainAgentProfile | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(MainAgentProfile.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        updated = await self.update(item_id, {"enabled": False})
        return updated is not None


class SQLModelProviderRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: ModelProviderORM) -> ModelProviderDefinition:
        config = dict(orm.config_json or {})
        endpoint = {"base_url": orm.base_url} if orm.base_url else config.pop("endpoint", None)
        return ModelProviderDefinition.model_validate(
            {
                "id": orm.id,
                "name": orm.name,
                "provider_type": orm.provider_type,
                "endpoint": endpoint,
                "config": config,
            }
        )

    def _to_orm(self, item: ModelProviderDefinition) -> ModelProviderORM:
        config = dict(item.config)
        if item.description is not None:
            config["description"] = item.description
        if item.capabilities:
            config["capabilities"] = item.capabilities
        if item.default_headers:
            config["default_headers"] = item.default_headers
        if item.secret_references:
            config["secret_references"] = [ref.model_dump(mode="json") for ref in item.secret_references]
        if item.framework_hints:
            config["framework_hints"] = item.framework_hints.model_dump(mode="json")
        return ModelProviderORM(
            id=item.id,
            name=item.name,
            provider_type=item.provider_type.value,
            base_url=item.endpoint.base_url if item.endpoint else None,
            enabled=True,
            config_json=config,
        )

    async def create(self, item: ModelProviderDefinition) -> ModelProviderDefinition:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: ModelProviderDefinition) -> ModelProviderDefinition:
        async with self.session_factory() as session:
            existing = await session.get(ModelProviderORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in ("name", "provider_type", "base_url", "enabled", "config_json"):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[ModelProviderDefinition]:
        async with self.session_factory() as session:
            stmt = select(ModelProviderORM)
            if not include_deleted:
                stmt = stmt.where(ModelProviderORM.enabled.is_(True))
            result = await session.execute(stmt.order_by(ModelProviderORM.name.asc()))
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *, include_deleted: bool = False) -> ModelProviderDefinition | None:
        async with self.session_factory() as session:
            item = await session.get(ModelProviderORM, item_id)
            if item is None:
                return None
            if not include_deleted and not item.enabled:
                return None
            return self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> ModelProviderDefinition | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(ModelProviderDefinition.model_validate(merged))

    async def update_tokens(
            self,
            provider_id: str,
            access_token: str,
            refresh_token: str | None,
            expires_at: float,
            *,
            auth_profile_id: str | None = None,
            account_id: str | None = None,
            auth_mode: str | None = None,
            client_id: str | None = None,
            redirect_uri: str | None = None,
    ) -> bool:
        """Update OAuth tokens in the provider's config_json."""
        async with self.session_factory() as session:
            orm = await session.get(ModelProviderORM, provider_id)
            if orm is None:
                return False
            config = dict(orm.config_json or {})
            profile_id = auth_profile_id or config.get("default_oauth_profile_id") or "default"
            auth_profiles = dict(config.get("auth_profiles") or {})
            active = dict(auth_profiles.get(profile_id) or {})
            active["access_token"] = access_token
            if refresh_token:
                active["refresh_token"] = refresh_token
            active["expires_at"] = expires_at
            if account_id is not None:
                active["account_id"] = account_id
            if auth_mode is not None:
                active["auth_mode"] = auth_mode
            if client_id is not None:
                active["client_id"] = client_id
            if redirect_uri is not None:
                active["redirect_uri"] = redirect_uri
            auth_profiles[profile_id] = active
            config["auth_profiles"] = auth_profiles
            if "default_oauth_profile_id" not in config:
                config["default_oauth_profile_id"] = profile_id
            if config.get("default_oauth_profile_id") == profile_id:
                config["access_token"] = access_token
                if refresh_token:
                    config["refresh_token"] = refresh_token
                config["expires_at"] = expires_at
                if account_id is not None:
                    config["account_id"] = account_id
                if auth_mode is not None:
                    config["auth_mode"] = auth_mode
                if client_id is not None:
                    config["client_id"] = client_id
                if redirect_uri is not None:
                    config["redirect_uri"] = redirect_uri
            orm.config_json = config
            await session.commit()
            return True

    async def soft_delete(self, item_id: str) -> bool:
        async with self.session_factory() as session:
            item = await session.get(ModelProviderORM, item_id)
            if item is None:
                return False
            item.enabled = False
            await session.commit()
            return True


class SQLModelProfileRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: ModelProfileORM) -> ModelProfileDefinition:
        config = dict(orm.config_json or {})
        return ModelProfileDefinition.model_validate(
            {
                "id": orm.id,
                "provider_id": orm.provider_id,
                "name": orm.name,
                "model": orm.model,
                "temperature": orm.temperature,
                "max_tokens": orm.max_tokens,
                "context_window": orm.context_window,
                "supports_tools": orm.supports_tools,
                "supports_structured_output": orm.supports_structured_output,
                "supports_vision": orm.supports_vision,
                "supports_streaming": orm.supports_streaming,
                "parameters": config.get("parameters", {}),
                "framework_hints": config.get("framework_hints", {}),
                "base_url": config.get("base_url"),
                "api_key_ref": config.get("api_key_ref"),
                "description": config.get("description"),
                "top_p": config.get("top_p"),
            }
        )

    def _to_orm(self, item: ModelProfileDefinition) -> ModelProfileORM:
        return ModelProfileORM(
            id=item.id,
            provider_id=item.provider_id,
            name=item.name,
            model=item.model,
            temperature=item.temperature,
            max_tokens=item.max_tokens,
            context_window=item.context_window,
            supports_tools=item.supports_tools,
            supports_structured_output=item.supports_structured_output,
            supports_vision=item.supports_vision,
            supports_streaming=item.supports_streaming,
            config_json={
                "parameters": item.parameters,
                "framework_hints": item.framework_hints.model_dump(mode="json"),
                "base_url": item.base_url,
                "api_key_ref": item.api_key_ref,
                "description": item.description,
                "top_p": item.top_p,
            },
        )

    async def create(self, item: ModelProfileDefinition) -> ModelProfileDefinition:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: ModelProfileDefinition) -> ModelProfileDefinition:
        async with self.session_factory() as session:
            existing = await session.get(ModelProfileORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "provider_id",
                        "name",
                        "model",
                        "temperature",
                        "max_tokens",
                        "context_window",
                        "supports_tools",
                        "supports_structured_output",
                        "supports_vision",
                        "supports_streaming",
                        "config_json",
                ):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def get(self, item_id: str, *, include_deleted: bool = False) -> ModelProfileDefinition | None:
        async with self.session_factory() as session:
            item = await session.get(ModelProfileORM, item_id)
            return None if item is None else self._to_domain(item)

    async def list(self, *, include_deleted: bool = False) -> list[ModelProfileDefinition]:
        async with self.session_factory() as session:
            result = await session.execute(select(ModelProfileORM).order_by(ModelProfileORM.name.asc()))
            return [self._to_domain(item) for item in result.scalars().all()]

    async def update(self, item_id: str, patch: dict[str, Any]) -> ModelProfileDefinition | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(ModelProfileDefinition.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        async with self.session_factory() as session:
            item = await session.get(ModelProfileORM, item_id)
            if item is None:
                return False
            await session.delete(item)
            await session.commit()
            return True

    async def get_profile(self, profile_id: str) -> ModelProfileDefinition | None:
        return await self.get(profile_id)

    async def save_profile(self, profile: ModelProfileDefinition) -> ModelProfileDefinition:
        return await self.save(profile)


class SQLScheduleRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: ScheduleORM) -> ScheduleDefinition:
        trigger_config = dict(orm.trigger_config_json or {})
        name = trigger_config.pop("__name", orm.id)
        metadata = trigger_config.pop("__metadata", {})
        return ScheduleDefinition.model_validate(
            {
                "id": orm.id,
                "name": name,
                "workflow_id": orm.workflow_id,
                "enabled": orm.enabled,
                "trigger_type": orm.trigger_type,
                "trigger_config": trigger_config,
                "input_template": orm.input_template_json,
                "runtime_adapter_override": orm.runtime_adapter,
                "max_concurrent_executions": orm.max_concurrent_executions,
                "timezone": orm.timezone,
                "next_fire_at": orm.next_fire_at,
                "last_fire_at": orm.last_fire_at,
                "metadata": metadata,
            }
        )

    def _to_orm(self, item: ScheduleDefinition) -> ScheduleORM:
        trigger_config = dict(item.trigger_config)
        trigger_config["__name"] = item.name
        if item.metadata:
            trigger_config["__metadata"] = item.metadata
        return ScheduleORM(
            id=item.id,
            workflow_id=item.workflow_id,
            enabled=item.enabled,
            trigger_type=item.trigger_type.value,
            trigger_config_json=trigger_config,
            input_template_json=item.input_template,
            runtime_adapter=item.runtime_adapter_override,
            max_concurrent_executions=item.max_concurrent_executions,
            timezone=item.timezone,
            next_fire_at=item.next_fire_at,
            last_fire_at=item.last_fire_at,
        )

    async def create(self, item: ScheduleDefinition) -> ScheduleDefinition:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: ScheduleDefinition) -> ScheduleDefinition:
        async with self.session_factory() as session:
            existing = await session.get(ScheduleORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "workflow_id",
                        "enabled",
                        "trigger_type",
                        "trigger_config_json",
                        "input_template_json",
                        "runtime_adapter",
                        "max_concurrent_executions",
                        "timezone",
                        "next_fire_at",
                        "last_fire_at",
                ):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[ScheduleDefinition]:
        async with self.session_factory() as session:
            stmt = select(ScheduleORM)
            result = await session.execute(stmt.order_by(ScheduleORM.id.asc()))
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *, include_deleted: bool = False) -> ScheduleDefinition | None:
        async with self.session_factory() as session:
            item = await session.get(ScheduleORM, item_id)
            return None if item is None else self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> ScheduleDefinition | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(ScheduleDefinition.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        async with self.session_factory() as session:
            item = await session.get(ScheduleORM, item_id)
            if item is None:
                return False
            await session.delete(item)
            await session.commit()
            return True

    async def acquire_schedule_fire_claim(
            self,
            *,
            schedule_id: str,
            scheduled_fire_at: datetime,
            claimed_by: str,
            lease_seconds: int,
    ) -> bool:
        scheduled_fire_at = ensure_utc(scheduled_fire_at)
        now = utc_now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        async with self.session_factory() as session:
            claim = ScheduleFireClaimORM(
                id=str(uuid4()),
                schedule_id=schedule_id,
                scheduled_fire_at=scheduled_fire_at,
                claimed_by=claimed_by,
                lease_expires_at=lease_expires_at,
                status="claimed",
                execution_id=None,
            )
            session.add(claim)
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()

            result = await session.execute(
                update(ScheduleFireClaimORM)
                .where(ScheduleFireClaimORM.schedule_id == schedule_id)
                .where(ScheduleFireClaimORM.scheduled_fire_at == scheduled_fire_at)
                .where(ScheduleFireClaimORM.status.in_(("claimed", "failed")))
                .where(ScheduleFireClaimORM.lease_expires_at <= now)
                .values(
                    claimed_by=claimed_by,
                    lease_expires_at=lease_expires_at,
                    status="claimed",
                    execution_id=None,
                    updated_at=now,
                )
            )
            await session.commit()
            return result.rowcount == 1

    async def mark_schedule_fire_claim_fired(
            self,
            *,
            schedule_id: str,
            scheduled_fire_at: datetime,
            execution_id: str,
            claimed_by: str,
    ) -> None:
        now = utc_now()
        async with self.session_factory() as session:
            await session.execute(
                update(ScheduleFireClaimORM)
                .where(ScheduleFireClaimORM.schedule_id == schedule_id)
                .where(ScheduleFireClaimORM.scheduled_fire_at == ensure_utc(scheduled_fire_at))
                .where(ScheduleFireClaimORM.claimed_by == claimed_by)
                .where(ScheduleFireClaimORM.status == "claimed")
                .values(
                    status="fired",
                    execution_id=execution_id,
                    lease_expires_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

    async def mark_schedule_fire_claim_failed(
            self,
            *,
            schedule_id: str,
            scheduled_fire_at: datetime,
            claimed_by: str,
    ) -> None:
        now = utc_now()
        async with self.session_factory() as session:
            await session.execute(
                update(ScheduleFireClaimORM)
                .where(ScheduleFireClaimORM.schedule_id == schedule_id)
                .where(ScheduleFireClaimORM.scheduled_fire_at == ensure_utc(scheduled_fire_at))
                .where(ScheduleFireClaimORM.claimed_by == claimed_by)
                .where(ScheduleFireClaimORM.status == "claimed")
                .values(
                    status="failed",
                    lease_expires_at=now,
                    updated_at=now,
                )
            )
            await session.commit()


class SQLRuntimeAdapterRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: RuntimeAdapterORM) -> RuntimeAdapterDefinition:
        config = dict(orm.config_json or {})
        return RuntimeAdapterDefinition.model_validate(
            {
                "id": orm.id,
                "name": orm.name,
                "adapter_type": orm.adapter_type,
                "description": config.get("description"),
                "version": config.get("version"),
                "capabilities": config.get("capabilities", []),
                "config_schema": config.get("config_schema", {}),
                "framework_hints": config.get("framework_hints", {}),
            }
        )

    def _to_orm(self, item: RuntimeAdapterDefinition) -> RuntimeAdapterORM:
        return RuntimeAdapterORM(
            id=item.id,
            name=item.name,
            adapter_type=item.adapter_type.value,
            enabled=True,
            available=True,
            unavailable_reason=None,
            config_json={
                "description": item.description,
                "version": item.version,
                "capabilities": item.capabilities,
                "config_schema": item.config_schema,
                "framework_hints": item.framework_hints.model_dump(mode="json"),
            },
        )

    async def create(self, item: RuntimeAdapterDefinition) -> RuntimeAdapterDefinition:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: RuntimeAdapterDefinition) -> RuntimeAdapterDefinition:
        async with self.session_factory() as session:
            existing = await session.get(RuntimeAdapterORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in ("name", "adapter_type", "enabled", "available", "unavailable_reason", "config_json"):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[RuntimeAdapterDefinition]:
        async with self.session_factory() as session:
            stmt = select(RuntimeAdapterORM)
            if not include_deleted:
                stmt = stmt.where(RuntimeAdapterORM.enabled.is_(True))
            result = await session.execute(stmt.order_by(RuntimeAdapterORM.name.asc()))
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *, include_deleted: bool = False) -> RuntimeAdapterDefinition | None:
        async with self.session_factory() as session:
            item = await session.get(RuntimeAdapterORM, item_id)
            if item is None:
                return None
            if not include_deleted and not item.enabled:
                return None
            return self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> RuntimeAdapterDefinition | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(RuntimeAdapterDefinition.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        async with self.session_factory() as session:
            item = await session.get(RuntimeAdapterORM, item_id)
            if item is None:
                return False
            item.enabled = False
            await session.commit()
            return True


class SQLCredentialRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: CredentialORM) -> CredentialDefinition:
        return CredentialDefinition.model_validate(
            {
                "id": orm.id,
                "owner_user_id": orm.owner_user_id,
                "name": orm.name,
                "provider": orm.provider,
                "secret_ref": orm.secret_ref,
                "status": orm.status,
                "last_rotated_at": orm.last_rotated_at,
                "expires_at": orm.expires_at,
                "revoked_at": orm.revoked_at,
                "secret_version": orm.secret_version,
                "rotation_policy": orm.rotation_policy_json or {},
                "metadata": orm.metadata_json or {},
            }
        )

    def _to_orm(self, item: CredentialDefinition) -> CredentialORM:
        return CredentialORM(
            id=item.id,
            owner_user_id=item.owner_user_id,
            name=item.name,
            provider=item.provider,
            secret_ref=item.secret_ref,
            status=item.status.value if hasattr(item.status, "value") else str(item.status),
            last_rotated_at=item.last_rotated_at,
            expires_at=item.expires_at,
            revoked_at=item.revoked_at,
            secret_version=item.secret_version,
            rotation_policy_json=item.rotation_policy,
            metadata_json=item.metadata,
        )

    async def create(self, item: CredentialDefinition) -> CredentialDefinition:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: CredentialDefinition) -> CredentialDefinition:
        async with self.session_factory() as session:
            existing = await session.get(CredentialORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "owner_user_id",
                        "name",
                        "provider",
                        "secret_ref",
                        "status",
                        "last_rotated_at",
                        "expires_at",
                        "revoked_at",
                        "secret_version",
                        "rotation_policy_json",
                        "metadata_json",
                ):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[CredentialDefinition]:
        async with self.session_factory() as session:
            result = await session.execute(select(CredentialORM).order_by(CredentialORM.name.asc()))
            return [self._to_domain(item) for item in result.scalars().all()]

    async def list_by_owner(self, owner_user_id: str) -> list[CredentialDefinition]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(CredentialORM)
                .where(CredentialORM.owner_user_id == owner_user_id)
                .order_by(CredentialORM.name.asc())
            )
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *, include_deleted: bool = False) -> CredentialDefinition | None:
        async with self.session_factory() as session:
            item = await session.get(CredentialORM, item_id)
            return None if item is None else self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> CredentialDefinition | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(CredentialDefinition.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        async with self.session_factory() as session:
            item = await session.get(CredentialORM, item_id)
            if item is None:
                return False
            await session.delete(item)
            await session.commit()
            return True


class SQLRuntimeRevisionRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: RuntimeRevisionORM) -> RuntimeRevision:
        return RuntimeRevision.model_validate(
            {
                "id": orm.id,
                "fingerprint": orm.fingerprint,
                "source_path": orm.source_path,
                "build_status": orm.build_status,
                "image_name": orm.image_name,
                "image_tag": orm.image_tag,
                "base_image": orm.base_image,
                "build_log_ref": orm.build_log_ref,
                "created_at": orm.created_at,
                "updated_at": orm.updated_at,
                "ready_at": orm.ready_at,
                "invalidated_at": orm.invalidated_at,
                "invalidation_reason": orm.invalidation_reason,
                "metadata_json": orm.metadata_json or {},
            }
        )

    def _to_orm(self, item: RuntimeRevision) -> RuntimeRevisionORM:
        return RuntimeRevisionORM(
            id=item.id,
            fingerprint=item.fingerprint,
            source_path=item.source_path,
            build_status=item.build_status.value,
            image_name=item.image_name,
            image_tag=item.image_tag,
            base_image=item.base_image,
            build_log_ref=item.build_log_ref,
            created_at=item.created_at,
            updated_at=item.updated_at,
            ready_at=item.ready_at,
            invalidated_at=item.invalidated_at,
            invalidation_reason=item.invalidation_reason,
            metadata_json=item.metadata,
        )

    async def create(self, item: RuntimeRevision) -> RuntimeRevision:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: RuntimeRevision) -> RuntimeRevision:
        async with self.session_factory() as session:
            existing = await session.get(RuntimeRevisionORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in (
                        "fingerprint",
                        "source_path",
                        "build_status",
                        "image_name",
                        "image_tag",
                        "base_image",
                        "build_log_ref",
                        "created_at",
                        "updated_at",
                        "ready_at",
                        "invalidated_at",
                        "invalidation_reason",
                        "metadata_json",
                ):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[RuntimeRevision]:
        async with self.session_factory() as session:
            stmt = select(RuntimeRevisionORM).order_by(RuntimeRevisionORM.created_at.desc())
            if not include_deleted:
                stmt = stmt.where(RuntimeRevisionORM.build_status != "invalidated")
            result = await session.execute(stmt)
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *, include_deleted: bool = False) -> RuntimeRevision | None:
        async with self.session_factory() as session:
            item = await session.get(RuntimeRevisionORM, item_id)
            if item is None:
                return None
            if not include_deleted and item.build_status == "invalidated":
                return None
            return self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> RuntimeRevision | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(RuntimeRevision.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        updated = await self.invalidate_revision(item_id, reason="soft_delete")
        return updated is not None

    async def get_by_fingerprint(self, fingerprint: str) -> RuntimeRevision | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(RuntimeRevisionORM).where(RuntimeRevisionORM.fingerprint == fingerprint)
            )
            item = result.scalar_one_or_none()
            return None if item is None else self._to_domain(item)

    async def get_latest_ready(self) -> RuntimeRevision | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(RuntimeRevisionORM)
                .where(RuntimeRevisionORM.build_status == "ready")
                .order_by(RuntimeRevisionORM.created_at.desc())
            )
            item = result.scalars().first()
            return None if item is None else self._to_domain(item)

    async def invalidate_revision(self, item_id: str, *, reason: str | None = None) -> RuntimeRevision | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        patch = {
            "build_status": "invalidated",
            "invalidated_at": utc_now().isoformat(),
            "invalidation_reason": reason,
        }
        return await self.update(item_id, patch)


class SQLMCPServerRepository(SQLDomainRepositoryBase):
    def _to_domain(self, orm: MCPServerORM) -> MCPServerDefinition:
        security = dict(orm.security_json or {})
        return MCPServerDefinition.model_validate(
            {
                "id": orm.id,
                "name": orm.name,
                "transport": orm.transport,
                "command": orm.command or "",
                "args": security.get("args", []),
                "url": orm.url,
                "env_refs": security.get("env_refs", orm.env_refs_json),
                "enabled": orm.enabled,
                "allowlisted_command": security.get("allowlisted_command"),
                "metadata": security.get("metadata", {}),
            }
        )

    def _to_orm(self, item: MCPServerDefinition) -> MCPServerORM:
        return MCPServerORM(
            id=item.id,
            name=item.name,
            transport=item.transport.value,
            command=item.command,
            url=item.url,
            env_refs_json=[ref.model_dump(mode="json") for ref in item.env_refs],
            enabled=item.enabled,
            security_json={
                "args": item.args,
                "allowlisted_command": item.allowlisted_command,
                "metadata": item.metadata,
                "env_refs": [ref.model_dump(mode="json") for ref in item.env_refs],
            },
        )

    async def create(self, item: MCPServerDefinition) -> MCPServerDefinition:
        async with self.session_factory() as session:
            entity = self._to_orm(item)
            session.add(entity)
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def save(self, item: MCPServerDefinition) -> MCPServerDefinition:
        async with self.session_factory() as session:
            existing = await session.get(MCPServerORM, item.id)
            if existing is None:
                entity = self._to_orm(item)
                session.add(entity)
            else:
                source = self._to_orm(item)
                entity = existing
                for field in ("name", "transport", "command", "url", "env_refs_json", "enabled", "security_json"):
                    setattr(entity, field, getattr(source, field))
            entity = await self._commit_and_refresh(session, entity)
            return self._to_domain(entity)

    async def list(self, *, include_deleted: bool = False) -> list[MCPServerDefinition]:
        async with self.session_factory() as session:
            stmt = select(MCPServerORM)
            if not include_deleted:
                stmt = stmt.where(MCPServerORM.enabled.is_(True))
            result = await session.execute(stmt.order_by(MCPServerORM.name.asc()))
            return [self._to_domain(item) for item in result.scalars().all()]

    async def get(self, item_id: str, *, include_deleted: bool = False) -> MCPServerDefinition | None:
        async with self.session_factory() as session:
            item = await session.get(MCPServerORM, item_id)
            if item is None:
                return None
            if not include_deleted and not item.enabled:
                return None
            return self._to_domain(item)

    async def update(self, item_id: str, patch: dict[str, Any]) -> MCPServerDefinition | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(MCPServerDefinition.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        async with self.session_factory() as session:
            item = await session.get(MCPServerORM, item_id)
            if item is None:
                return False
            item.enabled = False
            await session.commit()
            return True


class SQLWorkflowRepository(SQLDomainRepositoryBase):
    async def _load_domain(self, session: AsyncSession, workflow_id: str) -> WorkflowDefinition | None:
        workflow = await session.get(WorkflowORM, workflow_id)
        if workflow is None or not workflow.enabled:
            return None
        version_number = workflow.current_version or 1
        result = await session.execute(
            select(WorkflowVersionORM).where(
                WorkflowVersionORM.workflow_id == workflow_id,
                WorkflowVersionORM.version == version_number,
            )
        )
        version = result.scalar_one_or_none()
        if version is None:
            return None
        payload = dict(version.definition_json or {})
        payload["id"] = workflow.id
        payload["name"] = workflow.name
        payload["description"] = workflow.description
        return WorkflowDefinition.model_validate(payload)

    async def create(self, item: WorkflowDefinition) -> WorkflowDefinition:
        async with self.session_factory() as session:
            workflow = WorkflowORM(
                id=item.id,
                name=item.name,
                description=item.description,
                current_version=item.versioning.revision,
                enabled=True,
            )
            version = WorkflowVersionORM(
                id=f"{item.id}:v{item.versioning.revision}",
                workflow_id=item.id,
                version=item.versioning.revision,
                status="published" if item.versioning.is_published else "draft",
                definition_json=item.model_dump(mode="json"),
                published_at=utc_now() if item.versioning.is_published else None,
            )
            session.add(workflow)
            session.add(version)
            await session.commit()
            return item

    async def save(self, item: WorkflowDefinition) -> WorkflowDefinition:
        async with self.session_factory() as session:
            workflow = await session.get(WorkflowORM, item.id)
            if workflow is None:
                workflow = WorkflowORM(
                    id=item.id,
                    name=item.name,
                    description=item.description,
                    current_version=item.versioning.revision,
                    enabled=True,
                )
                session.add(workflow)
            else:
                workflow.name = item.name
                workflow.description = item.description
                workflow.current_version = item.versioning.revision
                workflow.enabled = True
            version_id = f"{item.id}:v{item.versioning.revision}"
            version = await session.get(WorkflowVersionORM, version_id)
            if version is None:
                version = WorkflowVersionORM(
                    id=version_id,
                    workflow_id=item.id,
                    version=item.versioning.revision,
                    status="published" if item.versioning.is_published else "draft",
                    definition_json=item.model_dump(mode="json"),
                    published_at=utc_now() if item.versioning.is_published else None,
                )
                session.add(version)
            else:
                version.status = "published" if item.versioning.is_published else "draft"
                version.definition_json = item.model_dump(mode="json")
                version.published_at = utc_now() if item.versioning.is_published else None
            await session.commit()
            return item

    async def list(self, *, include_deleted: bool = False) -> list[WorkflowDefinition]:
        async with self.session_factory() as session:
            stmt = select(WorkflowORM).order_by(WorkflowORM.name.asc())
            if not include_deleted:
                stmt = stmt.where(WorkflowORM.enabled.is_(True))
            result = await session.execute(stmt)
            items = []
            for workflow in result.scalars().all():
                loaded = await self._load_domain(session, workflow.id)
                if loaded is not None:
                    items.append(loaded)
            return items

    async def get(self, item_id: str, *, include_deleted: bool = False) -> WorkflowDefinition | None:
        async with self.session_factory() as session:
            workflow = await session.get(WorkflowORM, item_id)
            if workflow is None:
                return None
            if not include_deleted and not workflow.enabled:
                return None
            return await self._load_domain(session, item_id)

    async def update(self, item_id: str, patch: dict[str, Any]) -> WorkflowDefinition | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        merged = current.model_dump(mode="json")
        merged.update(patch)
        return await self.save(WorkflowDefinition.model_validate(merged))

    async def soft_delete(self, item_id: str) -> bool:
        async with self.session_factory() as session:
            workflow = await session.get(WorkflowORM, item_id)
            if workflow is None:
                return False
            workflow.enabled = False
            await session.commit()
            return True

    async def get_workflow(self, workflow_id: str) -> WorkflowDefinition | None:
        return await self.get(workflow_id)

    async def save_workflow(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        return await self.save(workflow)

    async def list_versions(self, workflow_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            workflow = await session.get(WorkflowORM, workflow_id)
            if workflow is None:
                return []
            result = await session.execute(
                select(WorkflowVersionORM)
                .where(WorkflowVersionORM.workflow_id == workflow_id)
                .order_by(WorkflowVersionORM.version.desc())
            )
            current_version = workflow.current_version
            items: list[dict[str, Any]] = []
            for version in result.scalars().all():
                definition = dict(version.definition_json or {})
                versioning = definition.get("versioning") if isinstance(definition.get("versioning"), dict) else {}
                metadata = definition.get("metadata") if isinstance(definition.get("metadata"), dict) else {}
                items.append(
                    {
                        "id": version.id,
                        "workflow_id": version.workflow_id,
                        "revision": version.version,
                        "version": versioning.get("version"),
                        "status": version.status,
                        "labels": versioning.get("labels") or [],
                        "parent_version": versioning.get("parent_version"),
                        "is_published": versioning.get("is_published") is True,
                        "is_current": version.version == current_version,
                        "definition": definition,
                        "created_at": version.created_at,
                        "published_at": version.published_at,
                        "provenance": metadata.get("provenance"),
                    }
                )
            return items

    async def get_version(self, workflow_id: str, revision: int) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            workflow = await session.get(WorkflowORM, workflow_id)
            if workflow is None:
                return None
            result = await session.execute(
                select(WorkflowVersionORM).where(
                    WorkflowVersionORM.workflow_id == workflow_id,
                    WorkflowVersionORM.version == revision,
                )
            )
            version = result.scalar_one_or_none()
            if version is None:
                return None
            definition = dict(version.definition_json or {})
            versioning = definition.get("versioning") if isinstance(definition.get("versioning"), dict) else {}
            metadata = definition.get("metadata") if isinstance(definition.get("metadata"), dict) else {}
            return {
                "id": version.id,
                "workflow_id": version.workflow_id,
                "revision": version.version,
                "version": versioning.get("version"),
                "status": version.status,
                "labels": versioning.get("labels") or [],
                "parent_version": versioning.get("parent_version"),
                "is_published": versioning.get("is_published") is True,
                "is_current": version.version == workflow.current_version,
                "definition": definition,
                "created_at": version.created_at,
                "published_at": version.published_at,
                "provenance": metadata.get("provenance"),
            }
