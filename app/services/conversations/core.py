from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncGenerator
from uuid import uuid4

from app.domain import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalTargetType,
    ApprovalType,
    AgentDefinition,
    Conversation,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    ExecutionEventType,
    MainAgentProfile,
    ModelProfileDefinition,
    NodeType,
    ScheduleDefinition,
    ScheduleType,
    TaskDefinition,
    ToolDefinition,
    UserDefinition,
    WorkflowDefinition,
    WorkflowEdgeDefinition,
    WorkflowNodeDefinition,
)
from app.core.config import get_settings
from app.llm.base import ModelMessage
from app.runtime.native.errors import WorkflowNotFoundError
from app.services.agent_tools import (
    AgentToolResolver,
    SYSTEM_COMMAND_RUN_TOOL_ID,
    SYSTEM_WORKFLOW_GET_TOOL_ID,
    SYSTEM_WORKFLOW_LIST_TOOL_ID,
    SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID,
    SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID,
    SYSTEM_WORKFLOW_RUN_TOOL_ID,
    SYSTEM_TOOL_GET_TOOL_ID,
    SYSTEM_TOOL_LIST_TOOL_ID,
    SYSTEM_TOOL_MANAGEMENT_TARGET,
    SYSTEM_MEMORY_DELETE_TOOL_ID,
    SYSTEM_MEMORY_LIST_TOOL_ID,
    SYSTEM_MEMORY_REMEMBER_TOOL_ID,
    SYSTEM_MEMORY_UPDATE_TOOL_ID,
    SYSTEM_TOOL_PROPOSE_CREATE_TOOL_ID,
    SYSTEM_TOOL_PROPOSE_UPDATE_TOOL_ID,
    SYSTEM_WORKFLOW_TOOL_TARGET,
    command_system_tool_definitions,
    is_system_memory_tool,
    is_system_tool_management_tool,
    is_system_workflow_tool,
)
from app.tools.names import make_tool_call_name, tool_call_name, tool_display_name, tool_matches_call_name
from app.services.main_agent_setup import MainAgentSetupService
from app.services.memory import MemoryPolicyError, MemoryService
from app.services.workflow_builder import WorkflowBuilderService
from app.services.workflows import WorkflowService
from app.services.workflow_validation import WorkflowValidationService
from .audit import ConversationAuditService
from .policy import MainAgentPolicyService

if TYPE_CHECKING:
    from app.api.context import ApiContext


logger = logging.getLogger(__name__)

WORKFLOW_CREATE_REQUEST_RE = re.compile(
    r"\b(?:build|create|make|set\s+up|setup|draft|design)\b[\s\S]{0,80}\bworkflow\b"
    r"|\bworkflow\b[\s\S]{0,80}\b(?:build|create|make|set\s+up|setup|draft|design)\b",
    re.IGNORECASE,
)
WORKFLOW_UPDATE_REQUEST_RE = re.compile(
    r"\b(?:update|enhance|improve|modify|change|extend|upgrade|tap\s+on|work\s+on|perform)\b[\s\S]{0,120}\bworkflow\b"
    r"|\bworkflow\b[\s\S]{0,120}\b(?:update|enhance|improve|modify|change|extend|upgrade|tap\s+on|work\s+on|perform)\b",
    re.IGNORECASE,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sse_encode(*, event: str, data: str, event_id: str | None = None) -> str:
    parts: list[str] = []
    if event_id:
        parts.append(f"id: {event_id}")
    parts.append(f"event: {event}")
    for line in data.splitlines() or [""]:
        parts.append(f"data: {line}")
    return "\n".join(parts) + "\n\n"


@dataclass(slots=True)
class ConversationService:
    context: ApiContext
    _execution_watch_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)

    async def create_conversation(self, payload: dict[str, Any]) -> Conversation:
        conversation = Conversation.model_validate(payload)
        if conversation.main_agent_profile_id is None:
            profile = await MainAgentSetupService(self.context).require_active_main_agent_profile()
            conversation = conversation.model_copy(update={"main_agent_profile_id": profile.id})
        return await self.context.conversation_repo.create(conversation)

    async def list_conversations(self) -> dict[str, list[dict[str, Any]]]:
        items = await self.context.conversation_repo.list()
        return {"items": [item.model_dump(mode="json") for item in items]}

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        return await self.context.conversation_repo.get(conversation_id)

    async def update_conversation(self, conversation_id: str, patch: dict[str, Any]) -> Conversation | None:
        return await self.context.conversation_repo.update(conversation_id, patch)

    async def post_message(self, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if await self.context.conversation_repo.get(conversation_id) is None:
            raise ConversationNotFoundError(f"Conversation '{conversation_id}' not found")
        response_mode = payload.get("response_mode", "sync")
        message_payload = payload.get("message", payload)
        validated = ConversationMessage.model_validate({**message_payload, "conversation_id": conversation_id})
        if validated.role != ConversationRole.USER or validated.message_type != ConversationMessageType.USER_TEXT:
            created = await self.context.conversation_message_repo.create(validated)
            await self.context.conversation_event_broker.publish(
                conversation_id,
                self.serialize_message_event(created),
            )
            return {"message": created.model_dump(mode="json")}

        created = await self.context.conversation_message_repo.create(validated)
        await self.context.conversation_event_broker.publish(
            conversation_id,
            self.serialize_message_event(created),
        )

        if response_mode == "async":
            self._schedule_async_user_text_response(
                conversation_id=conversation_id,
                origin_message=created,
            )
            return {
                "message": created.model_dump(mode="json"),
                "stream_url": f"/conversations/{conversation_id}/stream?after={created.id}",
            }

        return await self._complete_user_text_response(
            conversation_id=conversation_id,
            origin_message=created,
            response_mode=response_mode,
        )

    def _schedule_async_user_text_response(
            self,
            *,
            conversation_id: str,
            origin_message: ConversationMessage,
    ) -> None:
        task = asyncio.create_task(
            self._complete_user_text_response(
                conversation_id=conversation_id,
                origin_message=origin_message,
                response_mode="async",
            )
        )

        def _log_unhandled_failure(done: asyncio.Task[dict[str, Any]]) -> None:
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Async conversation response failed for conversation %s", conversation_id)

        task.add_done_callback(_log_unhandled_failure)

    async def _complete_user_text_response(
            self,
            *,
            conversation_id: str,
            origin_message: ConversationMessage,
            response_mode: str,
    ) -> dict[str, Any]:
        created = origin_message
        try:
            return await self._complete_user_text_response_or_raise(
                conversation_id=conversation_id,
                origin_message=origin_message,
                response_mode=response_mode,
            )
        except Exception as exc:
            logger.exception("Failed to complete conversation response for conversation %s", conversation_id)
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I could not complete the main-agent response: {exc}",
                metadata={
                    "delivery": "direct",
                    "error": "main_agent_response_failed",
                    "error_type": type(exc).__name__,
                },
            )
            await self._generate_title_if_needed(conversation_id)
            response = {
                "message": created.model_dump(mode="json"),
                "assistant_message": assistant_message.model_dump(mode="json"),
            }
            if response_mode in {"async", "stream"}:
                response["stream_url"] = f"/conversations/{conversation_id}/stream?after={created.id}"
            return response

    async def _complete_user_text_response_or_raise(
            self,
            *,
            conversation_id: str,
            origin_message: ConversationMessage,
            response_mode: str,
    ) -> dict[str, Any]:
        created = origin_message
        budget_decision = await self._policy().check_external_channel_message_budget(conversation_id)
        if not budget_decision.allowed:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=budget_decision.reason or "This external channel cannot send more main-agent requests right now.",
                metadata={"delivery": "direct", "policy_code": budget_decision.code},
            )
            await self._generate_title_if_needed(conversation_id)
            response = {
                "message": created.model_dump(mode="json"),
                "assistant_message": assistant_message.model_dump(mode="json"),
            }
            if response_mode in {"async", "stream"}:
                response["stream_url"] = f"/conversations/{conversation_id}/stream?after={created.id}"
            return response
        profile = await self._resolve_main_profile(conversation_id)
        execution_response = await self._maybe_handle_execution_request(
            profile=profile,
            conversation_id=conversation_id,
            origin_message=created,
            response_mode=response_mode,
        )
        if execution_response is not None:
            await self._generate_title_if_needed(conversation_id)
            return execution_response
        proposal_response = await self._maybe_handle_workflow_mutation_proposal(
            profile=profile,
            conversation_id=conversation_id,
            origin_message=created,
            response_mode=response_mode,
            infer_plain_text=False,
        )
        if proposal_response is not None:
            await self._generate_title_if_needed(conversation_id)
            return proposal_response
        approval_request = await self._maybe_create_approval_request(
            profile=profile,
            conversation_id=conversation_id,
            origin_message=created,
        )
        if approval_request is not None:
            await self._generate_title_if_needed(conversation_id)
            response = {
                "message": created.model_dump(mode="json"),
                "assistant_message": approval_request["message"],
                "approval_request": approval_request["approval_request"],
            }
            if response_mode in {"async", "stream"}:
                response["stream_url"] = f"/conversations/{conversation_id}/stream?after={created.id}"
            return response
        assistant_payload = await self._generate_assistant_reply(profile=profile, conversation_id=conversation_id)
        await self._generate_title_if_needed(conversation_id)
        response: dict[str, Any] = {
            "message": created.model_dump(mode="json"),
            "assistant_message": assistant_payload["assistant_message"],
        }
        if assistant_payload.get("approval_request") is not None:
            response["approval_request"] = assistant_payload["approval_request"]
        if response_mode in {"async", "stream"}:
            response["stream_url"] = f"/conversations/{conversation_id}/stream?after={created.id}"
        return response

    async def list_messages(self, conversation_id: str) -> dict[str, list[dict[str, Any]]]:
        if await self.context.conversation_repo.get(conversation_id) is None:
            raise ConversationNotFoundError(f"Conversation '{conversation_id}' not found")
        items = await self.context.conversation_message_repo.list_by_conversation(conversation_id)
        return {"items": [item.model_dump(mode="json") for item in items]}

    async def list_approval_requests(self, conversation_id: str) -> dict[str, list[dict[str, Any]]]:
        if await self.context.conversation_repo.get(conversation_id) is None:
            raise ConversationNotFoundError(f"Conversation '{conversation_id}' not found")
        items = await self.context.conversation_approval_repo.list_by_conversation(conversation_id)
        return {"items": [item.model_dump(mode="json") for item in items]}

    async def approve_request(self, approval_request_id: str, *, actor_user_id: str, reason: str | None) -> dict[
        str, Any]:
        approval = await self.context.conversation_approval_repo.get(approval_request_id)
        if approval is None:
            raise ConversationApprovalNotFoundError(f"Approval request '{approval_request_id}' not found")
        if approval.status != ApprovalStatus.PENDING:
            raise ConversationApprovalStateError(f"Approval request '{approval_request_id}' is not pending")
        await self._assert_may_resolve_approval(approval, actor_user_id=actor_user_id)
        resolved = await self.context.conversation_approval_repo.update(
            approval.id,
            {
                "status": ApprovalStatus.APPROVED.value,
                "decision_reason": reason,
                "approved_by_user_id": actor_user_id,
            },
        )
        assert resolved is not None
        result_message = await self._append_approval_result_message(resolved)
        workflow_payload = await self._maybe_apply_workflow_mutation_from_approval(resolved)
        tool_mutation_payload = await self._maybe_apply_tool_mutation_from_approval(resolved)
        execution_payload = await self._maybe_launch_execution_from_approval(resolved)
        tool_payload = await self._maybe_execute_tool_from_approval(resolved)
        await self.publish_approval_resolved(resolved.conversation_id, resolved.model_dump(mode="json"))
        response = {
            "approval_request": resolved.model_dump(mode="json"),
            "message": result_message.model_dump(mode="json"),
        }
        if workflow_payload is not None:
            response["workflow"] = workflow_payload
        if tool_mutation_payload is not None:
            response["tool"] = tool_mutation_payload
        if execution_payload is not None:
            response.update(execution_payload)
        if tool_payload is not None:
            response.update(tool_payload)
        return response

    async def reject_request(
            self,
            approval_request_id: str,
            *,
            actor_user_id: str,
            reason: str | None,
            store_reason_as_memory: bool = False,
    ) -> dict[str, Any]:
        approval = await self.context.conversation_approval_repo.get(approval_request_id)
        if approval is None:
            raise ConversationApprovalNotFoundError(f"Approval request '{approval_request_id}' not found")
        if approval.status != ApprovalStatus.PENDING:
            raise ConversationApprovalStateError(f"Approval request '{approval_request_id}' is not pending")
        await self._assert_may_resolve_approval(approval, actor_user_id=actor_user_id)
        resolved = await self.context.conversation_approval_repo.update(
            approval.id,
            {
                "status": ApprovalStatus.REJECTED.value,
                "decision_reason": reason,
                "approved_by_user_id": actor_user_id,
            },
        )
        assert resolved is not None
        memory_payload = await self._maybe_store_rejected_approval_memory(
            resolved,
            store_reason_as_memory=store_reason_as_memory,
        )
        workflow_payload = await self._maybe_persist_rejected_create_draft(resolved)
        result_message = await self._append_approval_result_message(resolved)
        await self.publish_approval_resolved(resolved.conversation_id, resolved.model_dump(mode="json"))
        response = {
            "approval_request": resolved.model_dump(mode="json"),
            "message": result_message.model_dump(mode="json"),
        }
        if workflow_payload is not None:
            response["workflow"] = workflow_payload
        if memory_payload is not None:
            response["memory"] = memory_payload
        return response

    async def request_changes_to_approval(
            self,
            approval_request_id: str,
            *,
            actor_user_id: str,
            reason: str | None,
    ) -> dict[str, Any]:
        approval = await self.context.conversation_approval_repo.get(approval_request_id)
        if approval is None:
            raise ConversationApprovalNotFoundError(f"Approval request '{approval_request_id}' not found")
        if approval.status != ApprovalStatus.PENDING:
            raise ConversationApprovalStateError(f"Approval request '{approval_request_id}' is not pending")
        await self._assert_may_resolve_approval(approval, actor_user_id=actor_user_id)
        requested_at = utcnow().isoformat()
        revision_request = {
            "requested_by_user_id": actor_user_id,
            "reason": reason,
            "requested_at": requested_at,
        }
        metadata = dict(approval.metadata or {})
        revision_requests = list(metadata.get("revision_requests") or [])
        revision_requests.append(revision_request)
        metadata.update(
            {
                "revision_requested": True,
                "last_revision_request": revision_request,
                "revision_requests": revision_requests,
            }
        )
        resolved = await self.context.conversation_approval_repo.update(
            approval.id,
            {
                "status": ApprovalStatus.CANCELLED.value,
                "decision_reason": reason,
                "approved_by_user_id": actor_user_id,
                "metadata": metadata,
            },
        )
        assert resolved is not None
        result_message = await self._append_approval_revision_requested_message(resolved)
        await self.publish_approval_revision_requested(resolved.conversation_id, resolved.model_dump(mode="json"))
        return {
            "approval_request": resolved.model_dump(mode="json"),
            "message": result_message.model_dump(mode="json"),
        }

    async def split_approval_request(
            self,
            approval_request_id: str,
            *,
            actor_user_id: str,
            reason: str | None = None,
    ) -> dict[str, Any]:
        approval = await self.context.conversation_approval_repo.get(approval_request_id)
        if approval is None:
            raise ConversationApprovalNotFoundError(f"Approval request '{approval_request_id}' not found")
        if approval.status != ApprovalStatus.PENDING:
            raise ConversationApprovalStateError(f"Approval request '{approval_request_id}' is not pending")
        await self._assert_may_resolve_approval(approval, actor_user_id=actor_user_id)
        parts = self._split_approval_parts(approval)
        if len(parts) < 2:
            raise ConversationApprovalStateError(f"Approval request '{approval_request_id}' cannot be split")

        split_at = utcnow().isoformat()
        metadata = dict(approval.metadata or {})
        metadata.update(
            {
                "split_requested": True,
                "split_requested_by_user_id": actor_user_id,
                "split_requested_at": split_at,
                "split_reason": reason,
                "split_part_count": len(parts),
            }
        )
        parent = await self.context.conversation_approval_repo.update(
            approval.id,
            {
                "status": ApprovalStatus.CANCELLED.value,
                "decision_reason": reason,
                "approved_by_user_id": actor_user_id,
                "metadata": metadata,
            },
        )
        assert parent is not None

        children: list[ApprovalRequest] = []
        profile_id = approval.requested_by_profile_id or ""
        for part in parts:
            child_metadata = {
                **approval.metadata,
                **part["metadata"],
                "parent_approval_request_id": approval.id,
                "split_from_parent": True,
                "split_requested_by_user_id": actor_user_id,
                "split_requested_at": split_at,
            }
            child = ApprovalRequest(
                approval_type=part["approval_type"],
                target_type=approval.target_type,
                target_id=approval.target_id,
                requested_by_agent_id=approval.requested_by_agent_id,
                requested_by_profile_id=approval.requested_by_profile_id,
                conversation_id=approval.conversation_id,
                origin_message_id=approval.origin_message_id,
                summary=part["summary"],
                diff_summary=part["diff_summary"],
                proposed_payload=part["proposed_payload"],
                metadata=child_metadata,
            )
            created = await self.context.conversation_approval_repo.create(child)
            children.append(created)
            await self._append_approval_request_message(
                conversation_id=approval.conversation_id,
                profile_id=profile_id,
                approval=created,
                target={
                    "target_type": created.target_type.value,
                    "target_id": created.target_id,
                    "split_part": part["metadata"]["split_part"],
                },
            )
            await self.publish_approval_requested(approval.conversation_id, created.model_dump(mode="json"))

        split_message = await self._append_approval_split_message(parent, children)
        await self.context.conversation_event_broker.publish(
            approval.conversation_id,
            self.serialize_approval_event(
                conversation_id=approval.conversation_id,
                event_type="approval.split",
                approval=parent.model_dump(mode="json"),
            ),
        )
        return {
            "approval_request": parent.model_dump(mode="json"),
            "approval_requests": [item.model_dump(mode="json") for item in children],
            "message": split_message.model_dump(mode="json"),
        }

    def serialize_message_event(self, message: ConversationMessage) -> dict[str, Any]:
        return {
            "id": message.id,
            "conversation_id": message.conversation_id,
            "event_type": "message.created",
            "occurred_at": message.created_at.isoformat(),
            "message": message.model_dump(mode="json"),
        }

    def serialize_approval_event(
            self,
            *,
            conversation_id: str,
            event_type: str,
            approval: dict[str, Any],
    ) -> dict[str, Any]:
        occurred_at = approval.get("updated_at") or approval.get("created_at") or utcnow().isoformat()
        event_id = approval.get("id") or f"{event_type}:{uuid4()}"
        return {
            "id": event_id,
            "conversation_id": conversation_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "approval": approval,
        }

    def serialize_idle_event(self, conversation_id: str) -> dict[str, Any]:
        return {
            "id": f"idle:{uuid4()}",
            "conversation_id": conversation_id,
            "event_type": "conversation.idle",
            "occurred_at": utcnow().isoformat(),
        }

    def _policy(self) -> MainAgentPolicyService:
        return MainAgentPolicyService(self.context)

    def _memory(self) -> MemoryService:
        return MemoryService(self.context)

    def serialize_error_event(self, conversation_id: str, detail: str) -> dict[str, Any]:
        return {
            "id": f"error:{uuid4()}",
            "conversation_id": conversation_id,
            "event_type": "error",
            "occurred_at": utcnow().isoformat(),
            "error": {"detail": detail},
        }

    async def _audit_conversation_event(
            self,
            *,
            conversation_id: str,
            event_type: ExecutionEventType,
            payload: dict[str, Any] | None = None,
            metadata: dict[str, Any] | None = None,
            metrics: dict[str, Any] | None = None,
            actor: str | None = None,
            agent_id: str | None = None,
            tool_call_id: str | None = None,
            model_request_id: str | None = None,
    ) -> None:
        try:
            await ConversationAuditService(self.context).emit(
                conversation_id=conversation_id,
                event_type=event_type,
                payload=payload,
                metadata=metadata,
                metrics=metrics,
                actor=actor,
                agent_id=agent_id,
                tool_call_id=tool_call_id,
                model_request_id=model_request_id,
            )
        except Exception:
            return

    async def publish_approval_requested(self, conversation_id: str, approval: dict[str, Any]) -> None:
        await self.context.conversation_event_broker.publish(
            conversation_id,
            self.serialize_approval_event(
                conversation_id=conversation_id,
                event_type="approval.requested",
                approval=approval,
            ),
        )
        await self._audit_conversation_event(
            conversation_id=conversation_id,
            event_type=ExecutionEventType.APPROVAL_REQUESTED,
            payload={
                "approval_request_id": approval.get("id"),
                "approval_type": approval.get("approval_type"),
                "target_type": approval.get("target_type"),
                "target_id": approval.get("target_id"),
                "summary": approval.get("summary"),
                "diff_summary": approval.get("diff_summary"),
            },
            metadata={
                "source": "conversation",
                "requested_by_profile_id": approval.get("requested_by_profile_id"),
                "requested_by_agent_id": approval.get("requested_by_agent_id"),
            },
            agent_id=approval.get("requested_by_agent_id"),
        )

    async def publish_approval_resolved(self, conversation_id: str, approval: dict[str, Any]) -> None:
        await self.context.conversation_event_broker.publish(
            conversation_id,
            self.serialize_approval_event(
                conversation_id=conversation_id,
                event_type="approval.resolved",
                approval=approval,
            ),
        )
        status_value = approval.get("status")
        event_type = (
            ExecutionEventType.APPROVAL_GRANTED
            if status_value == ApprovalStatus.APPROVED.value
            else ExecutionEventType.APPROVAL_REJECTED
        )
        await self._audit_conversation_event(
            conversation_id=conversation_id,
            event_type=event_type,
            payload={
                "approval_request_id": approval.get("id"),
                "approval_type": approval.get("approval_type"),
                "target_type": approval.get("target_type"),
                "target_id": approval.get("target_id"),
                "status": status_value,
                "decision_reason": approval.get("decision_reason"),
            },
            metadata={
                "source": "conversation",
                "resolved_by_user_id": approval.get("approved_by_user_id"),
            },
            actor=approval.get("approved_by_user_id"),
            agent_id=approval.get("requested_by_agent_id"),
        )

    async def publish_approval_revision_requested(self, conversation_id: str, approval: dict[str, Any]) -> None:
        await self.context.conversation_event_broker.publish(
            conversation_id,
            self.serialize_approval_event(
                conversation_id=conversation_id,
                event_type="approval.revision_requested",
                approval=approval,
            ),
        )
        await self._audit_conversation_event(
            conversation_id=conversation_id,
            event_type=ExecutionEventType.AGENT_MESSAGE_CREATED,
            payload={
                "approval_request_id": approval.get("id"),
                "approval_type": approval.get("approval_type"),
                "target_type": approval.get("target_type"),
                "target_id": approval.get("target_id"),
                "status": approval.get("status"),
                "revision_request": (approval.get("metadata") or {}).get("last_revision_request"),
            },
            metadata={
                "source": "conversation",
                "audit_kind": "approval_revision_requested",
            },
            actor=approval.get("approved_by_user_id"),
            agent_id=approval.get("requested_by_agent_id"),
        )

    async def stream_conversation_events(
            self,
            conversation_id: str,
            request: Any,
            *,
            after: str | None = None,
            idle_timeout_seconds: float = 5.0,
    ) -> AsyncGenerator[str, None]:
        if await self.context.conversation_repo.get(conversation_id) is None:
            raise ConversationNotFoundError(f"Conversation '{conversation_id}' not found")

        try:
            messages = await self.context.conversation_message_repo.list_by_conversation(conversation_id)
            for message in self._messages_after_cursor(messages, after):
                event = self.serialize_message_event(message)
                yield sse_encode(event=event["event_type"], data=_json_dump(event), event_id=event["id"])

            queue = await self.context.conversation_event_broker.subscribe(conversation_id)
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=idle_timeout_seconds)
                    except asyncio.TimeoutError:
                        idle_event = self.serialize_idle_event(conversation_id)
                        yield sse_encode(
                            event=idle_event["event_type"],
                            data=_json_dump(idle_event),
                            event_id=idle_event["id"],
                        )
                        continue
                    yield sse_encode(event=event["event_type"], data=_json_dump(event), event_id=event["id"])
            finally:
                await self.context.conversation_event_broker.unsubscribe(conversation_id, queue)
        except ConversationNotFoundError:
            raise
        except Exception as exc:
            error_event = self.serialize_error_event(conversation_id, str(exc))
            yield sse_encode(event=error_event["event_type"], data=_json_dump(error_event), event_id=error_event["id"])

    def _messages_after_cursor(
            self,
            messages: list[ConversationMessage],
            after: str | None,
    ) -> list[ConversationMessage]:
        if after is None:
            return messages
        seen = False
        filtered: list[ConversationMessage] = []
        for message in messages:
            if seen:
                filtered.append(message)
                continue
            if message.id == after:
                seen = True
        return filtered if seen else messages

    async def _maybe_create_approval_request(
            self,
            *,
            profile: MainAgentProfile,
            conversation_id: str,
            origin_message: ConversationMessage,
    ) -> dict[str, Any] | None:
        if self._execution_request_payload(origin_message) is not None:
            return None
        structured = origin_message.content.get("approval_request") if isinstance(origin_message.content,
                                                                                  dict) else None
        if not isinstance(structured, dict):
            return None

        approval = ApprovalRequest(
            approval_type=structured.get("approval_type", ApprovalType.OTHER.value),
            target_type=structured.get("target_type", ApprovalTargetType.OTHER.value),
            target_id=structured.get("target_id"),
            requested_by_agent_id=profile.agent_id,
            requested_by_profile_id=profile.id,
            conversation_id=conversation_id,
            origin_message_id=origin_message.id,
            summary=structured.get("summary", "Approval requested"),
            diff_summary=structured.get("diff_summary"),
            proposed_payload=structured.get("proposed_payload"),
            metadata={"source": "conversation", **structured.get("metadata", {})},
        )
        created = await self.context.conversation_approval_repo.create(approval)
        approval_message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.APPROVAL_REQUEST,
                plain_text=created.summary,
                approval_request_id=created.id,
                content={
                    "approval_request_id": created.id,
                    "approval_type": created.approval_type.value,
                    "summary": created.summary,
                    "status": created.status.value,
                    "target": {
                        "type": created.target_type.value,
                        "id": created.target_id,
                    },
                },
                metadata={"profile_id": profile.id},
            )
        )
        await self.context.conversation_event_broker.publish(
            conversation_id,
            self.serialize_message_event(approval_message),
        )
        await self.publish_approval_requested(conversation_id, created.model_dump(mode="json"))
        return {
            "approval_request": created.model_dump(mode="json"),
            "message": approval_message.model_dump(mode="json"),
        }

    async def _maybe_handle_execution_request(
            self,
            *,
            profile: MainAgentProfile,
            conversation_id: str,
            origin_message: ConversationMessage,
            response_mode: str,
    ) -> dict[str, Any] | None:
        request = self._execution_request_payload(origin_message)
        if request is None:
            return None

        workflow_id = request.get("workflow_id")
        if not workflow_id:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text="I could not run that because no workflow_id was provided.",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            response = {
                "message": origin_message.model_dump(mode="json"),
                "assistant_message": assistant_message.model_dump(mode="json"),
            }
            if response_mode == "stream":
                response["stream_url"] = f"/conversations/{conversation_id}/stream?after={origin_message.id}"
            return response

        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I could not find workflow '{workflow_id}'.",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            response = {
                "message": origin_message.model_dump(mode="json"),
                "assistant_message": assistant_message.model_dump(mode="json"),
            }
            if response_mode == "stream":
                response["stream_url"] = f"/conversations/{conversation_id}/stream?after={origin_message.id}"
            return response

        visibility_decision = self._policy().check_workflow_visibility(workflow)
        if not visibility_decision.allowed:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=visibility_decision.reason or f"I cannot access workflow '{workflow.name}'.",
                metadata={"profile_id": profile.id, "delivery": "direct", "policy_code": visibility_decision.code},
            )
            response = {
                "message": origin_message.model_dump(mode="json"),
                "assistant_message": assistant_message.model_dump(mode="json"),
            }
            if response_mode == "stream":
                response["stream_url"] = f"/conversations/{conversation_id}/stream?after={origin_message.id}"
            return response

        channel_decision = await self._policy().check_workflow_execution_channel(conversation_id)
        if not channel_decision.allowed:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=channel_decision.reason or "This channel is not allowed to launch workflows without a trusted mapped identity.",
                metadata={"profile_id": profile.id, "delivery": "direct", "policy_code": channel_decision.code},
            )
            response = {
                "message": origin_message.model_dump(mode="json"),
                "assistant_message": assistant_message.model_dump(mode="json"),
            }
            if response_mode == "stream":
                response["stream_url"] = f"/conversations/{conversation_id}/stream?after={origin_message.id}"
            return response

        if self._workflow_requires_protected_approval(workflow):
            approval = ApprovalRequest(
                approval_type=ApprovalType.WORKFLOW_EXECUTION,
                target_type=ApprovalTargetType.WORKFLOW,
                target_id=workflow.id,
                requested_by_agent_id=profile.agent_id,
                requested_by_profile_id=profile.id,
                conversation_id=conversation_id,
                origin_message_id=origin_message.id,
                summary=f"Run protected workflow '{workflow.name}'.",
                proposed_payload={
                    "workflow_id": workflow.id,
                    "input_payload": request.get("input_payload", {}),
                    "runtime_adapter_id": request.get("runtime_adapter_id"),
                },
                metadata={"action": "workflow_execution"},
            )
            created = await self.context.conversation_approval_repo.create(approval)
            approval_message = await self.context.conversation_message_repo.create(
                ConversationMessage(
                    conversation_id=conversation_id,
                    role=ConversationRole.ASSISTANT,
                    message_type=ConversationMessageType.APPROVAL_REQUEST,
                    plain_text=created.summary,
                    approval_request_id=created.id,
                    content={
                        "approval_request_id": created.id,
                        "approval_type": created.approval_type.value,
                        "summary": created.summary,
                        "status": created.status.value,
                        "target": {
                            "type": created.target_type.value,
                            "id": created.target_id,
                            "name": workflow.name,
                        },
                    },
                    metadata={"profile_id": profile.id},
                )
            )
            await self.context.conversation_event_broker.publish(
                conversation_id,
                self.serialize_message_event(approval_message),
            )
            await self.publish_approval_requested(conversation_id, created.model_dump(mode="json"))
            response = {
                "message": origin_message.model_dump(mode="json"),
                "assistant_message": approval_message.model_dump(mode="json"),
                "approval_request": created.model_dump(mode="json"),
            }
            if response_mode == "stream":
                response["stream_url"] = f"/conversations/{conversation_id}/stream?after={origin_message.id}"
            return response

        launch = await self._launch_execution_from_request(
            workflow=workflow,
            profile=profile,
            conversation_id=conversation_id,
            origin_message_id=origin_message.id,
            input_payload=request.get("input_payload", {}),
            runtime_adapter_id=request.get("runtime_adapter_id"),
        )
        response = {
            "message": origin_message.model_dump(mode="json"),
            **launch,
        }
        if response_mode == "stream":
            response["stream_url"] = f"/conversations/{conversation_id}/stream?after={origin_message.id}"
        return response

    async def _maybe_handle_workflow_mutation_proposal(
            self,
            *,
            profile: MainAgentProfile,
            conversation_id: str,
            origin_message: ConversationMessage,
            response_mode: str,
            infer_plain_text: bool = True,
    ) -> dict[str, Any] | None:
        create_request = self._workflow_create_proposal_payload(origin_message)
        update_request = self._workflow_update_proposal_payload(origin_message)
        if infer_plain_text and create_request is None and update_request is None:
            create_request = self._workflow_create_request_from_plain_text(origin_message)
        if infer_plain_text and create_request is None and update_request is None:
            update_request = await self._workflow_update_request_from_plain_text(origin_message)
        if create_request is None and update_request is None:
            return None

        if create_request is not None:
            response = await self._create_workflow_create_proposal(
                profile=profile,
                conversation_id=conversation_id,
                origin_message_id=origin_message.id,
                request=create_request,
            )
            response["message"] = origin_message.model_dump(mode="json")
            if response_mode == "stream":
                response["stream_url"] = f"/conversations/{conversation_id}/stream?after={origin_message.id}"
            return response

        assert update_request is not None
        response = await self._create_workflow_update_proposal(
            profile=profile,
            conversation_id=conversation_id,
            origin_message_id=origin_message.id,
            request=update_request,
        )
        response["message"] = origin_message.model_dump(mode="json")
        if response_mode == "stream":
            response["stream_url"] = f"/conversations/{conversation_id}/stream?after={origin_message.id}"
        return response

    def _workflow_create_request_from_plain_text(self, origin_message: ConversationMessage) -> dict[str, Any] | None:
        text = (origin_message.plain_text or "").strip()
        if not text or not WORKFLOW_CREATE_REQUEST_RE.search(text):
            return None
        return {
            "summary": "Create a workflow from the user's request.",
            "goal": text,
            "conversation_history": text,
            "plain_text_request": True,
        }

    async def _workflow_update_request_from_plain_text(
            self,
            origin_message: ConversationMessage,
    ) -> dict[str, Any] | None:
        text = (origin_message.plain_text or "").strip()
        if not text or not WORKFLOW_UPDATE_REQUEST_RE.search(text):
            return None
        workflow_id = self._workflow_id_from_plain_text(text)
        if workflow_id is None and len(text.split()) < 6:
            return None
        if workflow_id is None:
            workflow_id = await self._infer_workflow_id_for_plain_text_update(text)
        return {
            "workflow_id": workflow_id,
            "summary": "Update workflow from the user's request.",
            "goal": text,
            "conversation_history": text,
            "plain_text_request": True,
        }

    def _workflow_id_from_plain_text(self, text: str) -> str | None:
        match = re.search(r"\bworkflow[-_][a-zA-Z0-9][a-zA-Z0-9_-]*\b", text)
        return match.group(0) if match else None

    async def _infer_workflow_id_for_plain_text_update(self, text: str) -> str | None:
        workflows = [
            workflow
            for workflow in await self.context.workflow_repo.list()
            if self._policy().workflow_is_visible(workflow)
               and self._policy().workflow_is_mutable(workflow)
        ]
        if len(workflows) == 1:
            return workflows[0].id
        scored = sorted(
            (
                (self._plain_text_workflow_match_score(text, workflow), workflow)
                for workflow in workflows
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not scored or scored[0][0] <= 0:
            return None
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1].id

    def _plain_text_workflow_match_score(self, text: str, workflow: WorkflowDefinition) -> int:
        query_terms = {
            token
            for token in re.findall(r"[a-z0-9][a-z0-9_-]+", text.lower())
            if len(token) >= 4
               and token not in {"workflow", "enhance", "update", "improve", "perform", "based", "output"}
        }
        workflow_text = " ".join(
            [
                workflow.id,
                workflow.name,
                workflow.description or "",
                str(workflow.metadata.get("original_goal", "")),
            ]
        ).lower()
        score = sum(1 for term in query_terms if term in workflow_text)
        if "agency-fe" in text.lower() and "agency-fe" in workflow_text:
            score += 3
        if "repo" in text.lower() and "repo" in workflow_text:
            score += 2
        return score

    async def _create_workflow_create_proposal(
            self,
            *,
            profile: MainAgentProfile,
            conversation_id: str,
            origin_message_id: str,
            request: dict[str, Any],
    ) -> dict[str, Any]:
        mutation_decision = self._policy().check_workflow_mutation_enabled()
        if not mutation_decision.allowed:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=mutation_decision.reason or "Main-agent workflow mutation is disabled by policy.",
                metadata={"profile_id": profile.id, "delivery": "direct", "policy_code": mutation_decision.code},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        channel_decision = await self._policy().check_workflow_mutation_channel(conversation_id)
        if not channel_decision.allowed:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=channel_decision.reason or "This channel is not allowed to create or update workflows without a trusted mapped identity.",
                metadata={"profile_id": profile.id, "delivery": "direct", "policy_code": channel_decision.code},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        try:
            proposed_workflow = await self._workflow_from_create_request(profile=profile, request=request)
        except ValueError as exc:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I could not create that workflow proposal because {exc}",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        existing = await self.context.workflow_repo.get(proposed_workflow.id, include_deleted=True)
        if existing is not None:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I cannot create workflow '{proposed_workflow.name}' because that workflow id already exists.",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        proposed_workflow, repair_metadata = await self._repair_workflow_until_valid(
            proposed_workflow,
            model_profile_id=(
                request.get("model_profile_id")
                if isinstance(request.get("model_profile_id"), str)
                else profile.default_model_profile_id
            ),
            repair_goal=request.get("goal") if isinstance(request.get("goal"), str) else None,
        )
        validation_errors = repair_metadata.get("remaining_errors", [])
        if validation_errors:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text="I could not create that workflow proposal because validation failed: "
                     + "; ".join(validation_errors[:3]),
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        summary = request.get("summary") or f"Create workflow '{proposed_workflow.name}'."
        diff_summary = request.get("diff_summary") or self._workflow_create_diff_summary(proposed_workflow)
        approval = ApprovalRequest(
            approval_type=ApprovalType.WORKFLOW_CREATE,
            target_type=ApprovalTargetType.WORKFLOW,
            target_id=proposed_workflow.id,
            requested_by_agent_id=profile.agent_id,
            requested_by_profile_id=profile.id,
            conversation_id=conversation_id,
            origin_message_id=origin_message_id,
            summary=summary,
            diff_summary=diff_summary,
            proposed_payload={"workflow": proposed_workflow.model_dump(mode="json")},
            metadata={"action": "workflow_create", **repair_metadata},
        )
        created = await self.context.conversation_approval_repo.create(approval)
        proposal_message = await self._append_workflow_proposal_message(
            conversation_id=conversation_id,
            profile_id=profile.id,
            approval=created,
            workflow=proposed_workflow,
            message_type=ConversationMessageType.WORKFLOW_PROPOSAL,
        )
        await self.publish_approval_requested(conversation_id, created.model_dump(mode="json"))
        return {
            "assistant_message": proposal_message.model_dump(mode="json"),
            "approval_request": created.model_dump(mode="json"),
        }

    async def _workflow_from_create_request(
            self,
            *,
            profile: MainAgentProfile,
            request: dict[str, Any],
    ) -> WorkflowDefinition:
        workflow_payload = request.get("workflow")
        if isinstance(workflow_payload, dict):
            try:
                return WorkflowDefinition.model_validate(workflow_payload)
            except Exception as exc:
                raise ValueError(f"the workflow payload is invalid: {exc}") from exc

        goal = request.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("either workflow or goal is required")

        try:
            return await WorkflowBuilderService(self.context).build_workflow_definition(
                goal=goal.strip(),
                conversation_history=(
                    request.get("conversation_history")
                    if isinstance(request.get("conversation_history"), str)
                    else None
                ),
                model_profile_id=(
                    request.get("model_profile_id")
                    if isinstance(request.get("model_profile_id"), str)
                    else profile.default_model_profile_id
                ),
                default_agent_model_profile_id=profile.default_model_profile_id,
            )
        except Exception as exc:
            if request.get("plain_text_request") is True:
                return self._fallback_workflow_from_plain_text_request(
                    profile=profile,
                    goal=goal.strip(),
                    builder_error=str(exc),
                )
            raise ValueError(f"workflow builder failed: {exc}") from exc

    def _fallback_workflow_from_plain_text_request(
            self,
            *,
            profile: MainAgentProfile,
            goal: str,
            builder_error: str,
    ) -> WorkflowDefinition:
        workflow_id = f"workflow-{uuid4()}"
        reviewer_agent_id = f"{workflow_id}-agent-repo-reviewer"
        coder_agent_id = f"{workflow_id}-agent-coder"
        task_ids = [
            f"{workflow_id}-task-scan",
            f"{workflow_id}-task-risks",
            f"{workflow_id}-task-report",
            f"{workflow_id}-task-implement",
        ]
        node_ids = [
            f"{workflow_id}-node-scan",
            f"{workflow_id}-node-risks",
            f"{workflow_id}-node-report",
            f"{workflow_id}-node-implement",
        ]
        schedule_metadata = self._schedule_metadata_from_plain_text(goal, workflow_id=workflow_id)
        command_tool = command_system_tool_definitions(can_run_commands=True)[0]
        reviewer_agent = AgentDefinition(
            id=reviewer_agent_id,
            name="Agency Repo Improvement Reviewer",
            description="Reviews the Agency repository and proposes one concrete improvement plus relevant fixes.",
            model_profile_id=profile.default_model_profile_id,
            instructions=(
                "Review the Agency repository with a pragmatic engineering lens. Focus on one new improvement idea, "
                "then identify likely vulnerabilities, reliability risks, or maintenance fixes. Prefer concrete file "
                "paths, testable claims, and small actionable next steps. Escalate if repository access or required "
                "tools are missing."
            ),
        )
        coder_agent = AgentDefinition(
            id=coder_agent_id,
            name="Coder Agent",
            role="Repository coding specialist",
            description="Implements repository TODO items from approved recommendations and verifies outcomes.",
            model_profile_id=profile.default_model_profile_id,
            instructions=(
                "Implement repository TODO items safely, keep diffs small and reviewable, and run focused validation "
                "commands before final output. Prioritize high-impact TODOs first."
            ),
            tool_ids=[SYSTEM_COMMAND_RUN_TOOL_ID],
        )
        tasks = [
            TaskDefinition(
                id=task_ids[0],
                name="Inspect repository signals",
                description="Review recent repository structure, TODOs, tests, runtime scripts, and error-prone areas.",
                instructions=(
                    "Gather enough repository context to ground the daily recommendation. Prefer deterministic checks "
                    "and local evidence over broad speculation."
                ),
                expected_output="A concise list of relevant repo signals with file paths or command evidence.",
                agent_id=reviewer_agent_id,
            ),
            TaskDefinition(
                id=task_ids[1],
                name="Identify improvement and risk",
                description="Choose one high-leverage improvement idea and identify vulnerabilities or fixes worth addressing.",
                instructions=(
                    "Select exactly one new improvement idea for the day. Include any vulnerabilities, correctness bugs, "
                    "operational risks, dependency issues, or missing tests discovered during review."
                ),
                expected_output="One prioritized improvement idea plus supporting vulnerabilities or fixes.",
                agent_id=reviewer_agent_id,
                depends_on_task_ids=[task_ids[0]],
            ),
            TaskDefinition(
                id=task_ids[2],
                name="Prepare daily repo improvement brief",
                description="Produce the final daily brief for the human.",
                instructions=(
                    "Summarize the recommendation, why it matters, where to start, verification steps, and any follow-up "
                    "approval or implementation work needed. Include a concrete TODO list suitable for direct coding."
                ),
                expected_output=(
                    "A short daily brief with one improvement idea, identified vulnerabilities or fixes, evidence, "
                    "recommended next actions, and a concrete TODO list for implementation."
                ),
                agent_id=reviewer_agent_id,
                depends_on_task_ids=[task_ids[1]],
            ),
            TaskDefinition(
                id=task_ids[3],
                name="Implement TODOs from daily brief",
                description="Apply code changes directly from the TODO items produced in the daily brief.",
                instructions=(
                    "Use the output of 'Prepare daily repo improvement brief' as the source of truth. Implement selected "
                    "TODO items in the repository with focused diffs and run targeted verification commands."
                ),
                expected_output=(
                    "Completed TODO items, changed files, diff summary, verification command outputs, and any blockers."
                ),
                agent_id=coder_agent_id,
                tool_ids=[SYSTEM_COMMAND_RUN_TOOL_ID],
                depends_on_task_ids=[task_ids[2]],
            ),
        ]
        nodes = [
            WorkflowNodeDefinition(id=node_ids[index], name=task.name, node_type=NodeType.TASK, task_id=task.id)
            for index, task in enumerate(tasks)
        ]
        edges = [
            WorkflowEdgeDefinition(source_node_id=node_ids[0], target_node_id=node_ids[1]),
            WorkflowEdgeDefinition(source_node_id=node_ids[1], target_node_id=node_ids[2]),
            WorkflowEdgeDefinition(source_node_id=node_ids[2], target_node_id=node_ids[3]),
        ]
        metadata: dict[str, Any] = {
            "visible_to_main_agent": True,
            "mutable_by_main_agent": True,
            "generated_by": "conversation_plain_text_fallback",
            "original_goal": goal,
            "workflow_builder_error": builder_error,
        }
        if schedule_metadata is not None:
            metadata["requested_schedule"] = schedule_metadata
        return WorkflowDefinition(
            id=workflow_id,
            name="Daily Agency Repo Improvement Review",
            description=(
                "Daily workflow that proposes one new Agency repo improvement idea and highlights vulnerabilities, "
                "bugs, or fixes to consider."
            ),
            entrypoint=node_ids[0],
            nodes=nodes,
            edges=edges,
            task_definitions=tasks,
            agent_definitions=[reviewer_agent, coder_agent],
            tool_definitions=[command_tool],
            metadata=metadata,
        )

    def _schedule_metadata_from_plain_text(self, text: str, *, workflow_id: str) -> dict[str, Any] | None:
        lowered = text.lower()
        if not re.search(r"\b(?:daily|every\s+day|everyday|each\s+day)\b", lowered):
            return None
        hour = 7
        minute = 0
        match = re.search(r"\b([01]?\d|2[0-3])(?::([0-5]\d))?\s*(am|pm)\b", lowered)
        if match is None:
            match = re.search(r"\b(?:at|@|by|around)\s+([01]?\d|2[0-3])(?::([0-5]\d))?\b", lowered)
        if match is not None:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            meridiem = match.group(3)
            if meridiem == "pm" and hour < 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
        timezone_name = os.environ.get("TZ") or get_settings().memory_daily_summary_timezone or "UTC"
        return {
            "id": f"{workflow_id}-daily-review",
            "name": "Daily Agency Repo Improvement Review",
            "trigger_type": ScheduleType.CRON.value,
            "trigger_config": {"cron": f"{minute} {hour} * * *"},
            "timezone": timezone_name,
            "input_template": {
                "goal": "Create one new idea to improve the Agency repo and identify vulnerabilities or fixes.",
                "source": "conversation_plain_text_request",
            },
            "max_concurrent_executions": 1,
            "metadata": {"description": f"Run every day at {hour:02d}:{minute:02d} {timezone_name}."},
        }

    async def _workflow_validation_errors(self, workflow: WorkflowDefinition) -> list[str]:
        result = await WorkflowValidationService(self.context).validate(workflow)
        return [
            str(issue.get("message") or issue.get("code") or "validation error")
            for issue in result.validation_errors
        ]

    async def _repair_workflow_until_valid(
            self,
            workflow: WorkflowDefinition,
            *,
            model_profile_id: str | None = None,
            repair_goal: str | None = None,
    ) -> tuple[WorkflowDefinition, dict[str, Any]]:
        initial_errors = await self._workflow_validation_errors(workflow)
        if not initial_errors:
            return workflow, {}

        repaired = self._repair_workflow_definition(workflow)
        remaining_errors = await self._workflow_validation_errors(repaired)
        metadata: dict[str, Any] = {
            "validation_repair": {
                "attempted": True,
                "initial_errors": initial_errors,
                "remaining_errors": remaining_errors,
                "model_assisted": False,
            }
        }

        if remaining_errors:
            try:
                model_repaired = await WorkflowBuilderService(self.context).repair_workflow_definition(
                    workflow=repaired,
                    validation_errors=remaining_errors,
                    goal=repair_goal,
                    model_profile_id=model_profile_id,
                )
                model_repaired = model_repaired.model_copy(update={"id": workflow.id})
                model_remaining_errors = await self._workflow_validation_errors(model_repaired)
                metadata["validation_repair"].update(
                    {
                        "model_assisted": True,
                        "model_assisted_errors": model_remaining_errors,
                    }
                )
                repaired = model_repaired
                remaining_errors = model_remaining_errors
            except Exception as exc:
                metadata["validation_repair"].update(
                    {
                        "model_assisted": True,
                        "model_assisted_error": str(exc),
                    }
                )

        metadata["validation_repair"]["remaining_errors"] = remaining_errors
        if remaining_errors:
            metadata["remaining_errors"] = remaining_errors
        return repaired, metadata

    def _repair_workflow_definition(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        nodes = list(workflow.nodes)
        task_definitions = list(workflow.task_definitions)
        agent_definitions = list(workflow.agent_definitions)

        if task_definitions and agent_definitions:
            default_agent_id = agent_definitions[0].id
            task_definitions = [
                task.model_copy(update={"agent_id": task.agent_id or default_agent_id})
                for task in task_definitions
            ]

        if task_definitions and not nodes:
            nodes = [
                WorkflowNodeDefinition(
                    id=f"{task.id}-node",
                    name=task.name,
                    node_type=NodeType.TASK,
                    task_id=task.id,
                )
                for task in task_definitions
            ]

        task_ids = [task.id for task in task_definitions]
        if nodes and task_ids:
            repaired_nodes = []
            for index, node in enumerate(nodes):
                if node.node_type == NodeType.TASK and not node.task_id:
                    task_id = task_ids[min(index, len(task_ids) - 1)]
                    repaired_nodes.append(node.model_copy(update={"task_id": task_id}))
                else:
                    repaired_nodes.append(node)
            nodes = repaired_nodes

        node_ids = {node.id for node in nodes}
        entrypoint = workflow.entrypoint if workflow.entrypoint in node_ids else (
            nodes[0].id if nodes else workflow.entrypoint)
        return workflow.model_copy(
            update={
                "entrypoint": entrypoint,
                "nodes": nodes,
                "task_definitions": task_definitions,
            }
        )

    def _workflow_create_diff_summary(self, workflow: WorkflowDefinition) -> str:
        return (
            f"Create workflow '{workflow.name}' with "
            f"{len(workflow.agent_definitions)} agent(s), "
            f"{len(workflow.task_definitions)} task(s), and "
            f"{len(workflow.nodes)} node(s)."
        )

    def _workflow_update_diff_summary(self, current: WorkflowDefinition, proposed: WorkflowDefinition) -> str:
        changes: list[str] = []
        if current.name != proposed.name:
            changes.append(f"name: '{current.name}' -> '{proposed.name}'")
        if (current.description or "") != (proposed.description or ""):
            changes.append("description changed")
        if len(current.agent_definitions) != len(proposed.agent_definitions):
            changes.append(
                f"agents: {len(current.agent_definitions)} -> {len(proposed.agent_definitions)}"
            )
        if len(current.task_definitions) != len(proposed.task_definitions):
            changes.append(
                f"tasks: {len(current.task_definitions)} -> {len(proposed.task_definitions)}"
            )
        if len(current.nodes) != len(proposed.nodes):
            changes.append(f"nodes: {len(current.nodes)} -> {len(proposed.nodes)}")
        if current.entrypoint != proposed.entrypoint:
            changes.append(f"entrypoint: '{current.entrypoint}' -> '{proposed.entrypoint}'")

        current_task_descriptions = [task.description for task in current.task_definitions]
        proposed_task_descriptions = [task.description for task in proposed.task_definitions]
        if current_task_descriptions != proposed_task_descriptions:
            changes.append("task descriptions changed")

        current_agent_instructions = [agent.instructions for agent in current.agent_definitions]
        proposed_agent_instructions = [agent.instructions for agent in proposed.agent_definitions]
        if current_agent_instructions != proposed_agent_instructions:
            changes.append("agent instructions changed")

        if not changes:
            return f"Update workflow '{current.name}' with no structural changes detected."
        return f"Update workflow '{current.name}': " + "; ".join(changes[:8]) + "."

    def _tool_create_diff_summary(self, tool: ToolDefinition) -> str:
        return f"Create {tool.tool_type.value} tool '{tool.name}' with id '{tool.id}'."

    def _tool_update_diff_summary(self, current: ToolDefinition, proposed: ToolDefinition) -> str:
        changes: list[str] = []
        if current.name != proposed.name:
            changes.append(f"name: '{current.name}' -> '{proposed.name}'")
        if current.description != proposed.description:
            changes.append("description changed")
        if current.tool_type != proposed.tool_type:
            changes.append(f"type: {current.tool_type.value} -> {proposed.tool_type.value}")
        if current.input_schema != proposed.input_schema:
            changes.append("input schema changed")
        if current.output_schema != proposed.output_schema:
            changes.append("output schema changed")
        if current.implementation.model_dump(mode="json") != proposed.implementation.model_dump(mode="json"):
            changes.append("implementation changed")
        if current.security.model_dump(mode="json") != proposed.security.model_dump(mode="json"):
            changes.append("security settings changed")
        if current.tags != proposed.tags:
            changes.append("tags changed")
        if not changes:
            return f"Update tool '{current.name}' with no structural changes detected."
        return f"Update tool '{current.name}': " + "; ".join(changes[:8]) + "."

    def _tool_with_provenance(
            self,
            tool: ToolDefinition,
            *,
            approval: ApprovalRequest,
            action: str,
            decision: str,
    ) -> ToolDefinition:
        hints = tool.framework_hints
        metadata = dict(hints.metadata)
        metadata["provenance"] = {
            **metadata.get("provenance", {}),
            "source": "conversation",
            "action": action,
            "decision": decision,
            "approval_request_id": approval.id,
            "conversation_id": approval.conversation_id,
            "origin_message_id": approval.origin_message_id,
            "requested_by_agent_id": approval.requested_by_agent_id,
            "requested_by_profile_id": approval.requested_by_profile_id,
            "resolved_by_user_id": approval.approved_by_user_id,
        }
        return tool.model_copy(
            update={
                "framework_hints": hints.model_copy(update={"metadata": metadata}),
            }
        )

    def _reserved_tool_mutation_error(self, tool: ToolDefinition) -> str | None:
        if tool.id.startswith("agency."):
            return "reserved system tool ids cannot be created or updated from chat"
        if tool.implementation.target in {SYSTEM_WORKFLOW_TOOL_TARGET, SYSTEM_TOOL_MANAGEMENT_TARGET}:
            return "reserved system tool implementations cannot be created or updated from chat"
        if "system" in tool.tags:
            return "system-tagged tools cannot be created or updated from chat"
        return None

    async def _create_workflow_update_proposal(
            self,
            *,
            profile: MainAgentProfile,
            conversation_id: str,
            origin_message_id: str,
            request: dict[str, Any],
    ) -> dict[str, Any]:
        mutation_decision = self._policy().check_workflow_mutation_enabled()
        if not mutation_decision.allowed:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=mutation_decision.reason or "Main-agent workflow mutation is disabled by policy.",
                metadata={"profile_id": profile.id, "delivery": "direct", "policy_code": mutation_decision.code},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        channel_decision = await self._policy().check_workflow_mutation_channel(conversation_id)
        if not channel_decision.allowed:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=channel_decision.reason or "This channel is not allowed to create or update workflows without a trusted mapped identity.",
                metadata={"profile_id": profile.id, "delivery": "direct", "policy_code": channel_decision.code},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        workflow_id = request.get("workflow_id")
        if not workflow_id:
            text = (
                "I could not update that workflow because no workflow_id was provided."
                if not request.get("plain_text_request")
                else "I could not determine which workflow to update. Please include the workflow ID."
            )
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=text,
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I could not find workflow '{workflow_id}'.",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        if not self._policy().workflow_is_visible(workflow):
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I cannot update workflow '{workflow.name}' because it is not visible to this agent.",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        if not self._policy().workflow_is_mutable(workflow):
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I cannot update workflow '{workflow.name}' because it is not marked mutable by this agent.",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        try:
            proposed_workflow = await self._workflow_from_update_request(
                current_workflow=workflow,
                profile=profile,
                request=request,
            )
        except ValueError as exc:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I could not create that workflow update proposal because {exc}",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        if proposed_workflow.id != workflow.id:
            proposed_workflow = proposed_workflow.model_copy(update={"id": workflow.id})
        proposed_workflow, repair_metadata = await self._repair_workflow_until_valid(
            proposed_workflow,
            model_profile_id=(
                request.get("model_profile_id")
                if isinstance(request.get("model_profile_id"), str)
                else profile.default_model_profile_id
            ),
            repair_goal=request.get("goal") if isinstance(request.get("goal"), str) else None,
        )
        if proposed_workflow.id != workflow.id:
            proposed_workflow = proposed_workflow.model_copy(update={"id": workflow.id})
        validation_errors = repair_metadata.get("remaining_errors", [])
        if validation_errors:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text="I could not create that workflow update proposal because validation failed: "
                     + "; ".join(validation_errors[:3]),
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        summary = request.get("summary") or f"Update workflow '{workflow.name}'."
        diff_summary = request.get("diff_summary") or self._workflow_update_diff_summary(workflow, proposed_workflow)
        restart_active_executions = bool(request.get("restart_active_executions"))
        approval = ApprovalRequest(
            approval_type=ApprovalType.WORKFLOW_UPDATE,
            target_type=ApprovalTargetType.WORKFLOW,
            target_id=workflow.id,
            requested_by_agent_id=profile.agent_id,
            requested_by_profile_id=profile.id,
            conversation_id=conversation_id,
            origin_message_id=origin_message_id,
            summary=summary,
            diff_summary=diff_summary,
            proposed_payload={
                "workflow_id": workflow.id,
                "current_revision": workflow.versioning.revision,
                "restart_active_executions": restart_active_executions,
                "workflow": proposed_workflow.model_dump(mode="json"),
            },
            metadata={
                "action": "workflow_update",
                "restart_active_executions": restart_active_executions,
                **repair_metadata,
            },
        )
        created = await self.context.conversation_approval_repo.create(approval)
        proposal_message = await self._append_workflow_proposal_message(
            conversation_id=conversation_id,
            profile_id=profile.id,
            approval=created,
            workflow=proposed_workflow,
            message_type=ConversationMessageType.WORKFLOW_UPDATE_PROPOSAL,
        )
        await self.publish_approval_requested(conversation_id, created.model_dump(mode="json"))
        return {
            "assistant_message": proposal_message.model_dump(mode="json"),
            "approval_request": created.model_dump(mode="json"),
        }

    async def _create_tool_create_proposal(
            self,
            *,
            profile: MainAgentProfile,
            conversation_id: str,
            origin_message_id: str,
            request: dict[str, Any],
    ) -> dict[str, Any]:
        mutation_decision = self._policy().check_tool_mutation_enabled()
        if not mutation_decision.allowed:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=mutation_decision.reason or "Main-agent tool mutation is disabled by policy.",
                metadata={"profile_id": profile.id, "delivery": "direct", "policy_code": mutation_decision.code},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        channel_decision = await self._policy().check_tool_mutation_channel(conversation_id)
        if not channel_decision.allowed:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=channel_decision.reason or "This channel is not allowed to create or update tools without a trusted mapped identity.",
                metadata={"profile_id": profile.id, "delivery": "direct", "policy_code": channel_decision.code},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        try:
            proposed_tool = ToolDefinition.model_validate(request.get("tool") or {})
        except Exception as exc:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I could not create that tool proposal because the tool payload is invalid: {exc}",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        existing = await self.context.tool_repo.get(proposed_tool.id, include_deleted=True)
        if existing is not None:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I cannot create tool '{proposed_tool.name}' because that tool id already exists.",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        reserved_error = self._reserved_tool_mutation_error(proposed_tool)
        if reserved_error:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I could not create that tool proposal because {reserved_error}",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        embedded_secret_paths = self._embedded_tool_secret_paths(proposed_tool)
        if embedded_secret_paths:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=(
                        "I could not create that tool proposal because embedded secrets must be stored "
                        "as credential references, not inside tool definitions: "
                        + ", ".join(embedded_secret_paths[:3])
                ),
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        validation = self.context.tool_service.validate_definition(proposed_tool)
        if not validation.valid:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text="I could not create that tool proposal because validation failed: "
                     + "; ".join(
                    str(item.get("message") or item.get("code")) for item in validation.validation_errors[:3]),
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        summary = request.get("summary") or f"Create tool '{proposed_tool.name}'."
        diff_summary = request.get("diff_summary") or self._tool_create_diff_summary(proposed_tool)
        approval = ApprovalRequest(
            approval_type=ApprovalType.TOOL_CREATE,
            target_type=ApprovalTargetType.TOOL,
            target_id=proposed_tool.id,
            requested_by_agent_id=profile.agent_id,
            requested_by_profile_id=profile.id,
            conversation_id=conversation_id,
            origin_message_id=origin_message_id,
            summary=summary,
            diff_summary=diff_summary,
            proposed_payload={"tool": proposed_tool.model_dump(mode="json")},
            metadata={
                "action": "tool_create",
                "validation_warnings": validation.validation_warnings,
            },
        )
        created = await self.context.conversation_approval_repo.create(approval)
        approval_message = await self._append_approval_request_message(
            conversation_id=conversation_id,
            profile_id=profile.id,
            approval=created,
            target={
                "type": created.target_type.value,
                "id": created.target_id,
                "name": proposed_tool.name,
                "tool_type": proposed_tool.tool_type.value,
                "diff_summary": diff_summary,
            },
        )
        await self.publish_approval_requested(conversation_id, created.model_dump(mode="json"))
        return {
            "assistant_message": approval_message.model_dump(mode="json"),
            "approval_request": created.model_dump(mode="json"),
        }

    async def _create_tool_update_proposal(
            self,
            *,
            profile: MainAgentProfile,
            conversation_id: str,
            origin_message_id: str,
            request: dict[str, Any],
    ) -> dict[str, Any]:
        mutation_decision = self._policy().check_tool_mutation_enabled()
        if not mutation_decision.allowed:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=mutation_decision.reason or "Main-agent tool mutation is disabled by policy.",
                metadata={"profile_id": profile.id, "delivery": "direct", "policy_code": mutation_decision.code},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        channel_decision = await self._policy().check_tool_mutation_channel(conversation_id)
        if not channel_decision.allowed:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=channel_decision.reason or "This channel is not allowed to create or update tools without a trusted mapped identity.",
                metadata={"profile_id": profile.id, "delivery": "direct", "policy_code": channel_decision.code},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        tool_id = request.get("tool_id")
        if not isinstance(tool_id, str) or not tool_id.strip():
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text="I could not update that tool because no tool_id was provided.",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        current = await self.context.tool_repo.get(tool_id)
        if current is None:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I could not find tool '{tool_id}'.",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        try:
            proposed_tool = ToolDefinition.model_validate(request.get("tool") or {})
        except Exception as exc:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I could not create that tool update proposal because the tool payload is invalid: {exc}",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        if proposed_tool.id != current.id:
            proposed_tool = proposed_tool.model_copy(update={"id": current.id})
        reserved_error = self._reserved_tool_mutation_error(proposed_tool)
        if reserved_error:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=f"I could not create that tool update proposal because {reserved_error}",
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        embedded_secret_paths = self._embedded_tool_secret_paths(proposed_tool)
        if embedded_secret_paths:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text=(
                        "I could not create that tool update proposal because embedded secrets must be stored "
                        "as credential references, not inside tool definitions: "
                        + ", ".join(embedded_secret_paths[:3])
                ),
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        validation = self.context.tool_service.validate_definition(proposed_tool)
        if not validation.valid:
            assistant_message = await self._append_assistant_text_message(
                conversation_id=conversation_id,
                text="I could not create that tool update proposal because validation failed: "
                     + "; ".join(
                    str(item.get("message") or item.get("code")) for item in validation.validation_errors[:3]),
                metadata={"profile_id": profile.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant_message.model_dump(mode="json")}

        summary = request.get("summary") or f"Update tool '{current.name}'."
        diff_summary = request.get("diff_summary") or self._tool_update_diff_summary(current, proposed_tool)
        approval = ApprovalRequest(
            approval_type=ApprovalType.TOOL_UPDATE,
            target_type=ApprovalTargetType.TOOL,
            target_id=current.id,
            requested_by_agent_id=profile.agent_id,
            requested_by_profile_id=profile.id,
            conversation_id=conversation_id,
            origin_message_id=origin_message_id,
            summary=summary,
            diff_summary=diff_summary,
            proposed_payload={
                "tool_id": current.id,
                "tool": proposed_tool.model_dump(mode="json"),
            },
            metadata={
                "action": "tool_update",
                "validation_warnings": validation.validation_warnings,
            },
        )
        created = await self.context.conversation_approval_repo.create(approval)
        approval_message = await self._append_approval_request_message(
            conversation_id=conversation_id,
            profile_id=profile.id,
            approval=created,
            target={
                "type": created.target_type.value,
                "id": created.target_id,
                "name": proposed_tool.name,
                "tool_type": proposed_tool.tool_type.value,
                "diff_summary": diff_summary,
            },
        )
        await self.publish_approval_requested(conversation_id, created.model_dump(mode="json"))
        return {
            "assistant_message": approval_message.model_dump(mode="json"),
            "approval_request": created.model_dump(mode="json"),
        }

    async def _workflow_from_update_request(
            self,
            *,
            current_workflow: WorkflowDefinition,
            profile: MainAgentProfile,
            request: dict[str, Any],
    ) -> WorkflowDefinition:
        workflow_payload = request.get("workflow")
        if isinstance(workflow_payload, dict):
            try:
                return WorkflowDefinition.model_validate(workflow_payload)
            except Exception as exc:
                raise ValueError(f"the workflow payload is invalid: {exc}") from exc

        goal = request.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("either workflow or goal is required")

        try:
            builder = WorkflowBuilderService(self.context)
            return await builder.update_workflow_definition(
                workflow=current_workflow,
                goal=goal.strip(),
                conversation_history=(
                    request.get("conversation_history")
                    if isinstance(request.get("conversation_history"), str)
                    else None
                ),
                model_profile_id=(
                    request.get("model_profile_id")
                    if isinstance(request.get("model_profile_id"), str)
                    else profile.default_model_profile_id
                ),
            )
        except Exception as exc:
            builder = WorkflowBuilderService(self.context)
            if builder._goal_requests_recommendation_to_code(
                goal=goal.strip(),
                workflow=current_workflow,
            ):
                return builder._ensure_recommendation_to_code_pipeline(
                    workflow=current_workflow,
                    goal=goal.strip(),
                )
            raise ValueError(f"workflow builder failed: {exc}") from exc

    async def _resolve_main_profile(self, conversation_id: str) -> MainAgentProfile:
        conversation = await self.context.conversation_repo.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"Conversation '{conversation_id}' not found")
        setup_service = MainAgentSetupService(self.context)
        profile = await self.context.main_agent_profile_repo.get(
            conversation.main_agent_profile_id) if conversation.main_agent_profile_id else None
        if profile is None:
            profile = await setup_service.require_active_main_agent_profile()
        if conversation.main_agent_profile_id != profile.id:
            await self.context.conversation_repo.update(conversation.id, {"main_agent_profile_id": profile.id})
        return profile

    async def _generate_assistant_reply(self, *, profile: MainAgentProfile, conversation_id: str) -> dict[str, Any]:
        agent = await self.context.agent_repo.get(profile.agent_id)
        model_profile_id = (
            agent.model_profile_id
            if agent is not None and agent.model_profile_id
            else profile.default_model_profile_id
        )
        model_profile = await self.context.model_profile_repo.get(
            model_profile_id) if model_profile_id else None
        history = await self.context.conversation_message_repo.list_by_conversation(conversation_id)
        conversation = await self.context.conversation_repo.get(conversation_id)
        tools = [
            tool
            for tool in await AgentToolResolver(self.context).resolve_agent_tools(agent)
            if self._policy().tool_is_visible(tool)
        ]
        instructions = await self._compose_main_agent_instructions(
            agent=agent,
            profile=profile,
        )
        if conversation is not None:
            latest_user_text = next(
                (item.plain_text for item in reversed(history) if item.role == ConversationRole.USER and item.plain_text),
                None,
            )
            memory_prompt = await self._build_memory_prompt(
                conversation=conversation,
                agent_id=profile.agent_id,
                query=latest_user_text,
            )
            if memory_prompt:
                instructions = f"{instructions or ''}\n\n{memory_prompt}".strip()

        outcome = await self._call_direct_reply_model(
            profile=profile,
            instructions=instructions,
            model_profile=model_profile,
            history=history,
            conversation_id=conversation_id,
            tools=tools,
        )
        if outcome.get("assistant_message") is not None:
            return outcome
        latest_user_message = next(
            (item for item in reversed(history) if item.role == ConversationRole.USER and item.plain_text),
            None,
        )
        if latest_user_message is not None:
            create_request = self._workflow_create_request_from_plain_text(latest_user_message)
            if create_request is not None:
                proposal = await self._create_workflow_create_proposal(
                    profile=profile,
                    conversation_id=conversation_id,
                    origin_message_id=latest_user_message.id,
                    request=create_request,
                )
                if proposal.get("approval_request") is not None:
                    return proposal
        text = outcome.get("text") or self._fallback_reply("How can I help?")
        metadata = {"profile_id": profile.id, "delivery": "direct"}
        if isinstance(outcome.get("metadata"), dict):
            metadata.update(outcome["metadata"])
        assistant = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.ASSISTANT_TEXT,
                plain_text=text,
                content={"text": text},
                metadata=metadata,
            )
        )
        await self.context.conversation_event_broker.publish(
            conversation_id,
            self.serialize_message_event(assistant),
        )
        await self._audit_conversation_event(
            conversation_id=conversation_id,
            event_type=ExecutionEventType.LLM_RESPONSE_CREATED,
            payload={
                "message_id": assistant.id,
                "message_type": assistant.message_type.value,
                "response_kind": "direct_reply",
                "text": text,
                "model_profile_id": model_profile_id,
            },
            metadata={"profile_id": profile.id, "delivery": "direct"},
            agent_id=profile.agent_id,
        )
        return {"assistant_message": assistant.model_dump(mode="json")}

    async def _append_assistant_text_message(
            self,
            *,
            conversation_id: str,
            text: str,
            metadata: dict[str, Any] | None = None,
    ) -> ConversationMessage:
        assistant = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.ASSISTANT_TEXT,
                plain_text=text,
                content={"text": text},
                metadata=metadata or {},
            )
        )
        await self.context.conversation_event_broker.publish(
            conversation_id,
            self.serialize_message_event(assistant),
        )
        await self._audit_conversation_event(
            conversation_id=conversation_id,
            event_type=ExecutionEventType.AGENT_MESSAGE_CREATED,
            payload={
                "message_id": assistant.id,
                "message_type": assistant.message_type.value,
                "text": text,
            },
            metadata=metadata or {},
        )
        return assistant

    async def _append_approval_result_message(self, approval: ApprovalRequest) -> ConversationMessage:
        text = (
            f"Approval {'granted' if approval.status == ApprovalStatus.APPROVED else 'rejected'}: {approval.summary}"
        )
        if approval.decision_reason:
            text = f"{text} ({approval.decision_reason})"
        message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=approval.conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.APPROVAL_RESULT,
                plain_text=text,
                approval_request_id=approval.id,
                content={
                    "approval_request_id": approval.id,
                    "status": approval.status.value,
                    "reason": approval.decision_reason,
                    "summary": approval.summary,
                },
                metadata={"resolved_by": approval.approved_by_user_id},
            )
        )
        await self.context.conversation_event_broker.publish(
            approval.conversation_id,
            self.serialize_message_event(message),
        )
        return message

    async def _append_approval_revision_requested_message(self, approval: ApprovalRequest) -> ConversationMessage:
        text = f"Approval changes requested: {approval.summary}"
        if approval.decision_reason:
            text = f"{text} ({approval.decision_reason})"
        message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=approval.conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.SYSTEM_NOTE,
                plain_text=text,
                approval_request_id=approval.id,
                content={
                    "approval_request_id": approval.id,
                    "status": approval.status.value,
                    "reason": approval.decision_reason,
                    "summary": approval.summary,
                    "revision_requested": True,
                    "revision_request": approval.metadata.get("last_revision_request"),
                },
                metadata={
                    "resolved_by": approval.approved_by_user_id,
                    "approval_event": "revision_requested",
                },
            )
        )
        await self.context.conversation_event_broker.publish(
            approval.conversation_id,
            self.serialize_message_event(message),
        )
        return message

    async def _append_approval_split_message(
            self,
            parent: ApprovalRequest,
            children: list[ApprovalRequest],
    ) -> ConversationMessage:
        text = f"Approval split into {len(children)} separate requests: {parent.summary}"
        message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=parent.conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.SYSTEM_NOTE,
                plain_text=text,
                approval_request_id=parent.id,
                content={
                    "approval_request_id": parent.id,
                    "status": parent.status.value,
                    "summary": parent.summary,
                    "split_requested": True,
                    "child_approval_request_ids": [item.id for item in children],
                },
                metadata={
                    "resolved_by": parent.approved_by_user_id,
                    "approval_event": "split",
                },
            )
        )
        await self.context.conversation_event_broker.publish(
            parent.conversation_id,
            self.serialize_message_event(message),
        )
        return message

    async def _maybe_store_rejected_approval_memory(
            self,
            approval: ApprovalRequest,
            *,
            store_reason_as_memory: bool,
    ) -> dict[str, Any] | None:
        reason = (approval.decision_reason or "").strip()
        if not store_reason_as_memory or approval.status != ApprovalStatus.REJECTED or not reason:
            return None
        if approval.target_type != ApprovalTargetType.WORKFLOW or not approval.target_id:
            return {"status": "skipped", "reason": "approval_not_workflow_scoped"}
        content = (
            f"Human rejected workflow improvement proposal for workflow {approval.target_id}: {reason}"
        )
        try:
            memory = await MemoryService(self.context).create_memory(
                {
                    "scope": "workflow",
                    "workflow_id": approval.target_id,
                    "content": content,
                    "summary": f"Rejected workflow improvement proposal: {reason}",
                    "tags": ["workflow_improvement", "approval_rejection", "main_agent_monitor"],
                    "source": "conversation_approval_rejection",
                    "source_conversation_id": approval.conversation_id,
                    "agent_id": approval.requested_by_agent_id,
                    "memory_kind": "decision",
                    "importance": 60,
                    "metadata": {
                        "approval_request_id": approval.id,
                        "approval_type": approval.approval_type.value,
                        "proposal_kind": approval.metadata.get("proposal_kind"),
                        "monitor_proposal_event_id": approval.metadata.get("monitor_proposal_event_id"),
                    },
                },
                trusted_actor=True,
            )
        except MemoryPolicyError:
            return {"status": "skipped", "reason": "sensitive_memory_requires_confirmation"}
        except ValueError as exc:
            return {"status": "skipped", "reason": str(exc)}
        return {"status": "created", "memory": memory.model_dump(mode="json")}

    def _split_approval_parts(self, approval: ApprovalRequest) -> list[dict[str, Any]]:
        payload = dict(approval.proposed_payload or {})
        metadata = dict(approval.metadata or {})
        parts: list[dict[str, Any]] = []
        if payload.get("workflow") is not None:
            parts.append(
                {
                    "approval_type": approval.approval_type,
                    "summary": f"{approval.summary} (workflow definition)",
                    "diff_summary": approval.diff_summary,
                    "proposed_payload": {
                        key: value
                        for key, value in payload.items()
                        if key not in {
                            "schedule_change_approval",
                            "tool_assignment_change_approval",
                            "memory_write_approval",
                        }
                    },
                    "metadata": {
                        "split_part": "workflow_definition",
                        "action": metadata.get("action"),
                    },
                }
            )
        for key, split_part, approval_type, summary in (
                (
                    "schedule_change_approval",
                    "schedule_change",
                    ApprovalType.OTHER,
                    "schedule, concurrency, runtime adapter, or execution host review",
                ),
                (
                    "tool_assignment_change_approval",
                    "tool_assignment",
                    ApprovalType.OTHER,
                    "tool assignment permission review",
                ),
                (
                    "memory_write_approval",
                    "memory_write",
                    ApprovalType.OTHER,
                    "durable workflow memory write review",
                ),
        ):
            approval_requirements = payload.get(key) or metadata.get(key)
            if approval_requirements is None:
                continue
            parts.append(
                {
                    "approval_type": approval_type,
                    "summary": f"{approval.summary} ({summary})",
                    "diff_summary": summary,
                    "proposed_payload": {
                        "workflow_id": approval.target_id,
                        key: approval_requirements,
                        "parent_proposed_payload": {
                            "current_revision": payload.get("current_revision"),
                            "expected_replacement_revision": payload.get("expected_replacement_revision"),
                            "evidence": payload.get("evidence"),
                        },
                    },
                    "metadata": {
                        "split_part": split_part,
                        "action": f"{split_part}_review",
                    },
                }
            )
        return parts

    async def _append_approval_request_message(
            self,
            *,
            conversation_id: str,
            profile_id: str,
            approval: ApprovalRequest,
            target: dict[str, Any],
    ) -> ConversationMessage:
        message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.APPROVAL_REQUEST,
                plain_text=approval.summary,
                approval_request_id=approval.id,
                content={
                    "approval_request_id": approval.id,
                    "approval_type": approval.approval_type.value,
                    "summary": approval.summary,
                    "status": approval.status.value,
                    "target": target,
                },
                metadata={"profile_id": profile_id},
            )
        )
        await self.context.conversation_event_broker.publish(
            conversation_id,
            self.serialize_message_event(message),
        )
        return message

    async def _append_workflow_proposal_message(
            self,
            *,
            conversation_id: str,
            profile_id: str,
            approval: ApprovalRequest,
            workflow: WorkflowDefinition,
            message_type: ConversationMessageType,
    ) -> ConversationMessage:
        message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=message_type,
                plain_text=approval.summary,
                approval_request_id=approval.id,
                content={
                    "approval_request_id": approval.id,
                    "approval_type": approval.approval_type.value,
                    "summary": approval.summary,
                    "diff_summary": approval.diff_summary,
                    "status": approval.status.value,
                    "workflow": {
                        "id": workflow.id,
                        "name": workflow.name,
                        "version": workflow.versioning.version,
                        "revision": workflow.versioning.revision,
                    },
                    "restart_active_executions": bool(
                        (approval.proposed_payload or {}).get("restart_active_executions")
                        or approval.metadata.get("restart_active_executions")
                    ),
                },
                metadata={"profile_id": profile_id},
            )
        )
        await self.context.conversation_event_broker.publish(
            conversation_id,
            self.serialize_message_event(message),
        )
        return message

    async def _generate_title_if_needed(self, conversation_id: str) -> None:
        conversation = await self.context.conversation_repo.get(conversation_id)
        if conversation is None or conversation.title:
            return
        messages = await self.context.conversation_message_repo.list_by_conversation(conversation_id)
        first_user_text = next((m.plain_text for m in messages if m.role == ConversationRole.USER and m.plain_text),
                               None)
        if not first_user_text:
            return
        title = self._fallback_title(first_user_text)
        await self.context.conversation_repo.update(conversation_id, {"title": title})

    async def _create_tool_execution_approval(
            self,
            *,
            profile: MainAgentProfile,
            conversation_id: str,
            tool: ToolDefinition,
            arguments: dict[str, Any],
            tool_call_id: str,
            origin_message_id: str | None,
    ) -> dict[str, Any]:
        redacted_arguments = self._redact_tool_arguments(tool, arguments)
        approval = ApprovalRequest(
            approval_type=ApprovalType.TOOL_EXECUTE,
            target_type=ApprovalTargetType.TOOL,
            target_id=tool.id,
            requested_by_agent_id=profile.agent_id,
            requested_by_profile_id=profile.id,
            conversation_id=conversation_id,
            origin_message_id=origin_message_id or tool_call_id,
            summary=self._tool_execution_summary(tool, arguments),
            proposed_payload={
                "tool_id": tool.id,
                "tool_name": tool.name,
                "tool_call_id": tool_call_id,
                "arguments": redacted_arguments,
                "redacted_arguments": redacted_arguments,
            },
            metadata={"action": "tool_execution", "delivery": "direct"},
        )
        created = await self.context.conversation_approval_repo.create(approval)
        approval_message = await self._append_approval_request_message(
            conversation_id=conversation_id,
            profile_id=profile.id,
            approval=created,
            target={
                "type": created.target_type.value,
                "id": created.target_id,
                "name": tool.name,
                "arguments": redacted_arguments,
            },
        )
        await self.publish_approval_requested(conversation_id, created.model_dump(mode="json"))
        return {
            "assistant_message": approval_message.model_dump(mode="json"),
            "approval_request": created.model_dump(mode="json"),
        }

    async def _maybe_launch_execution_from_approval(self, approval: ApprovalRequest) -> dict[str, Any] | None:
        if approval.metadata.get("action") != "workflow_execution":
            return None
        if approval.status != ApprovalStatus.APPROVED:
            return None
        if approval.metadata.get("execution_id"):
            execution = await self.context.execution_store.get_execution(approval.metadata["execution_id"])
            if execution is None:
                return None
            return {"execution": execution.model_dump(mode="json")}
        workflow_id = (approval.proposed_payload or {}).get("workflow_id")
        if not workflow_id:
            return None
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' was not found")
        profile = await self._resolve_main_profile(approval.conversation_id)
        launch = await self._launch_execution_from_request(
            workflow=workflow,
            profile=profile,
            conversation_id=approval.conversation_id,
            origin_message_id=approval.origin_message_id,
            input_payload=(approval.proposed_payload or {}).get("input_payload", {}),
            runtime_adapter_id=(approval.proposed_payload or {}).get("runtime_adapter_id"),
        )
        execution_payload = launch.get("execution")
        if execution_payload is not None:
            updated_metadata = dict(approval.metadata)
            updated_metadata["execution_id"] = execution_payload["id"]
            await self.context.conversation_approval_repo.update(approval.id, {"metadata": updated_metadata})
        return launch

    async def _maybe_execute_tool_from_approval(self, approval: ApprovalRequest) -> dict[str, Any] | None:
        if approval.metadata.get("action") != "tool_execution":
            return None
        if approval.status != ApprovalStatus.APPROVED:
            return None
        payload = approval.proposed_payload or {}
        tool_id = payload.get("tool_id")
        if not tool_id:
            return None
        tool = await self.context.tool_repo.get(tool_id)
        if tool is None:
            assistant = await self._append_assistant_text_message(
                conversation_id=approval.conversation_id,
                text=f"I could not execute approved tool '{payload.get('tool_name') or tool_id}' because it is unavailable.",
                metadata={"approval_request_id": approval.id, "delivery": "direct"},
            )
            return {"assistant_message": assistant.model_dump(mode="json")}

        tool_call_id = payload.get("tool_call_id") or f"tool-call-{uuid4()}"
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        await self._append_tool_call_message(
            conversation_id=approval.conversation_id,
            tool=tool,
            tool_call_id=tool_call_id,
            arguments=self._redact_tool_arguments(tool, arguments),
        )
        try:
            result = await self.context.tool_service.tool_registry.execute(
                tool,
                arguments,
                execution_id=f"conversation-tool-{uuid4()}",
                workflow_id=None,
            )
        except Exception as exc:
            result = {"status": "error", "error": str(exc), "tool_name": tool.name}
        display_result = self._redact_tool_value(tool, result)
        tool_result_message = await self._append_tool_result_message(
            conversation_id=approval.conversation_id,
            tool_name=tool.name,
            tool_id=tool.id,
            tool_call_id=tool_call_id,
            result=display_result if isinstance(display_result, dict) else {"result": display_result},
        )

        profile = await self._resolve_main_profile(approval.conversation_id)
        follow_up = await self._generate_assistant_reply(profile=profile, conversation_id=approval.conversation_id)
        return {
            "tool_result": display_result,
            "tool_result_message": tool_result_message.model_dump(mode="json"),
            **follow_up,
        }

    async def _maybe_apply_workflow_mutation_from_approval(self, approval: ApprovalRequest) -> dict[str, Any] | None:
        if approval.status != ApprovalStatus.APPROVED:
            return None
        action = approval.metadata.get("action")
        if action in {"workflow_create", "workflow_update"}:
            mutation_decision = self._policy().check_workflow_mutation_enabled()
            if not mutation_decision.allowed:
                assistant = await self._append_assistant_text_message(
                    conversation_id=approval.conversation_id,
                    text=mutation_decision.reason or "Main-agent workflow mutation is disabled by policy.",
                    metadata={
                        "approval_request_id": approval.id,
                        "delivery": "direct",
                        "policy_code": mutation_decision.code,
                    },
                )
                return {"status": "blocked", "assistant_message": assistant.model_dump(mode="json")}
        if action == "workflow_create":
            proposed = WorkflowDefinition.model_validate((approval.proposed_payload or {}).get("workflow") or {})
            workflow = proposed.model_copy(
                update={
                    "metadata": self._workflow_provenance_metadata(
                        proposed.metadata,
                        approval=approval,
                        action="workflow_create",
                        decision="approved",
                        owner_user_id=approval.approved_by_user_id,
                    )
                }
            )
            saved = await self.context.workflow_repo.save(workflow)
            schedule = await self._maybe_create_requested_schedule_for_workflow(saved)
            await self._audit_conversation_event(
                conversation_id=approval.conversation_id,
                event_type=ExecutionEventType.AGENT_MESSAGE_CREATED,
                payload={
                    "mutation_type": "workflow_create",
                    "decision": "approved",
                    "approval_request_id": approval.id,
                    "workflow_id": saved.id,
                    "workflow_name": saved.name,
                    "schedule_id": schedule.id if schedule is not None else None,
                },
                metadata={"source": "conversation", "audit_kind": "workflow_mutation_applied"},
                actor=approval.approved_by_user_id,
                agent_id=approval.requested_by_agent_id,
            )
            return saved.model_dump(mode="json")
        if action != "workflow_update":
            return None

        current = await self.context.workflow_repo.get(approval.target_id or "")
        if current is None:
            raise WorkflowNotFoundError(f"Workflow '{approval.target_id}' was not found")
        proposed = WorkflowDefinition.model_validate((approval.proposed_payload or {}).get("workflow") or {})
        next_revision = current.versioning.revision + 1
        next_version = proposed.versioning.version or current.versioning.version
        updated = proposed.model_copy(
            update={
                "id": current.id,
                "versioning": proposed.versioning.model_copy(
                    update={
                        "version": next_version,
                        "revision": next_revision,
                        "parent_version": current.versioning.version,
                        "is_published": True,
                        "labels": proposed.versioning.labels,
                    }
                ),
                "metadata": self._workflow_provenance_metadata(
                    proposed.metadata,
                    approval=approval,
                    action="workflow_update",
                    decision="approved",
                    fallback_metadata=current.metadata,
                ),
            }
        )
        saved = await self.context.workflow_repo.save(updated)
        restart_active_executions = bool(
            (approval.proposed_payload or {}).get("restart_active_executions")
            or approval.metadata.get("restart_active_executions")
        )
        await WorkflowService(self.context).maybe_replace_active_executions_for_revision_change(
            before=current,
            after=saved,
            restart_requested=restart_active_executions,
            source="main_agent_workflow_update",
        )
        await self._audit_conversation_event(
            conversation_id=approval.conversation_id,
            event_type=ExecutionEventType.AGENT_MESSAGE_CREATED,
            payload={
                "mutation_type": "workflow_update",
                "decision": "approved",
                "approval_request_id": approval.id,
                "workflow_id": saved.id,
                "workflow_name": saved.name,
                "revision": saved.versioning.revision,
            },
            metadata={"source": "conversation", "audit_kind": "workflow_mutation_applied"},
            actor=approval.approved_by_user_id,
            agent_id=approval.requested_by_agent_id,
        )
        return saved.model_dump(mode="json")

    async def _maybe_apply_tool_mutation_from_approval(self, approval: ApprovalRequest) -> dict[str, Any] | None:
        if approval.status != ApprovalStatus.APPROVED:
            return None
        action = approval.metadata.get("action")
        if action in {"tool_create", "tool_update"}:
            mutation_decision = self._policy().check_tool_mutation_enabled()
            if not mutation_decision.allowed:
                assistant = await self._append_assistant_text_message(
                    conversation_id=approval.conversation_id,
                    text=mutation_decision.reason or "Main-agent tool mutation is disabled by policy.",
                    metadata={
                        "approval_request_id": approval.id,
                        "delivery": "direct",
                        "policy_code": mutation_decision.code,
                    },
                )
                return {"status": "blocked", "assistant_message": assistant.model_dump(mode="json")}
        if action == "tool_create":
            proposed = ToolDefinition.model_validate((approval.proposed_payload or {}).get("tool") or {})
            saved = await self.context.tool_repo.save(
                self._tool_with_provenance(
                    proposed,
                    approval=approval,
                    action="tool_create",
                    decision="approved",
                )
            )
            await self._audit_conversation_event(
                conversation_id=approval.conversation_id,
                event_type=ExecutionEventType.AGENT_MESSAGE_CREATED,
                payload={
                    "mutation_type": "tool_create",
                    "decision": "approved",
                    "approval_request_id": approval.id,
                    "tool_id": saved.id,
                    "tool_name": saved.name,
                    "tool_type": saved.tool_type.value,
                },
                metadata={"source": "conversation", "audit_kind": "tool_mutation_applied"},
                actor=approval.approved_by_user_id,
                agent_id=approval.requested_by_agent_id,
            )
            return saved.model_dump(mode="json")
        if action != "tool_update":
            return None

        current = await self.context.tool_repo.get(approval.target_id or "")
        if current is None:
            raise KeyError(f"Tool '{approval.target_id}' was not found")
        proposed = ToolDefinition.model_validate((approval.proposed_payload or {}).get("tool") or {})
        if proposed.id != current.id:
            proposed = proposed.model_copy(update={"id": current.id})
        saved = await self.context.tool_repo.save(
            self._tool_with_provenance(
                proposed,
                approval=approval,
                action="tool_update",
                decision="approved",
            )
        )
        await self._audit_conversation_event(
            conversation_id=approval.conversation_id,
            event_type=ExecutionEventType.AGENT_MESSAGE_CREATED,
            payload={
                "mutation_type": "tool_update",
                "decision": "approved",
                "approval_request_id": approval.id,
                "tool_id": saved.id,
                "tool_name": saved.name,
                "tool_type": saved.tool_type.value,
            },
            metadata={"source": "conversation", "audit_kind": "tool_mutation_applied"},
            actor=approval.approved_by_user_id,
            agent_id=approval.requested_by_agent_id,
        )
        return saved.model_dump(mode="json")

    async def _maybe_create_requested_schedule_for_workflow(
            self,
            workflow: WorkflowDefinition,
    ) -> ScheduleDefinition | None:
        schedule_payload = workflow.metadata.get("requested_schedule")
        if not isinstance(schedule_payload, dict):
            return None
        try:
            schedule = ScheduleDefinition.model_validate(
                {
                    **schedule_payload,
                    "workflow_id": workflow.id,
                    "enabled": schedule_payload.get("enabled", True),
                }
            )
        except Exception as exc:
            logger.warning("Ignoring invalid requested schedule for workflow %s: %s", workflow.id, exc)
            return None
        existing = await self.context.schedule_repo.get(schedule.id)
        if existing is not None:
            return existing
        try:
            return await self.context.scheduler.create_schedule(schedule)
        except Exception as exc:
            logger.warning("Failed to create requested schedule for workflow %s: %s", workflow.id, exc)
            return None

    async def _maybe_persist_rejected_create_draft(self, approval: ApprovalRequest) -> dict[str, Any] | None:
        if approval.status != ApprovalStatus.REJECTED or approval.metadata.get("action") != "workflow_create":
            return None
        proposed = WorkflowDefinition.model_validate((approval.proposed_payload or {}).get("workflow") or {})
        labels = list(proposed.versioning.labels)
        if "draft" not in labels:
            labels.append("draft")
        workflow = proposed.model_copy(
            update={
                "versioning": proposed.versioning.model_copy(
                    update={
                        "version": proposed.versioning.version,
                        "revision": proposed.versioning.revision,
                        "parent_version": proposed.versioning.parent_version,
                        "is_published": False,
                        "labels": labels,
                    }
                ),
                "metadata": self._workflow_provenance_metadata(
                    proposed.metadata,
                    approval=approval,
                    action="workflow_create",
                    decision="rejected_saved_as_draft",
                    owner_user_id=approval.approved_by_user_id,
                ),
            }
        )
        saved = await self.context.workflow_repo.save(workflow)
        await self._audit_conversation_event(
            conversation_id=approval.conversation_id,
            event_type=ExecutionEventType.AGENT_MESSAGE_CREATED,
            payload={
                "mutation_type": "workflow_create",
                "decision": "rejected_saved_as_draft",
                "approval_request_id": approval.id,
                "workflow_id": saved.id,
                "workflow_name": saved.name,
            },
            metadata={"source": "conversation", "audit_kind": "workflow_mutation_applied"},
            actor=approval.approved_by_user_id,
            agent_id=approval.requested_by_agent_id,
        )
        return saved.model_dump(mode="json")

    async def _launch_execution_from_request(
            self,
            *,
            workflow: WorkflowDefinition,
            profile: MainAgentProfile,
            conversation_id: str,
            origin_message_id: str,
            input_payload: dict[str, Any],
            runtime_adapter_id: str | None,
    ) -> dict[str, Any]:
        trigger = {
            "type": "conversation",
            "created_by": profile.id,
            "conversation_id": conversation_id,
            "origin_message_id": origin_message_id,
            "requested_via_channel": (await self.context.conversation_repo.get(conversation_id)).channel_type.value,
        }
        execution = await self.context.runtime_registry.create_execution(
            workflow.id,
            input_payload,
            trigger,
            runtime_adapter_id=runtime_adapter_id,
        )
        execution.metadata = {
            **execution.metadata,
            "conversation_id": conversation_id,
            "origin_message_id": origin_message_id,
            "requested_by_profile_id": profile.id,
        }
        await self.context.execution_store.update_execution(execution)
        queued = await self.context.control_plane.queue_start(execution.id)
        started_message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.SYSTEM,
                message_type=ConversationMessageType.EXECUTION_STARTED,
                plain_text=f"Started workflow '{workflow.name}'.",
                execution_id=queued.id,
                content={
                    "execution_id": queued.id,
                    "workflow_id": workflow.id,
                    "workflow_name": workflow.name,
                },
                metadata={"profile_id": profile.id},
            )
        )
        await self.context.conversation_event_broker.publish(
            conversation_id,
            self.serialize_message_event(started_message),
        )
        await self._audit_conversation_event(
            conversation_id=conversation_id,
            event_type=ExecutionEventType.EXECUTION_STARTED,
            payload={
                "message_id": started_message.id,
                "execution_id": queued.id,
                "workflow_id": workflow.id,
                "workflow_name": workflow.name,
                "status": queued.status.value,
                "origin_message_id": origin_message_id,
            },
            metadata={"profile_id": profile.id, "source": "conversation"},
            agent_id=profile.agent_id,
        )
        latest = await self._sync_execution_completion_message(
            conversation_id=conversation_id,
            workflow=workflow,
            execution_id=queued.id,
        )
        if latest is None:
            self._ensure_execution_completion_watch(
                conversation_id=conversation_id,
                workflow=workflow,
                execution_id=queued.id,
            )
        response = {
            "assistant_message": started_message.model_dump(mode="json"),
            "execution": queued.model_dump(mode="json"),
        }
        if latest is not None:
            response["execution_result_message"] = latest.model_dump(mode="json")
        return response

    async def _sync_execution_completion_message(
            self,
            *,
            conversation_id: str,
            workflow: WorkflowDefinition,
            execution_id: str,
    ) -> ConversationMessage | None:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None or execution.status.value not in {"completed", "failed", "cancelled"}:
            return None
        existing = await self.context.conversation_message_repo.list_by_conversation(conversation_id)
        if any(item.execution_id == execution_id and item.message_type == ConversationMessageType.EXECUTION_COMPLETED
               for item in existing):
            return None
        summary = f"Workflow '{workflow.name}' {execution.status.value}."
        message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.SYSTEM,
                message_type=ConversationMessageType.EXECUTION_COMPLETED,
                plain_text=summary,
                execution_id=execution.id,
                content={
                    "execution_id": execution.id,
                    "workflow_id": workflow.id,
                    "status": execution.status.value,
                    "summary": summary,
                    "final_output": execution.output_payload,
                },
            )
        )
        await self.context.conversation_event_broker.publish(
            conversation_id,
            self.serialize_message_event(message),
        )
        await self._audit_conversation_event(
            conversation_id=conversation_id,
            event_type=(
                ExecutionEventType.EXECUTION_COMPLETED
                if execution.status.value == "completed"
                else ExecutionEventType.EXECUTION_FAILED
            ),
            payload={
                "message_id": message.id,
                "execution_id": execution.id,
                "workflow_id": workflow.id,
                "workflow_name": workflow.name,
                "status": execution.status.value,
                "final_output": execution.output_payload,
                "error": execution.error,
            },
            metadata={"source": "conversation"},
        )
        return message

    def _ensure_execution_completion_watch(
            self,
            *,
            conversation_id: str,
            workflow: WorkflowDefinition,
            execution_id: str,
    ) -> None:
        existing = self._execution_watch_tasks.get(execution_id)
        if existing is not None and not existing.done():
            return
        self._execution_watch_tasks[execution_id] = asyncio.create_task(
            self._watch_execution_completion(
                conversation_id=conversation_id,
                workflow=workflow,
                execution_id=execution_id,
            )
        )

    async def _watch_execution_completion(
            self,
            *,
            conversation_id: str,
            workflow: WorkflowDefinition,
            execution_id: str,
    ) -> None:
        try:
            while True:
                latest = await self._sync_execution_completion_message(
                    conversation_id=conversation_id,
                    workflow=workflow,
                    execution_id=execution_id,
                )
                if latest is not None:
                    return
                execution = await self.context.execution_store.get_execution(execution_id)
                if execution is None:
                    return
                if execution.status.value in {"completed", "failed", "cancelled"}:
                    return
                await asyncio.sleep(0.1)
        finally:
            self._execution_watch_tasks.pop(execution_id, None)

    async def _call_direct_reply_model(
            self,
            *,
            profile: MainAgentProfile,
            instructions: str | None,
            model_profile: ModelProfileDefinition | None,
            history: list[ConversationMessage],
            conversation_id: str,
            tools: list[ToolDefinition],
    ) -> dict[str, Any]:
        if model_profile is not None:
            try:
                client = self.context.llm_provider_registry.resolve(model_profile)
                auth_failure = await self._preflight_direct_reply_model_auth(
                    model_profile=model_profile,
                    client=client,
                )
                if auth_failure is not None:
                    return auth_failure
                messages = self._build_model_messages(instructions=instructions, history=history)
                tool_payload = self._build_direct_reply_tool_payload(tools)
                latest_user_message_id = next(
                    (item.id for item in reversed(history) if item.role == ConversationRole.USER),
                    None,
                )
                for _ in range(4):
                    if hasattr(client, "agenerate_text"):
                        response = await client.agenerate_text(
                            messages,
                            temperature=model_profile.temperature,
                            max_tokens=model_profile.max_tokens,
                            tools=tool_payload or None,
                        )
                    else:
                        response = await asyncio.to_thread(
                            client.generate_text,
                            messages,
                            temperature=model_profile.temperature,
                            max_tokens=model_profile.max_tokens,
                            tools=tool_payload or None,
                        )
                    if response.content:
                        messages.append(ModelMessage(role="assistant", content=response.content))
                    if response.tool_calls:
                        for tool_call in response.tool_calls:
                            tool = next((item for item in tools if tool_matches_call_name(item, tool_call.name)), None)
                            response_tool_call_name = tool_call.name
                            call_id = tool_call.id or f"tool-call-{uuid4()}"
                            if tool is None:
                                unavailable_result = {
                                    "status": "error",
                                    "error": f"Tool '{tool_call.name}' is not available to this conversation.",
                                }
                                await self._append_tool_result_message(
                                    conversation_id=conversation_id,
                                    tool_name=tool_call.name,
                                    tool_id=None,
                                    tool_call_id=call_id,
                                    result=unavailable_result,
                                )
                                messages.append(
                                    ModelMessage(
                                        role="tool",
                                        content=_json_dump(unavailable_result),
                                        name=response_tool_call_name,
                                        tool_call_id=call_id,
                                    )
                                )
                                continue
                            response_tool_call_name = tool_call_name(tool)
                            if tool.security.requires_approval:
                                channel_decision = await self._policy().check_tool_execution_channel(conversation_id)
                                if not channel_decision.allowed:
                                    return {
                                        "text": channel_decision.reason
                                                or "This channel is not allowed to run approval-gated tools without a trusted mapped identity."
                                    }
                                approval_payload = await self._create_tool_execution_approval(
                                    profile=profile,
                                    conversation_id=conversation_id,
                                    tool=tool,
                                    arguments=tool_call.arguments,
                                    tool_call_id=call_id,
                                    origin_message_id=latest_user_message_id,
                                )
                                return approval_payload
                            if is_system_memory_tool(tool):
                                await self._append_tool_call_message(
                                    conversation_id=conversation_id,
                                    tool=tool,
                                    tool_call_id=call_id,
                                    arguments=self._redact_tool_arguments(tool, tool_call.arguments),
                                )
                                internal_result = await self._execute_conversation_memory_tool(
                                    profile=profile,
                                    conversation_id=conversation_id,
                                    tool=tool,
                                    arguments=tool_call.arguments,
                                )
                                result = internal_result.get("result", internal_result)
                                display_result = self._redact_tool_value(tool, result)
                                await self._append_tool_result_message(
                                    conversation_id=conversation_id,
                                    tool_name=tool.name,
                                    tool_id=tool.id,
                                    tool_call_id=call_id,
                                    result=display_result if isinstance(display_result, dict) else {
                                        "result": display_result},
                                )
                                messages.append(
                                    ModelMessage(
                                        role="tool",
                                        content=_json_dump(display_result),
                                        name=response_tool_call_name,
                                        tool_call_id=call_id,
                                    )
                                )
                                continue
                            if is_system_tool_management_tool(tool):
                                await self._append_tool_call_message(
                                    conversation_id=conversation_id,
                                    tool=tool,
                                    tool_call_id=call_id,
                                    arguments=self._redact_tool_arguments(tool, tool_call.arguments),
                                )
                                internal_result = await self._execute_conversation_tool_management_tool(
                                    profile=profile,
                                    conversation_id=conversation_id,
                                    tool=tool,
                                    arguments=tool_call.arguments,
                                    origin_message_id=latest_user_message_id or call_id,
                                )
                                if internal_result.get("approval_payload") is not None:
                                    return internal_result["approval_payload"]
                                result = internal_result.get("result", internal_result)
                                display_result = self._redact_tool_value(tool, result)
                                await self._append_tool_result_message(
                                    conversation_id=conversation_id,
                                    tool_name=tool.name,
                                    tool_id=tool.id,
                                    tool_call_id=call_id,
                                    result=display_result if isinstance(display_result, dict) else {
                                        "result": display_result},
                                )
                                messages.append(
                                    ModelMessage(
                                        role="tool",
                                        content=_json_dump(display_result),
                                        name=response_tool_call_name,
                                        tool_call_id=call_id,
                                    )
                                )
                                continue
                            if is_system_workflow_tool(tool):
                                await self._append_tool_call_message(
                                    conversation_id=conversation_id,
                                    tool=tool,
                                    tool_call_id=call_id,
                                    arguments=self._redact_tool_arguments(tool, tool_call.arguments),
                                )
                                internal_result = await self._execute_conversation_workflow_tool(
                                    profile=profile,
                                    conversation_id=conversation_id,
                                    tool=tool,
                                    arguments=tool_call.arguments,
                                    origin_message_id=latest_user_message_id or call_id,
                                )
                                if internal_result.get("approval_payload") is not None:
                                    return internal_result["approval_payload"]
                                result = internal_result.get("result", internal_result)
                                display_result = self._redact_tool_value(tool, result)
                                await self._append_tool_result_message(
                                    conversation_id=conversation_id,
                                    tool_name=tool.name,
                                    tool_id=tool.id,
                                    tool_call_id=call_id,
                                    result=display_result if isinstance(display_result, dict) else {
                                        "result": display_result},
                                )
                                messages.append(
                                    ModelMessage(
                                        role="tool",
                                        content=_json_dump(display_result),
                                        name=response_tool_call_name,
                                        tool_call_id=call_id,
                                    )
                                )
                                continue
                            await self._append_tool_call_message(
                                conversation_id=conversation_id,
                                tool=tool,
                                tool_call_id=call_id,
                                arguments=self._redact_tool_arguments(tool, tool_call.arguments),
                            )
                            try:
                                result = await self.context.tool_service.tool_registry.execute(
                                    tool,
                                    tool_call.arguments,
                                    execution_id=f"conversation-tool-{uuid4()}",
                                    workflow_id=None,
                                )
                            except Exception as exc:
                                result = {
                                    "status": "error",
                                    "error": str(exc),
                                    "tool_name": tool.name,
                                }
                            display_result = self._redact_tool_value(tool, result)
                            await self._append_tool_result_message(
                                conversation_id=conversation_id,
                                tool_name=tool.name,
                                tool_id=tool.id,
                                tool_call_id=call_id,
                                result=display_result if isinstance(display_result, dict) else {
                                    "result": display_result},
                            )
                            messages.append(
                                ModelMessage(
                                    role="tool",
                                    content=_json_dump(display_result),
                                    name=response_tool_call_name,
                                    tool_call_id=call_id,
                                )
                            )
                        continue
                    content = response.content
                    if isinstance(content, str) and content.strip():
                        return {"text": content.strip()}
            except Exception as exc:
                logger.exception(
                    "Direct main-agent reply model call failed for conversation %s with model profile %s",
                    conversation_id,
                    model_profile.id,
                )
                return {
                    "text": (
                        "I could not reach the configured LLM for this main agent. "
                        f"Model profile '{model_profile.id}' failed with: {exc}"
                    )
                }
        latest_user = next(
            (item.plain_text for item in reversed(history) if item.role == ConversationRole.USER and item.plain_text),
            None)
        return {"text": self._fallback_reply(latest_user or "How can I help?")}

    async def _preflight_direct_reply_model_auth(
            self,
            *,
            model_profile: ModelProfileDefinition,
            client: Any,
    ) -> dict[str, Any] | None:
        provider_key = str(getattr(client, "provider_key", model_profile.provider)).strip().lower().replace("-", "_")
        if provider_key != "openai_codex" or not hasattr(client, "health_check"):
            return None
        try:
            if hasattr(client, "ahealth_check"):
                health = await client.ahealth_check()
            else:
                health = await asyncio.to_thread(client.health_check)
        except Exception as exc:
            return {
                "text": (
                    "The configured Codex model could not be checked before replying. "
                    f"Model profile '{model_profile.id}' failed auth preflight with: {exc}"
                ),
                "metadata": {
                    "model_auth": {
                        "provider": model_profile.provider,
                        "profile_id": model_profile.id,
                        "auth_required": True,
                        "reauthorization_required": True,
                        "auth_status": "auth_check_failed",
                        "error": str(exc),
                    }
                },
            }
        if not isinstance(health, dict):
            return None
        if health.get("ok") is True:
            return None
        auth_required = bool(health.get("auth_required") or health.get("reauthorization_required"))
        if not auth_required:
            return None
        auth_status = str(health.get("auth_status") or "authorization_required")
        auth_endpoint = health.get("auth_endpoint")
        action = health.get("auth_action") or "reauthorize"
        endpoint_text = f" Use {auth_endpoint} to start re-authorization." if auth_endpoint else ""
        return {
            "text": (
                "The configured Codex model requires authorization before I can reply. "
                f"Auth status: {auth_status}. Action: {action}.{endpoint_text}"
            ),
            "metadata": {
                "model_auth": {
                    key: health.get(key)
                    for key in (
                        "provider",
                        "model",
                        "status_code",
                        "error_code",
                        "auth_status",
                        "auth_required",
                        "reauthorization_required",
                        "auth_mode",
                        "auth_action",
                        "auth_endpoint",
                        "auth_profile_id",
                        "provider_id",
                        "raw_error",
                    )
                    if key in health
                }
            },
        }

    async def _build_memory_prompt(
            self,
            *,
            conversation: Conversation,
            agent_id: str,
            query: str | None,
    ) -> str:
        memory_service = self._memory()
        if not get_settings().memory_retrieval_v2_enabled:
            memories = await memory_service.retrieve_for_conversation(
                conversation=conversation,
                query=query,
                agent_id=agent_id,
            )
            return memory_service.format_for_prompt(memories)

        current_user = await self._resolve_memory_read_user_for_conversation(conversation)
        try:
            operational_context = await memory_service.retrieve_operational_context(
                conversation=conversation,
                agent_id=agent_id,
                query=query,
                current_user=current_user,
            )
            memory_prompt = memory_service.format_operational_context_for_prompt(operational_context)
            if memory_prompt:
                await self._audit_conversation_event(
                    conversation_id=conversation.id,
                    event_type=ExecutionEventType.LLM_REQUEST_CREATED,
                    payload={
                        "memory_retrieval_version": "v2",
                        "memory_layer_counts": {
                            key: len(value)
                            for key, value in operational_context.items()
                        },
                        "memory_total_injected": sum(len(value) for value in operational_context.values()),
                        "memory_sensitive_excluded": 0,
                        "memory_prompt_chars": len(memory_prompt),
                    },
                    metadata={"source": "conversation_memory"},
                    agent_id=agent_id,
                )
            return memory_prompt
        except Exception:
            memories = await memory_service.retrieve_for_conversation(
                conversation=conversation,
                query=query,
                agent_id=agent_id,
            )
            return memory_service.format_for_prompt(memories)

    async def _resolve_memory_read_user_for_conversation(self, conversation: Conversation) -> UserDefinition | None:
        memory_service = self._memory()
        user_id = await memory_service._memory_user_id(conversation)
        if not user_id:
            return None
        if hasattr(self.context.user_repo, "get"):
            existing = await self.context.user_repo.get(user_id)
            if existing is not None:
                return existing
        return UserDefinition(id=user_id, email=f"{user_id}@memory.local")

    async def _execute_conversation_workflow_tool(
            self,
            *,
            profile: MainAgentProfile,
            conversation_id: str,
            tool: ToolDefinition,
            arguments: dict[str, Any],
            origin_message_id: str,
    ) -> dict[str, Any]:
        if tool.id == SYSTEM_WORKFLOW_LIST_TOOL_ID:
            return {"result": await self._list_visible_workflows_for_tool()}
        if tool.id == SYSTEM_WORKFLOW_GET_TOOL_ID:
            return {"result": await self._get_workflow_for_tool(arguments)}
        if tool.id == SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID:
            proposal = await self._create_workflow_create_proposal(
                profile=profile,
                conversation_id=conversation_id,
                origin_message_id=origin_message_id,
                request=arguments,
            )
            if proposal.get("approval_request") is not None:
                return {"approval_payload": proposal}
            return {
                "result": {
                    "status": "error",
                    "error": proposal.get("assistant_message", {}).get(
                        "plain_text") or "Workflow create proposal failed.",
                }
            }
        if tool.id == SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID:
            proposal = await self._create_workflow_update_proposal(
                profile=profile,
                conversation_id=conversation_id,
                origin_message_id=origin_message_id,
                request=arguments,
            )
            if proposal.get("approval_request") is not None:
                return {"approval_payload": proposal}
            return {
                "result": {
                    "status": "error",
                    "error": proposal.get("assistant_message", {}).get(
                        "plain_text") or "Workflow update proposal failed.",
                }
            }
        if tool.id != SYSTEM_WORKFLOW_RUN_TOOL_ID:
            return {"result": {"status": "error", "error": f"Unknown workflow tool '{tool.name}'."}}
        workflow_id = arguments.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            return {"result": {"status": "error", "error": "workflow_id is required."}}
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return {"result": {"status": "error", "error": f"Workflow '{workflow_id}' was not found."}}
        visibility_decision = self._policy().check_workflow_visibility(workflow)
        if not visibility_decision.allowed:
            return {
                "result": {
                    "status": "error",
                    "error": visibility_decision.reason or f"Workflow '{workflow.name}' is not visible to this agent.",
                }
            }
        channel_decision = await self._policy().check_workflow_execution_channel(conversation_id)
        if not channel_decision.allowed:
            return {
                "result": {
                    "status": "blocked",
                    "error": channel_decision.reason
                             or "This channel is not allowed to launch workflows without a trusted mapped identity.",
                }
            }
        if self._policy().workflow_requires_execution_approval(workflow):
            approval = ApprovalRequest(
                approval_type=ApprovalType.WORKFLOW_EXECUTION,
                target_type=ApprovalTargetType.WORKFLOW,
                target_id=workflow.id,
                requested_by_agent_id=profile.agent_id,
                requested_by_profile_id=profile.id,
                conversation_id=conversation_id,
                origin_message_id=origin_message_id,
                summary=f"Run protected workflow '{workflow.name}'.",
                proposed_payload={
                    "workflow_id": workflow.id,
                    "input_payload": arguments.get("input_payload", {}),
                    "runtime_adapter_id": arguments.get("runtime_adapter_id"),
                },
                metadata={"action": "workflow_execution", "source_tool": tool.name},
            )
            created = await self.context.conversation_approval_repo.create(approval)
            approval_message = await self._append_approval_request_message(
                conversation_id=conversation_id,
                profile_id=profile.id,
                approval=created,
                target={
                    "type": created.target_type.value,
                    "id": created.target_id,
                    "name": workflow.name,
                },
            )
            await self.publish_approval_requested(conversation_id, created.model_dump(mode="json"))
            return {
                "approval_payload": {
                    "assistant_message": approval_message.model_dump(mode="json"),
                    "approval_request": created.model_dump(mode="json"),
                }
            }

        launch = await self._launch_execution_from_request(
            workflow=workflow,
            profile=profile,
            conversation_id=conversation_id,
            origin_message_id=origin_message_id,
            input_payload=arguments.get("input_payload", {}) if isinstance(arguments.get("input_payload", {}),
                                                                           dict) else {},
            runtime_adapter_id=arguments.get("runtime_adapter_id") if isinstance(arguments.get("runtime_adapter_id"),
                                                                                 str) else None,
        )
        execution = launch.get("execution", {})
        return {
            "result": {
                "status": "started",
                "workflow_id": workflow.id,
                "workflow_name": workflow.name,
                "execution_id": execution.get("id"),
                "execution_status": execution.get("status"),
            }
        }

    async def _execute_conversation_memory_tool(
            self,
            *,
            profile: MainAgentProfile,
            conversation_id: str,
            tool: ToolDefinition,
            arguments: dict[str, Any],
    ) -> dict[str, Any]:
        conversation = await self.context.conversation_repo.get(conversation_id)
        if conversation is None:
            return {"result": {"status": "error", "error": f"Conversation '{conversation_id}' was not found."}}
        memory_service = self._memory()
        user_id = await memory_service._memory_user_id(conversation)
        trust_context = {
            "channel_type": conversation.channel_type.value,
            "trusted_channel_identity": self._policy().is_trusted_conversation(conversation),
            "workspace_id": conversation.workspace_id,
        }
        if tool.id == SYSTEM_MEMORY_LIST_TOOL_ID:
            items = await memory_service.retrieve_for_conversation(
                conversation=conversation,
                query=arguments.get("query") if isinstance(arguments.get("query"), str) else None,
                agent_id=profile.agent_id,
                limit=int(arguments.get("limit") or 20),
            )
            scope = arguments.get("scope")
            if isinstance(scope, str) and scope.strip():
                items = [item for item in items if item.scope.value == scope]
            return {
                "result": {
                    "status": "ok",
                    "memories": [item.model_dump(mode="json") for item in items],
                }
            }
        if tool.id == SYSTEM_MEMORY_REMEMBER_TOOL_ID:
            scope = arguments.get("scope")
            if not isinstance(scope, str) or not scope.strip():
                return {"result": {"status": "error", "error": "scope is required."}}
            payload = {
                "scope": scope,
                "content": arguments.get("content"),
                "summary": arguments.get("summary"),
                "tags": arguments.get("tags") if isinstance(arguments.get("tags"), list) else [],
                "sensitive": arguments.get("sensitive"),
                "created_by_user_id": user_id,
                "workspace_id": arguments.get("workspace_id") or conversation.workspace_id,
                "conversation_id": arguments.get("conversation_id") or conversation.id,
                "workflow_id": arguments.get("workflow_id"),
                "agent_id": profile.agent_id,
                "source": "main_agent_tool",
                "metadata": {"trust_context": trust_context},
            }
            if scope == "user" and not user_id:
                return {
                    "result": {
                        "status": "blocked",
                        "error": "User-scoped memory requires an authenticated user or trusted external identity mapping.",
                    }
                }
            if scope == "workspace" and not payload["workspace_id"]:
                return {"result": {"status": "error", "error": "Workspace-scoped memory requires workspace_id."}}
            try:
                created = await memory_service.create_memory(
                    payload,
                    confirmed=bool(arguments.get("confirmed")),
                    trusted_actor=self._policy().is_trusted_conversation(conversation),
                )
            except MemoryPolicyError as exc:
                return {"result": {"status": "needs_confirmation", "error": str(exc)}}
            except ValueError as exc:
                return {"result": {"status": "error", "error": str(exc)}}
            return {"result": {"status": "ok", "memory": created.model_dump(mode="json")}}
        if tool.id == SYSTEM_MEMORY_UPDATE_TOOL_ID:
            memory_id = arguments.get("memory_id")
            if not isinstance(memory_id, str) or not memory_id.strip():
                return {"result": {"status": "error", "error": "memory_id is required."}}
            patch = {
                key: value
                for key, value in {
                    "content": arguments.get("content"),
                    "summary": arguments.get("summary"),
                    "tags": arguments.get("tags"),
                    "sensitive": arguments.get("sensitive"),
                    "metadata": {"trust_context": trust_context},
                }.items()
                if value is not None
            }
            try:
                updated = await memory_service.update_memory(
                    memory_id,
                    patch,
                    confirmed=bool(arguments.get("confirmed")),
                    trusted_actor=self._policy().is_trusted_conversation(conversation),
                )
            except MemoryPolicyError as exc:
                return {"result": {"status": "needs_confirmation", "error": str(exc)}}
            except ValueError as exc:
                return {"result": {"status": "error", "error": str(exc)}}
            if updated is None:
                return {"result": {"status": "error", "error": f"Memory '{memory_id}' was not found."}}
            return {"result": {"status": "ok", "memory": updated.model_dump(mode="json")}}
        if tool.id == SYSTEM_MEMORY_DELETE_TOOL_ID:
            memory_id = arguments.get("memory_id")
            if not isinstance(memory_id, str) or not memory_id.strip():
                return {"result": {"status": "error", "error": "memory_id is required."}}
            deleted = await memory_service.delete_memory(
                memory_id,
                trusted_actor=self._policy().is_trusted_conversation(conversation),
            )
            return {"result": {"status": "ok" if deleted else "error", "deleted": deleted, "memory_id": memory_id}}
        return {"result": {"status": "error", "error": f"Unknown memory tool '{tool.name}'."}}

    async def _execute_conversation_tool_management_tool(
            self,
            *,
            profile: MainAgentProfile,
            conversation_id: str,
            tool: ToolDefinition,
            arguments: dict[str, Any],
            origin_message_id: str,
    ) -> dict[str, Any]:
        if tool.id == SYSTEM_TOOL_LIST_TOOL_ID:
            return {"result": await self._list_tools_for_tool()}
        if tool.id == SYSTEM_TOOL_GET_TOOL_ID:
            return {"result": await self._get_tool_for_tool(arguments)}
        if tool.id == SYSTEM_TOOL_PROPOSE_CREATE_TOOL_ID:
            proposal = await self._create_tool_create_proposal(
                profile=profile,
                conversation_id=conversation_id,
                origin_message_id=origin_message_id,
                request=arguments,
            )
            if proposal.get("approval_request") is not None:
                return {"approval_payload": proposal}
            return {
                "result": {
                    "status": "error",
                    "error": proposal.get("assistant_message", {}).get("plain_text") or "Tool create proposal failed.",
                }
            }
        if tool.id == SYSTEM_TOOL_PROPOSE_UPDATE_TOOL_ID:
            proposal = await self._create_tool_update_proposal(
                profile=profile,
                conversation_id=conversation_id,
                origin_message_id=origin_message_id,
                request=arguments,
            )
            if proposal.get("approval_request") is not None:
                return {"approval_payload": proposal}
            return {
                "result": {
                    "status": "error",
                    "error": proposal.get("assistant_message", {}).get("plain_text") or "Tool update proposal failed.",
                }
            }
        return {"result": {"status": "error", "error": f"Unknown tool-management tool '{tool.name}'."}}

    async def _get_tool_for_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tool_id = arguments.get("tool_id")
        if not isinstance(tool_id, str) or not tool_id.strip():
            return {"status": "error", "error": "tool_id is required."}
        tool = await self.context.tool_repo.get(tool_id)
        if tool is None:
            return {"status": "error", "error": f"Tool '{tool_id}' was not found."}
        if not self._policy().tool_is_visible(tool):
            return {"status": "error", "error": f"Tool '{tool.name}' is not visible to this agent."}
        return {"status": "ok", "tool": tool.model_dump(mode="json")}

    async def _list_tools_for_tool(self) -> dict[str, Any]:
        tools = await self.context.tool_repo.list()
        visible_tools = [tool for tool in tools if self._policy().tool_is_visible(tool)]
        return {
            "status": "ok",
            "tools": [
                {
                    "id": item.id,
                    "name": item.name,
                    "description": item.description,
                    "tool_type": item.tool_type.value,
                    "requires_approval": item.security.requires_approval,
                    "read_only": item.security.read_only,
                    "tags": item.tags,
                }
                for item in visible_tools
            ],
        }

    async def _get_workflow_for_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        workflow_id = arguments.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            return {"status": "error", "error": "workflow_id is required."}
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return {"status": "error", "error": f"Workflow '{workflow_id}' was not found."}
        visibility_decision = self._policy().check_workflow_visibility(workflow)
        if not visibility_decision.allowed:
            return {
                "status": "error",
                "error": visibility_decision.reason or f"Workflow '{workflow.name}' is not visible to this agent.",
            }
        return {
            "status": "ok",
            "workflow": workflow.model_dump(mode="json"),
            "summary": self._workflow_tool_summary(workflow),
        }

    async def _list_visible_workflows_for_tool(self) -> dict[str, Any]:
        workflows = await self.context.workflow_repo.list()
        visible = [workflow for workflow in workflows if self._policy().workflow_is_visible(workflow)]
        return {
            "status": "ok",
            "workflows": [self._workflow_tool_summary(workflow) for workflow in visible],
        }

    def _workflow_tool_summary(self, workflow: WorkflowDefinition) -> dict[str, Any]:
        return {
            "id": workflow.id,
            "name": workflow.name,
            "description": workflow.description,
            "input_keys": self._workflow_input_keys(workflow),
            "protected_execution": self._policy().workflow_requires_execution_approval(workflow),
            "mutable_by_agent": self._policy().workflow_is_mutable(workflow),
            "monitoring": self._policy().workflow_monitoring_summary(workflow),
        }

    def _workflow_input_keys(self, workflow: WorkflowDefinition) -> list[str]:
        metadata_inputs = workflow.metadata.get("inputs")
        if isinstance(metadata_inputs, list):
            return sorted({item for item in metadata_inputs if isinstance(item, str) and item.strip()})
        keys: set[str] = set()
        for task in workflow.task_definitions:
            properties = task.input_schema.get("properties") if isinstance(task.input_schema, dict) else None
            if isinstance(properties, dict):
                keys.update(str(key) for key in properties.keys())
        return sorted(keys)

    def _build_direct_reply_tool_payload(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for tool in tools:
            payload.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_call_name(tool),
                        "description": tool.description,
                        "parameters": tool.input_schema or {"type": "object"},
                    },
                }
            )
        return payload

    async def _append_tool_call_message(
            self,
            *,
            conversation_id: str,
            tool: ToolDefinition,
            tool_call_id: str,
            arguments: dict[str, Any],
    ) -> ConversationMessage:
        message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.TOOL,
                message_type=ConversationMessageType.TOOL_CALL,
            plain_text=f"Tool call: {tool_display_name(tool)}",
                tool_call_id=tool_call_id,
                content={
                    "tool_id": tool.id,
                    "tool_name": tool.name,
                    "tool_call_name": tool_call_name(tool),
                    "arguments": arguments,
                },
                metadata={"delivery": "direct"},
            )
        )
        await self.context.conversation_event_broker.publish(conversation_id, self.serialize_message_event(message))
        await self._audit_conversation_event(
            conversation_id=conversation_id,
            event_type=ExecutionEventType.TOOL_CALL_STARTED,
            payload={
                "message_id": message.id,
                "tool_id": tool.id,
                "tool_name": tool.name,
                "arguments": arguments,
            },
            metadata={"delivery": "direct"},
            tool_call_id=tool_call_id,
        )
        return message

    async def _append_tool_result_message(
            self,
            *,
            conversation_id: str,
            tool_name: str,
            tool_id: str | None,
            tool_call_id: str,
            result: dict[str, Any],
    ) -> ConversationMessage:
        plain_text = result.get("status") if isinstance(result, dict) else None
        if not plain_text:
            plain_text = f"Tool result: {tool_name}"
        message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.TOOL,
                message_type=ConversationMessageType.TOOL_RESULT,
                plain_text=str(plain_text),
                tool_call_id=tool_call_id,
                content={
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "tool_call_name": make_tool_call_name(tool_name),
                    "result": result,
                },
                metadata={"delivery": "direct"},
            )
        )
        await self.context.conversation_event_broker.publish(conversation_id, self.serialize_message_event(message))
        result_status = str(result.get("status") or "").lower() if isinstance(result, dict) else ""
        await self._audit_conversation_event(
            conversation_id=conversation_id,
            event_type=(
                ExecutionEventType.TOOL_CALL_FAILED
                if result_status == "error"
                else ExecutionEventType.TOOL_CALL_COMPLETED
            ),
            payload={
                "message_id": message.id,
                "tool_id": tool_id,
                "tool_name": tool_name,
                "result": result,
            },
            metadata={"delivery": "direct"},
            tool_call_id=tool_call_id,
            metrics={"tool_success": result_status != "error"},
        )
        return message

    async def _compose_main_agent_instructions(
            self,
            *,
            agent: Any,
            profile: MainAgentProfile,
    ) -> str | None:
        base = agent.instructions if agent is not None else None
        planning_contract = (
            "\n\nWorkflow Planning Contract:\n"
            "Use assigned workflow tools instead of inventing out-of-band workflow state.\n"
            "For a new workflow request, call propose_workflow_create. If you only have a natural-language request, "
            "pass goal and optional conversation_history; the backend workflow builder will create a canonical WorkflowDefinition.\n"
            "For an update request, call get_workflow when you need current details, then call propose_workflow_update. "
            "If you only have a natural-language edit request, pass goal and optional conversation_history; the backend will draft the complete updated WorkflowDefinition.\n"
            "For tool management, use list_tools/get_tool for inspection, then propose_tool_create or propose_tool_update with a complete ToolDefinition. "
            "Never claim a tool was created or updated until the human approves the proposal.\n"
            "Do not claim a workflow was created or updated until the human approves the proposal.\n"
            "For durable memory, use RememberMemory only when the user explicitly asks you to remember something or confirms it. "
            "Use list_memories/update_memory/delete_memory when the user asks what is remembered, corrects a memory, or asks you to forget something. "
            "Do not store secrets or sensitive facts unless confirmed=true after explicit user confirmation.\n"
        )
        base = f"{base or ''}{planning_contract}".strip()
        if not profile.policy.get("enable_computer_use", True):
            return base
        tools = await self.context.tool_repo.list()
        computer_use_tools = [
            tool
            for tool in tools
            if "computer_use" in getattr(tool, "tags", [])
               and tool.tool_type.value == "mcp_tool"
        ]
        if not computer_use_tools:
            return base
        canonical_names = sorted(
            {
                str(tool.framework_hints.metadata.get("canonical_tool_name", tool.name))
                for tool in computer_use_tools
            }
        )
        contract = (
            "\n\nComputer Use Contract:\n"
            "You may reference these normalized cross-platform computer-use capabilities when reasoning about desktop actions: "
            f"{', '.join(canonical_names)}.\n"
            "Use Agency-normalized names and concepts only. Do not rely on upstream platform-specific MCP tool names.\n"
            "For desktop-state inspection, prefer snapshot or screenshot before proposing mutating actions.\n"
            "Treat click, type, press_key, move, scroll, app, shell, and similar desktop mutations as potentially approval-gated.\n"
        )
        return f"{base or ''}{contract}".strip()

    async def _assert_may_resolve_approval(self, approval: ApprovalRequest, *, actor_user_id: str) -> None:
        conversation = await self.context.conversation_repo.get(approval.conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"Conversation '{approval.conversation_id}' not found")
        owner = conversation.created_by_user_id
        if owner is not None and owner != actor_user_id:
            raise ConversationApprovalPermissionError(
                f"User '{actor_user_id}' may not resolve approval request '{approval.id}'"
            )

    def _build_model_messages(
            self,
            *,
            instructions: str | None,
            history: list[ConversationMessage],
    ) -> list[ModelMessage]:
        messages: list[ModelMessage] = []
        if instructions:
            messages.append(ModelMessage(role="system", content=instructions))
        for item in history:
            if item.message_type == ConversationMessageType.TOOL_RESULT:
                tool_name = item.content.get("tool_name") if isinstance(item.content, dict) else None
                tool_call_name_value = item.content.get("tool_call_name") if isinstance(item.content, dict) else None
                tool_result = item.content.get("result") if isinstance(item.content, dict) else None
                if tool_name and tool_result is not None:
                    messages.append(
                        ModelMessage(
                            role="tool",
                            content=_json_dump(tool_result),
                            name=(
                                tool_call_name_value
                                if isinstance(tool_call_name_value, str) and tool_call_name_value.strip()
                                else make_tool_call_name(str(tool_name))
                            ),
                            tool_call_id=item.tool_call_id,
                        )
                    )
                continue
            if item.role not in {ConversationRole.USER, ConversationRole.ASSISTANT, ConversationRole.SYSTEM}:
                continue
            if item.plain_text is None:
                continue
            if item.role == ConversationRole.ASSISTANT and self._is_synthetic_direct_reply(item.plain_text):
                continue
            messages.append(ModelMessage(role=item.role.value, content=item.plain_text))
        return messages

    def _is_synthetic_direct_reply(self, text: str) -> bool:
        cleaned = " ".join(text.strip().split())
        return cleaned.startswith(
            "I could not reach the configured LLM for this main agent."
        ) or cleaned.startswith("I received your message:")

    def _execution_request_payload(self, origin_message: ConversationMessage) -> dict[str, Any] | None:
        if not isinstance(origin_message.content, dict):
            return None
        request = origin_message.content.get("execution_request")
        return request if isinstance(request, dict) else None

    def _workflow_create_proposal_payload(self, origin_message: ConversationMessage) -> dict[str, Any] | None:
        if not isinstance(origin_message.content, dict):
            return None
        request = origin_message.content.get("workflow_proposal")
        return request if isinstance(request, dict) else None

    def _workflow_update_proposal_payload(self, origin_message: ConversationMessage) -> dict[str, Any] | None:
        if not isinstance(origin_message.content, dict):
            return None
        request = origin_message.content.get("workflow_update_proposal")
        return request if isinstance(request, dict) else None

    def _is_workflow_visible_to_main_agent(self, workflow: WorkflowDefinition) -> bool:
        return self._policy().workflow_is_visible(workflow)

    def _is_workflow_visible_to_agent(self, workflow: WorkflowDefinition) -> bool:
        return self._policy().workflow_is_visible(workflow)

    def _workflow_requires_protected_approval(self, workflow: WorkflowDefinition) -> bool:
        return self._policy().workflow_requires_execution_approval(workflow)

    def _is_workflow_mutable_by_main_agent(self, workflow: WorkflowDefinition) -> bool:
        return self._policy().workflow_is_mutable(workflow)

    async def _is_trusted_for_workflow_execution(self, conversation_id: str) -> bool:
        conversation = await self.context.conversation_repo.get(conversation_id)
        return self._policy().is_trusted_conversation(conversation)

    async def _is_trusted_for_workflow_mutation(self, conversation_id: str) -> bool:
        return await self._is_trusted_for_workflow_execution(conversation_id)

    async def _is_trusted_for_tool_execution(self, conversation_id: str) -> bool:
        return await self._is_trusted_for_workflow_execution(conversation_id)

    async def _is_trusted_for_tool_mutation(self, conversation_id: str) -> bool:
        return await self._is_trusted_for_workflow_execution(conversation_id)

    def _tool_execution_summary(self, tool: ToolDefinition, arguments: dict[str, Any]) -> str:
        if tool.implementation.config.get("tool_family") == "computer_use":
            canonical_name = tool.implementation.config.get("canonical_tool_name") or tool.name
            app_name = arguments.get("name") or arguments.get("bundle_id") or arguments.get("window_title")
            if app_name:
                return f"Allow computer-use action '{canonical_name}' targeting '{app_name}'."
            if canonical_name == "click" and arguments.get("x") is not None and arguments.get("y") is not None:
                return f"Allow computer-use action 'click' at ({arguments['x']}, {arguments['y']})."
            return f"Allow computer-use action '{canonical_name}'."
        return f"Allow tool '{tool.name}' to run."

    def _redact_tool_arguments(self, tool: ToolDefinition, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return {}
        redacted = self._redact_tool_value(tool, arguments)
        return redacted if isinstance(redacted, dict) else {}

    def _redact_tool_value(self, tool: ToolDefinition, value: Any) -> Any:
        rules = self._tool_redaction_rules(tool)

        def redact(item: Any, *, key: str | None = None) -> Any:
            if isinstance(item, dict):
                return {
                    nested_key: redact(nested_value, key=str(nested_key).lower())
                    for nested_key, nested_value in item.items()
                }
            if isinstance(item, list):
                return [redact(entry, key=key) for entry in item]
            if key and any(rule in key for rule in rules):
                return "[REDACTED]"
            return item

        return redact(value)

    def _tool_redaction_rules(self, tool: ToolDefinition) -> set[str]:
        rules = {
            "api_key",
            "apikey",
            "authorization",
            "bearer",
            "client_secret",
            "credential",
            "password",
            "secret",
            "token",
        }
        rules.update(item.lower() for item in tool.security.redaction_rules)
        if tool.implementation.config.get("tool_family") == "computer_use":
            rules.update({"password", "token", "secret"})
        return rules

    def _embedded_tool_secret_paths(self, tool: ToolDefinition) -> list[str]:
        secret_paths: list[str] = []
        rules = self._tool_redaction_rules(tool)

        def visit(value: Any, *, path: str) -> None:
            if isinstance(value, dict):
                for key, nested_value in value.items():
                    key_text = str(key).lower()
                    nested_path = f"{path}.{key}" if path else str(key)
                    if any(rule in key_text for rule in rules) and nested_value not in {None, "", "[REDACTED]"}:
                        secret_paths.append(nested_path)
                        continue
                    visit(nested_value, path=nested_path)
                return
            if isinstance(value, list):
                for index, nested_value in enumerate(value):
                    visit(nested_value, path=f"{path}[{index}]")

        visit(tool.implementation.config, path="implementation.config")
        return secret_paths

    def _workflow_provenance_metadata(
            self,
            metadata: dict[str, Any],
            *,
            approval: ApprovalRequest,
            action: str,
            decision: str,
            owner_user_id: str | None = None,
            fallback_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = {
            **(fallback_metadata or {}),
            **metadata,
        }
        existing_owner_ids = merged.get("owner_ids")
        owner_ids = (
            [item for item in existing_owner_ids if isinstance(item, str) and item]
            if isinstance(existing_owner_ids, list)
            else []
        )
        resolved_owner_user_id = (
            owner_user_id
            or (merged.get("created_by") if isinstance(merged.get("created_by"), str) and merged.get("created_by") else None)
            or approval.approved_by_user_id
        )
        if resolved_owner_user_id:
            merged["created_by"] = resolved_owner_user_id
            merged["owner_ids"] = list(dict.fromkeys([*owner_ids, resolved_owner_user_id]))
        return {
            **merged,
            "provenance": {
                "action": action,
                "decision": decision,
                "conversation_id": approval.conversation_id,
                "origin_message_id": approval.origin_message_id,
                "approval_request_id": approval.id,
                "requested_by_profile_id": approval.requested_by_profile_id,
                "resolved_by_user_id": approval.approved_by_user_id,
            },
        }

    def _fallback_reply(self, user_text: str) -> str:
        cleaned = " ".join(user_text.strip().split())
        return f"I received your message: {cleaned}"

    def _fallback_title(self, first_user_text: str) -> str:
        cleaned = " ".join(first_user_text.strip().split())
        if len(cleaned) <= 60:
            return cleaned
        shortened = cleaned[:57].rstrip()
        if " " in shortened:
            shortened = shortened.rsplit(" ", 1)[0]
        return shortened + "..."


class ConversationNotFoundError(Exception):
    pass


class ConversationApprovalNotFoundError(Exception):
    pass


class ConversationApprovalStateError(Exception):
    pass


class ConversationApprovalPermissionError(Exception):
    pass


def _json_dump(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"), default=str)
