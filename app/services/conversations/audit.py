from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.api.context import ApiContext
from app.core.time import utc_now
from app.domain import Execution, ExecutionEvent, ExecutionEventType, ExecutionStatus
from app.observability import get_default_event_bus


CONVERSATION_AUDIT_WORKFLOW_ID = "conversation-main-agent"
CONVERSATION_AUDIT_RUNTIME_ADAPTER_ID = "conversation"


@dataclass(slots=True)
class ConversationAuditService:
    context: ApiContext

    async def emit(
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
    ) -> ExecutionEvent:
        execution_id = self.audit_execution_id(conversation_id)
        await self._ensure_execution(conversation_id=conversation_id, execution_id=execution_id)
        existing_events = await self.context.execution_store.list_events(execution_id)
        sequence = len(existing_events) + 1
        parent_event_id = existing_events[-1].id if existing_events else None
        event = ExecutionEvent(
            execution_id=execution_id,
            workflow_id=CONVERSATION_AUDIT_WORKFLOW_ID,
            agent_id=agent_id,
            tool_call_id=tool_call_id,
            model_request_id=model_request_id,
            parent_event_id=parent_event_id,
            trace_id=f"conversation:{conversation_id}",
            event_type=event_type,
            sequence=sequence,
            actor=actor,
            payload={"conversation_id": conversation_id, **(payload or {})},
            metrics=metrics or {},
            metadata={"category": "conversation", **(metadata or {})},
        )
        prepared = get_default_event_bus().publish(event)
        saved = await self.context.execution_store.save_event(prepared)
        self.context.runtime_operations.record_action(
            "conversation.audit_event",
            conversation_id=conversation_id,
            event_type=event_type.value,
            execution_id=execution_id,
        )
        return saved

    def audit_execution_id(self, conversation_id: str) -> str:
        return f"conversation-audit-{conversation_id}"

    async def _ensure_execution(self, *, conversation_id: str, execution_id: str) -> None:
        existing = await self.context.execution_store.get_execution(execution_id)
        if existing is not None:
            return
        conversation = await self.context.conversation_repo.get(conversation_id)
        await self.context.execution_store.save_execution(
            Execution(
                id=execution_id,
                workflow_id=CONVERSATION_AUDIT_WORKFLOW_ID,
                runtime_adapter_id=CONVERSATION_AUDIT_RUNTIME_ADAPTER_ID,
                status=ExecutionStatus.RUNNING,
                trigger_type="conversation",
                trigger_payload={"conversation_id": conversation_id},
                input_payload={},
                metadata={
                    "mode": "conversation_audit",
                    "audit_conversation_id": conversation_id,
                    "main_agent_profile_id": conversation.main_agent_profile_id if conversation else None,
                    "channel_type": conversation.channel_type.value if conversation else None,
                    "agent_ids": [],
                },
                started_at=utc_now(),
                created_by=conversation.created_by_user_id if conversation else None,
            )
        )


__all__ = [
    "CONVERSATION_AUDIT_RUNTIME_ADAPTER_ID",
    "CONVERSATION_AUDIT_WORKFLOW_ID",
    "ConversationAuditService",
]
