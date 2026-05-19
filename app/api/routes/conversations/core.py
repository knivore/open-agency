from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ValidationError
from starlette.responses import StreamingResponse
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.services.conversations import (
    ConversationApprovalNotFoundError,
    ConversationApprovalPermissionError,
    ConversationApprovalStateError,
    ConversationNotFoundError,
    ConversationService,
)
from app.services.main_agent_setup import MainAgentModelProfileRequiredError, MainAgentSetupInvalidError, \
    MainAgentSetupRequiredError
from app.services.main_agent_setup import MainAgentSetupService
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


class MainAgentProfilePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    default_model_profile_id: str | None = None


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
