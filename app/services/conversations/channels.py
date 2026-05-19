from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.api.context import ApiContext
from app.domain import ApprovalStatus, Conversation, ConversationChannelType, ConversationMessage, \
    ConversationMessageType
from app.services.channel_identity import ChannelIdentityMappingService
from .core import (
    ConversationApprovalNotFoundError,
    ConversationApprovalPermissionError,
    ConversationApprovalStateError,
    ConversationNotFoundError,
    ConversationService,
)


@dataclass(slots=True)
class ConversationChannelService:
    context: ApiContext

    async def resolve_or_create_conversation(
            self,
            *,
            channel_type: str,
            channel_thread_id: str | None,
            channel_user_id: str,
            channel_display_name: str | None,
            internal_user_id: str | None,
            metadata: dict[str, Any] | None = None,
    ) -> Conversation:
        normalized_channel = ConversationChannelType(channel_type)
        resolved_internal_user_id = await self._resolve_internal_user_id(
            channel_type=normalized_channel,
            channel_user_id=channel_user_id,
            supplied_internal_user_id=internal_user_id,
        )
        items = await self.context.conversation_repo.list()
        for item in items:
            if item.channel_type != normalized_channel:
                continue
            if item.channel_thread_id == channel_thread_id and item.channel_user_id == channel_user_id:
                if resolved_internal_user_id and item.created_by_user_id != resolved_internal_user_id:
                    updated = await self.context.conversation_repo.update(
                        item.id,
                        {"created_by_user_id": resolved_internal_user_id},
                    )
                    return updated or item
                return item

        return await ConversationService(self.context).create_conversation(
            {
                "id": f"conv-{uuid4()}",
                "created_by_user_id": resolved_internal_user_id,
                "channel_type": normalized_channel.value,
                "channel_thread_id": channel_thread_id,
                "channel_user_id": channel_user_id,
                "channel_display_name": channel_display_name,
                "metadata": metadata or {},
            }
        )

    async def handle_inbound_message(
            self,
            *,
            channel_type: str,
            channel_thread_id: str | None,
            channel_user_id: str,
            channel_display_name: str | None,
            internal_user_id: str | None,
            text: str,
            response_mode: str = "sync",
            message_id: str | None = None,
            content: dict[str, Any] | None = None,
            metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conversation = await self.resolve_or_create_conversation(
            channel_type=channel_type,
            channel_thread_id=channel_thread_id,
            channel_user_id=channel_user_id,
            channel_display_name=channel_display_name,
            internal_user_id=internal_user_id,
            metadata=metadata,
        )
        if message_id:
            existing_message = await self._find_existing_external_message(
                conversation_id=conversation.id,
                external_message_id=message_id,
            )
            if existing_message is not None:
                return await self._replay_idempotent_response(
                    conversation=conversation,
                    origin_message=existing_message,
                )
        service = ConversationService(self.context)
        result = await service.post_message(
            conversation.id,
            {
                "message": {
                    "id": message_id or f"msg-{uuid4()}",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": text,
                    "content": {"text": text, **(content or {})},
                    "external_message_id": message_id,
                    "metadata": metadata or {},
                },
                "response_mode": response_mode,
            },
        )
        return {
            "conversation": conversation.model_dump(mode="json"),
            "result": result,
            "outbound_messages": self._transport_messages_from_result(result),
        }

    async def _find_existing_external_message(
            self,
            *,
            conversation_id: str,
            external_message_id: str,
    ) -> ConversationMessage | None:
        repo = self.context.conversation_message_repo
        if hasattr(repo, "find_by_external_message_id"):
            return await repo.find_by_external_message_id(conversation_id, external_message_id)
        for item in await repo.list_by_conversation(conversation_id):
            if item.external_message_id == external_message_id:
                return item
        return None

    async def _replay_idempotent_response(
            self,
            *,
            conversation: Conversation,
            origin_message: ConversationMessage,
    ) -> dict[str, Any]:
        replay_messages = await self._response_messages_after(origin_message)
        outbound_messages: list[dict[str, Any]] = []
        for message in replay_messages:
            approval_request = None
            if message.approval_request_id:
                approval = await self.context.conversation_approval_repo.get(message.approval_request_id)
                if approval is not None:
                    approval_request = approval.model_dump(mode="json")
            transport_message = self._transport_message_from_message(
                message.model_dump(mode="json"),
                approval_request,
            )
            if transport_message is not None:
                outbound_messages.append(transport_message)
        return {
            "conversation": conversation.model_dump(mode="json"),
            "result": {
                "message": origin_message.model_dump(mode="json"),
                "idempotent": True,
                "replayed_message_ids": [message.id for message in replay_messages],
            },
            "outbound_messages": outbound_messages,
        }

    async def _response_messages_after(self, origin_message: ConversationMessage) -> list[ConversationMessage]:
        messages = await self.context.conversation_message_repo.list_by_conversation(origin_message.conversation_id)
        try:
            origin_index = next(index for index, message in enumerate(messages) if message.id == origin_message.id)
        except StopIteration:
            return []
        replay_messages: list[ConversationMessage] = []
        for message in messages[origin_index + 1:]:
            if message.role.value == "user" and message.message_type == ConversationMessageType.USER_TEXT:
                break
            replay_messages.append(message)
        return replay_messages

    async def handle_approval_action(
            self,
            *,
            approval_request_id: str,
            action: str,
            channel_type: str,
            channel_user_id: str,
            internal_user_id: str | None,
            reason: str | None,
    ) -> dict[str, Any]:
        service = ConversationService(self.context)
        normalized_channel = ConversationChannelType(channel_type)
        actor_user_id = await self._resolve_internal_user_id(
            channel_type=normalized_channel,
            channel_user_id=channel_user_id,
            supplied_internal_user_id=internal_user_id,
        ) or f"external:{channel_user_id}"
        if action == "approve":
            result = await service.approve_request(approval_request_id, actor_user_id=actor_user_id, reason=reason)
        elif action == "reject":
            result = await service.reject_request(approval_request_id, actor_user_id=actor_user_id, reason=reason)
        else:
            raise ValueError(f"Unsupported approval action '{action}'")
        return {
            "result": result,
            "outbound_messages": self._transport_messages_from_result(result),
        }

    async def _resolve_internal_user_id(
            self,
            *,
            channel_type: ConversationChannelType,
            channel_user_id: str,
            supplied_internal_user_id: str | None,
    ) -> str | None:
        if channel_type in {ConversationChannelType.API, ConversationChannelType.WEB}:
            return supplied_internal_user_id
        return await ChannelIdentityMappingService(self.context).resolve_trusted_internal_user_id(
            channel_type=channel_type.value,
            channel_user_id=channel_user_id,
        )

    def _transport_messages_from_result(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for key in ("assistant_message", "execution_result_message", "message"):
            message = result.get(key)
            if not isinstance(message, dict):
                continue
            transport_message = self._transport_message_from_message(message, result.get("approval_request"))
            if transport_message is not None:
                items.append(transport_message)
        return items

    def _transport_message_from_message(
            self,
            message: dict[str, Any],
            approval_request: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        message_type = message.get("message_type")
        plain_text = message.get("plain_text")
        if message_type in {"assistant_text", "approval_result", "execution_started", "execution_completed"}:
            return {
                "type": "text",
                "text": plain_text,
                "message_type": message_type,
            }
        if message_type in {"approval_request", "workflow_proposal", "workflow_update_proposal"}:
            request = approval_request or {}
            return {
                "type": "approval",
                "text": plain_text,
                "message_type": message_type,
                "approval_request_id": message.get("approval_request_id") or request.get("id"),
                "approval_status": request.get("status", ApprovalStatus.PENDING.value),
                "actions": [
                    {"type": "approve", "label": "Approve"},
                    {"type": "reject", "label": "Reject"},
                ],
            }
        if message_type == "user_text":
            return None
        return {
            "type": "event",
            "text": plain_text,
            "message_type": message_type,
        }


__all__ = [
    "ConversationApprovalNotFoundError",
    "ConversationApprovalPermissionError",
    "ConversationApprovalStateError",
    "ConversationNotFoundError",
    "ConversationChannelService",
]
