from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ValidationError
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.services.channel_identity import ChannelIdentityMappingService
from app.services.conversations import (
    ConversationApprovalNotFoundError,
    ConversationApprovalPermissionError,
    ConversationApprovalStateError,
    ConversationChannelService,
    ChannelOutboundDeliveryService,
    ChannelWebhookVerificationService,
)
from app.services.conversations.channel_adapters import create_chat_channel_adapter
from .._crud import serializable_validation_errors


class ChannelConversationRequest(BaseModel):
    channel_thread_id: str | None = None
    channel_user_id: str
    channel_display_name: str | None = None
    internal_user_id: str | None = None
    metadata: dict[str, Any] | None = None


class ChannelMessageRequest(ChannelConversationRequest):
    text: str
    response_mode: str = "sync"
    external_message_id: str | None = None
    content: dict[str, Any] | None = None


class ChannelApprovalActionRequest(ChannelConversationRequest):
    approval_request_id: str
    action: str
    reason: str | None = None


class ChannelIdentityMappingRequest(BaseModel):
    channel_type: str
    channel_user_id: str
    internal_user_id: str
    channel_display_name: str | None = None
    trusted: bool = True
    metadata: dict[str, Any] | None = None


class ChannelDeliveryRequest(BaseModel):
    credential_id: str
    provider_outbound_messages: list[dict[str, Any]]


def create_conversation_channels_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/integrations/conversations", tags=["Conversation Channels"])
    service = ConversationChannelService(context)
    delivery_service = ChannelOutboundDeliveryService(context)
    webhook_verification_service = ChannelWebhookVerificationService(context)
    identity_service = ChannelIdentityMappingService(context)

    @router.get("/channel-identity-mappings", summary="List Channel Identity Mappings")
    async def list_channel_identity_mappings(request: Request):
        await resolve_current_user(request, context, required_scopes=["integrations:read"])
        mappings = await identity_service.list_mappings()
        return {"items": [item.model_dump(mode="json") for item in mappings]}

    @router.post("/channel-identity-mappings", summary="Create Or Update Channel Identity Mapping")
    async def upsert_channel_identity_mapping(payload: ChannelIdentityMappingRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            mapping = await identity_service.upsert_mapping(
                channel_type=payload.channel_type,
                channel_user_id=payload.channel_user_id,
                internal_user_id=payload.internal_user_id,
                channel_display_name=payload.channel_display_name,
                trusted=payload.trusted,
                metadata=payload.metadata,
            )
        except (ValidationError, ValueError) as exc:
            detail = serializable_validation_errors(exc) if isinstance(exc, ValidationError) else str(exc)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail) from exc
        return mapping.model_dump(mode="json")

    @router.delete("/channel-identity-mappings/{mapping_id}", summary="Delete Channel Identity Mapping")
    async def delete_channel_identity_mapping(mapping_id: str, request: Request):
        await resolve_current_user(request, context, required_scopes=["integrations:write"])
        deleted = await identity_service.delete_mapping(mapping_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Channel identity mapping '{mapping_id}' not found")
        return {"deleted": True}

    @router.post("/channels/{channel_type}/resolve", summary="Resolve Or Create Channel Conversation")
    async def resolve_channel_conversation(channel_type: str, payload: ChannelConversationRequest):
        try:
            conversation = await service.resolve_or_create_conversation(
                channel_type=channel_type,
                channel_thread_id=payload.channel_thread_id,
                channel_user_id=payload.channel_user_id,
                channel_display_name=payload.channel_display_name,
                internal_user_id=payload.internal_user_id,
                metadata=payload.metadata,
            )
        except (ValidationError, ValueError) as exc:
            detail = serializable_validation_errors(exc) if isinstance(exc, ValidationError) else str(exc)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail) from exc
        return conversation.model_dump(mode="json")

    @router.post("/channels/{channel_type}/messages", summary="Handle Channel Conversation Message")
    async def post_channel_message(channel_type: str, payload: ChannelMessageRequest):
        try:
            return await service.handle_inbound_message(
                channel_type=channel_type,
                channel_thread_id=payload.channel_thread_id,
                channel_user_id=payload.channel_user_id,
                channel_display_name=payload.channel_display_name,
                internal_user_id=payload.internal_user_id,
                text=payload.text,
                response_mode=payload.response_mode,
                message_id=payload.external_message_id,
                content=payload.content,
                metadata=payload.metadata,
            )
        except (ValidationError, ValueError) as exc:
            detail = serializable_validation_errors(exc) if isinstance(exc, ValidationError) else str(exc)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail) from exc

    @router.post("/channels/{channel_type}/approval-actions", summary="Handle Channel Approval Action")
    async def post_channel_approval_action(channel_type: str, payload: ChannelApprovalActionRequest):  # noqa: ARG001
        try:
            return await service.handle_approval_action(
                approval_request_id=payload.approval_request_id,
                action=payload.action,
                channel_type=channel_type,
                channel_user_id=payload.channel_user_id,
                internal_user_id=payload.internal_user_id,
                reason=payload.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        except ConversationApprovalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConversationApprovalPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ConversationApprovalStateError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/adapters/{provider}/webhook", summary="Handle Chat Adapter Webhook")
    async def post_chat_adapter_webhook(provider: str, request: Request, credential_id: str | None = None):
        try:
            body = await request.body()
            verification = await webhook_verification_service.verify(
                provider=provider,
                credential_id=credential_id,
                headers={key.lower(): value for key, value in request.headers.items()},
                body=body,
            )
            payload = json.loads(body) if body else {}
            if not isinstance(payload, dict):
                raise ValueError("Webhook payload must be a JSON object.")
            adapter = create_chat_channel_adapter(context, provider)
            result = await adapter.handle_webhook(payload)
            result["webhook_verification"] = verification
            return result
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload") from exc
        except (ValidationError, ValueError) as exc:
            detail = serializable_validation_errors(exc) if isinstance(exc, ValidationError) else str(exc)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail) from exc
        except ConversationApprovalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConversationApprovalPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ConversationApprovalStateError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/adapters/{provider}/deliver", summary="Deliver Chat Adapter Outbound Messages")
    async def deliver_chat_adapter_messages(provider: str, payload: ChannelDeliveryRequest, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            result = await delivery_service.deliver_for_owner(
                provider=provider,
                credential_id=payload.credential_id,
                owner_user_id=current_user.id,
                provider_outbound_messages=payload.provider_outbound_messages,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")
        return result

    return router


__all__ = ["create_conversation_channels_router"]
