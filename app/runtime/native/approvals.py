"""Human approval coordination for native runtime tool calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

from app.domain import ExecutionStatus


@dataclass(slots=True)
class ApprovalDecision:
    """Result delivered back to a paused tool call after a human decision."""

    granted: bool
    reason: str | None = None
    metadata: Dict[str, Any] | None = None


class ApprovalManager:
    """Track pending approval futures and mirror their state into the execution store."""

    DelegateDecisionProvider = Callable[..., Awaitable[ApprovalDecision | None]]

    def __init__(self, execution_store=None, delegate_decision_provider: DelegateDecisionProvider | None = None):
        self.execution_store = execution_store
        self.delegate_decision_provider = delegate_decision_provider
        self._pending: Dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._request_ids: Dict[str, str] = {}

    def _key(self, execution_id: str, tool_id: str) -> str:
        return f"{execution_id}:{tool_id}"

    async def request_approval(
            self,
            *,
            execution_id: str,
            tool_id: str,
            payload: Dict[str, Any],
            event_id: str | None = None,
            approval_metadata: Dict[str, Any] | None = None,
    ) -> ApprovalDecision:
        key = self._key(execution_id, tool_id)
        future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        request_payload = {
            "arguments": payload,
            "approval_metadata": approval_metadata or {},
        }
        if self.execution_store is not None and hasattr(self.execution_store, "create_approval_request"):
            request_id = await self.execution_store.create_approval_request(
                execution_id=execution_id,
                event_id=event_id,
                tool_id=tool_id,
                status="pending",
                payload=request_payload,
            )
            self._request_ids[key] = request_id
        if self.execution_store is not None:
            execution = await self.execution_store.get_execution(execution_id)
            if execution is not None:
                execution.status = ExecutionStatus.WAITING_FOR_APPROVAL
                execution.metadata = {
                    **execution.metadata,
                    "pending_approval": {
                        "tool_id": tool_id,
                        "payload": payload,
                        "approval_metadata": approval_metadata or {},
                    },
                }
                await self.execution_store.update_execution(execution)
        delegated_decision = await self._request_delegated_decision(
            execution_id=execution_id,
            tool_id=tool_id,
            payload=payload,
            approval_metadata=approval_metadata or {},
        )
        if delegated_decision is not None:
            await self._resolve(
                execution_id=execution_id,
                tool_id=tool_id,
                decision=delegated_decision,
                responded_by="main_agent",
            )
        try:
            decision = await future
            if self.execution_store is not None:
                execution = await self.execution_store.get_execution(execution_id)
                if execution is not None and execution.status == ExecutionStatus.WAITING_FOR_APPROVAL:
                    execution.status = ExecutionStatus.RUNNING
                    execution.metadata = {**execution.metadata}
                    execution.metadata.pop("pending_approval", None)
                    await self.execution_store.update_execution(execution)
            return decision
        finally:
            self._pending.pop(key, None)

    async def _request_delegated_decision(
            self,
            *,
            execution_id: str,
            tool_id: str,
            payload: Dict[str, Any],
            approval_metadata: Dict[str, Any],
    ) -> ApprovalDecision | None:
        if self.delegate_decision_provider is None:
            return None
        try:
            return await self.delegate_decision_provider(
                execution_id=execution_id,
                tool_id=tool_id,
                payload=payload,
                approval_metadata=approval_metadata,
            )
        except Exception:
            return None

    async def _resolve(
            self,
            *,
            execution_id: str,
            tool_id: str,
            decision: ApprovalDecision,
            responded_by: str,
    ) -> bool:
        key = self._key(execution_id, tool_id)
        future = self._pending.get(key)
        if future is None or future.done():
            return False
        request_id = self._request_ids.pop(key, None)
        if request_id and self.execution_store is not None and hasattr(self.execution_store, "update_approval_request"):
            await self.execution_store.update_approval_request(
                request_id,
                status="approved" if decision.granted else "rejected",
                response_payload={
                    "reason": decision.reason,
                    "granted": decision.granted,
                    "metadata": decision.metadata or {},
                },
                responded_by=responded_by,
            )
        future.set_result(decision)
        return True

    async def approve(self, *, execution_id: str, tool_id: str, reason: str | None = None) -> bool:
        return await self._resolve(
            execution_id=execution_id,
            tool_id=tool_id,
            decision=ApprovalDecision(granted=True, reason=reason, metadata={"mode": "manual"}),
            responded_by="manual",
        )

    async def reject(self, *, execution_id: str, tool_id: str, reason: str | None = None) -> bool:
        return await self._resolve(
            execution_id=execution_id,
            tool_id=tool_id,
            decision=ApprovalDecision(granted=False, reason=reason or "Rejected", metadata={"mode": "manual"}),
            responded_by="manual",
        )
