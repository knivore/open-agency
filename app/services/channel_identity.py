from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.api.context import ApiContext
from app.domain import ChannelIdentityMapping, ConversationChannelType


@dataclass(slots=True)
class ChannelIdentityMappingService:
    context: ApiContext

    async def list_mappings(self) -> list[ChannelIdentityMapping]:
        return await self.context.channel_identity_mapping_repo.list()

    async def get_mapping(self, mapping_id: str) -> ChannelIdentityMapping | None:
        return await self.context.channel_identity_mapping_repo.get(mapping_id)

    async def find_mapping(self, *, channel_type: str, channel_user_id: str) -> ChannelIdentityMapping | None:
        repo = self.context.channel_identity_mapping_repo
        if hasattr(repo, "find_by_channel_identity"):
            return await repo.find_by_channel_identity(channel_type, channel_user_id)
        for item in await repo.list():
            if item.channel_type.value == channel_type and item.channel_user_id == channel_user_id:
                return item
        return None

    async def resolve_trusted_internal_user_id(self, *, channel_type: str, channel_user_id: str) -> str | None:
        mapping = await self.find_mapping(channel_type=channel_type, channel_user_id=channel_user_id)
        if mapping is None or not mapping.trusted:
            return None
        user = await self.context.user_repo.get(mapping.internal_user_id)
        if user is None:
            return None
        if getattr(user.status, "value", user.status) == "disabled":
            return None
        return user.id

    async def upsert_mapping(
            self,
            *,
            channel_type: str,
            channel_user_id: str,
            internal_user_id: str,
            channel_display_name: str | None = None,
            trusted: bool = True,
            metadata: dict[str, Any] | None = None,
    ) -> ChannelIdentityMapping:
        normalized_channel = ConversationChannelType(channel_type)
        user = await self.context.user_repo.get(internal_user_id)
        if user is None:
            raise ValueError(f"Internal user '{internal_user_id}' was not found")
        existing = await self.find_mapping(channel_type=normalized_channel.value, channel_user_id=channel_user_id)
        mapping = ChannelIdentityMapping(
            id=existing.id if existing is not None else f"channel-map-{uuid4()}",
            channel_type=normalized_channel,
            channel_user_id=channel_user_id,
            internal_user_id=internal_user_id,
            channel_display_name=channel_display_name,
            trusted=trusted,
            metadata=metadata or {},
        )
        return await self.context.channel_identity_mapping_repo.save(mapping)

    async def delete_mapping(self, mapping_id: str) -> bool:
        return await self.context.channel_identity_mapping_repo.soft_delete(mapping_id)
