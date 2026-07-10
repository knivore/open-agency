"""Conversation CRUD, streaming, approval, and main-agent profile routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ValidationError
from starlette.responses import StreamingResponse
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.core.config import get_settings
from app.services.conversation_compact import (
    ConversationCompactService,
    SUPPORTED_COMPACT_FORMATS,
    SUPPORTED_COMPACT_MODES,
    SUPPORTED_COMPACT_SCOPES,
    SUPPORTED_COMPACT_SOURCE_RANGES,
    SUPPORTED_COMPACT_STRATEGIES,
)
from app.services.conversations.core import (
    ConversationApprovalNotFoundError,
    ConversationApprovalPermissionError,
    ConversationApprovalStateError,
    ConversationNotFoundError,
    ConversationService,
)
from app.services.main_agent_setup.service import (
    MainAgentModelProfileRequiredError,
    MainAgentSetupInvalidError,
    MainAgentSetupRequiredError,
    MainAgentSetupService,
)
from app.services.memory import MemoryPolicyError
from .._crud import serializable_validation_errors


class ConversationPatch(BaseModel):
    title: str | None = None
    status: str | None = None
    metadata: dict[str, Any] | None = None
    created_by_user_id: str | None = None
    main_agent_profile_id: str | None = None
    channel_type: str | None = None
    channel_thread_id: str | None = None
    channel_user_id: str | None = None
    channel_display_name: str | None = None
    workspace_id: str | None = None


class ApprovalDecisionRequest(BaseModel):
    user_id: str
    reason: str | None = None
    store_reason_as_memory: bool = False
    steering_parameters: dict[str, Any] | None = None


class MainAgentProfilePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    default_model_profile_id: str | None = None


class ConversationCompactRequest(BaseModel):
    mode: str = "handoff"
    token_budget: int = 1200
    format: str = "markdown"
    source_execution_id: str | None = None
    source_range: str = "full"
    source_message_start_id: str | None = None
    source_message_end_id: str | None = None
    recent_message_limit: int = 8
    scope: str = "conversation"
    workflow_id: str | None = None
    persist: bool = True
    confirmed: bool = False
    supersede_previous: bool = True
    idempotency_key: str | None = None
    strategy: str = "deterministic"
    model_profile_id: str | None = None
    custom_keep: list[str] | None = None
    custom_drop: list[str] | None = None


def create_conversations_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/conversations", tags=["Conversations"])
    service = ConversationService(context)

    @router.post("", summary="Create Conversation")
    async def create_conversation(payload: dict[str, Any]):
        try:
            created = await service.create_conversation(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except (MainAgentModelProfileRequiredError, MainAgentSetupRequiredError, MainAgentSetupInvalidError) as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        return created.model_dump(mode="json")

    @router.get("", summary="List Conversations")
    async def list_conversations():
        return await service.list_conversations()

    @router.get("/main-agent-profile", summary="Get Active Main-Agent Profile")
    async def get_active_main_agent_profile():
        try:
            profile = await MainAgentSetupService(context).require_active_main_agent_profile()
        except MainAgentSetupRequiredError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except (MainAgentModelProfileRequiredError, MainAgentSetupInvalidError) as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @router.patch("/main-agent-profile", summary="Update Active Main-Agent Profile")
    async def update_active_main_agent_profile(patch: MainAgentProfilePatch):
        try:
            profile = await MainAgentSetupService(context).update_active_main_agent_profile(
                name=patch.name,
                description=patch.description,
                default_model_profile_id=patch.default_model_profile_id,
            )
        except MainAgentSetupRequiredError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except (MainAgentModelProfileRequiredError, MainAgentSetupInvalidError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @router.get("/{conversation_id}", summary="Get Conversation By Id")
    async def get_conversation(conversation_id: str):
        item = await service.get_conversation(conversation_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Conversation '{conversation_id}' not found")
        return item.model_dump(mode="json")

    @router.patch("/{conversation_id}", summary="Update Conversation")
    async def update_conversation(conversation_id: str, patch: ConversationPatch):
        payload = patch.model_dump(exclude_unset=True)
        try:
            item = await service.update_conversation(conversation_id, payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Conversation '{conversation_id}' not found")
        return item.model_dump(mode="json")

    @router.post("/{conversation_id}/messages", summary="Append Conversation Message")
    async def append_message(conversation_id: str, payload: dict[str, Any]):
        try:
            return await service.post_message(conversation_id, payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except (MainAgentModelProfileRequiredError, MainAgentSetupRequiredError, MainAgentSetupInvalidError) as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    @router.get("/{conversation_id}/messages", summary="List Conversation Messages")
    async def list_messages(conversation_id: str):
        try:
            return await service.list_messages(conversation_id)
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/{conversation_id}/context-usage", summary="Get Conversation Context Usage")
    async def get_context_usage(conversation_id: str):
        try:
            return await service.get_context_usage(conversation_id)
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/{conversation_id}/compact", summary="Compact Conversation")
    async def compact_conversation(conversation_id: str, payload: ConversationCompactRequest):
        if not get_settings().memory_context_pack_enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Conversation compact packs are disabled.",
            )
        normalized_mode = payload.mode.strip().lower()
        normalized_strategy = payload.strategy.strip().lower()
        normalized_format = payload.format.strip().lower().replace("-", "_")
        normalized_format = {
            "md": "markdown",
            "structured": "json",
            "markdown+json": "markdown_json",
            "markdown_plus_json": "markdown_json",
            "both": "markdown_json",
        }.get(normalized_format, normalized_format)
        normalized_source_range = payload.source_range.strip().lower()
        normalized_scope = payload.scope.strip().lower()
        if normalized_mode not in SUPPORTED_COMPACT_MODES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_MODES))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported compact mode '{payload.mode}'. Choose one of: {allowed}.",
            )
        if normalized_strategy not in SUPPORTED_COMPACT_STRATEGIES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_STRATEGIES))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported compact strategy '{payload.strategy}'. Choose one of: {allowed}.",
            )
        if normalized_format not in SUPPORTED_COMPACT_FORMATS:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_FORMATS))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported compact format '{payload.format}'. Choose one of: {allowed}.",
            )
        if normalized_source_range not in SUPPORTED_COMPACT_SOURCE_RANGES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_SOURCE_RANGES))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported compact source_range '{payload.source_range}'. Choose one of: {allowed}.",
            )
        if normalized_scope not in SUPPORTED_COMPACT_SCOPES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_SCOPES))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported compact scope '{payload.scope}'. Choose one of: {allowed}.",
            )
        if payload.token_budget < 100 or payload.token_budget > 8000:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="token_budget must be between 100 and 8000.",
            )
        if payload.recent_message_limit < 0 or payload.recent_message_limit > 200:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="recent_message_limit must be between 0 and 200.",
            )
        try:
            return await ConversationCompactService(context).compact_conversation(
                conversation_id,
                mode=normalized_mode,
                token_budget=payload.token_budget,
                output_format=normalized_format,
                source_execution_id=payload.source_execution_id,
                source_range=normalized_source_range,
                source_message_start_id=payload.source_message_start_id,
                source_message_end_id=payload.source_message_end_id,
                recent_message_limit=payload.recent_message_limit,
                scope=normalized_scope,
                workflow_id=payload.workflow_id,
                persist=payload.persist,
                confirmed=payload.confirmed,
                supersede_previous=payload.supersede_previous,
                idempotency_key=payload.idempotency_key,
                strategy=normalized_strategy,
                model_profile_id=payload.model_profile_id,
                custom_keep=payload.custom_keep,
                custom_drop=payload.custom_drop,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except MemoryPolicyError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.get("/{conversation_id}/compact-packs", summary="List Conversation Compact Packs")
    async def list_compact_packs(
            conversation_id: str,
            mode: str | None = None,
            limit: int = 20,
            include_superseded: bool = False,
    ):
        if not get_settings().memory_context_pack_enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Conversation compact packs are disabled.",
            )
        normalized_mode = mode.strip().lower() if mode is not None else None
        if normalized_mode is not None and normalized_mode not in SUPPORTED_COMPACT_MODES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_MODES))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported compact mode '{mode}'. Choose one of: {allowed}.",
            )
        if limit < 0 or limit > 200:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="limit must be between 0 and 200.",
            )
        try:
            return await ConversationCompactService(context).list_compact_packs(
                conversation_id,
                mode=normalized_mode,
                limit=limit,
                include_superseded=include_superseded,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/{conversation_id}/approval-requests", summary="List Conversation Approval Requests")
    async def list_approval_requests(conversation_id: str):
        try:
            return await service.list_approval_requests(conversation_id)
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/{conversation_id}/stream", summary="Stream Conversation Events")
    async def stream_conversation_events(
            conversation_id: str,
            request: Request,
            after: str | None = None,
            idle_timeout_seconds: float = 5.0,
    ):
        if await service.get_conversation(conversation_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Conversation '{conversation_id}' not found")
        try:
            return StreamingResponse(
                service.stream_conversation_events(
                    conversation_id,
                    request,
                    after=after,
                    idle_timeout_seconds=idle_timeout_seconds,
                ),
                media_type="text/event-stream",
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/approval-requests/{approval_request_id}/approve", summary="Approve Conversation Approval Request")
    async def approve_approval_request(approval_request_id: str, payload: ApprovalDecisionRequest):
        try:
            return await service.approve_request(
                approval_request_id,
                actor_user_id=payload.user_id,
                reason=payload.reason,
                steering_parameters=payload.steering_parameters,
            )
        except ConversationApprovalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConversationApprovalPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ConversationApprovalStateError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/approval-requests/{approval_request_id}/reject", summary="Reject Conversation Approval Request")
    async def reject_approval_request(approval_request_id: str, payload: ApprovalDecisionRequest):
        try:
            return await service.reject_request(
                approval_request_id,
                actor_user_id=payload.user_id,
                reason=payload.reason,
                store_reason_as_memory=payload.store_reason_as_memory,
            )
        except ConversationApprovalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConversationApprovalPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ConversationApprovalStateError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post(
        "/approval-requests/{approval_request_id}/request-changes",
        summary="Request Changes To Conversation Approval Request",
    )
    async def request_changes_to_approval_request(approval_request_id: str, payload: ApprovalDecisionRequest):
        try:
            return await service.request_changes_to_approval(
                approval_request_id,
                actor_user_id=payload.user_id,
                reason=payload.reason,
            )
        except ConversationApprovalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConversationApprovalPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ConversationApprovalStateError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post(
        "/approval-requests/{approval_request_id}/split",
        summary="Split Conversation Approval Request",
    )
    async def split_approval_request(approval_request_id: str, payload: ApprovalDecisionRequest):
        try:
            return await service.split_approval_request(
                approval_request_id,
                actor_user_id=payload.user_id,
                reason=payload.reason,
            )
        except ConversationApprovalNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConversationApprovalPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ConversationApprovalStateError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return router
