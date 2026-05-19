from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict

from app.domain import ExecutionStatus


@dataclass(slots=True)
class ApprovalDecision:
    granted: bool
    reason: str | None = None
    metadata: Dict[str, Any] | None = None


class ApprovalManager:
    def __init__(self, execution_store=None):
        self.execution_store = execution_store
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
    ) -> ApprovalDecision:
        key = self._key(execution_id, tool_id)
        future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        if self.execution_store is not None and hasattr(self.execution_store, "create_approval_request"):
            request_id = await self.execution_store.create_approval_request(
                execution_id=execution_id,
                event_id=event_id,
                tool_id=tool_id,
                status="pending",
                payload=payload,
            )
            self._request_ids[key] = request_id
        if self.execution_store is not None:
            execution = await self.execution_store.get_execution(execution_id)
            if execution is not None:
                execution.status = ExecutionStatus.WAITING_FOR_APPROVAL
                execution.metadata = {
                    **execution.metadata,
                    "pending_approval": {"tool_id": tool_id, "payload": payload},
                }
                await self.execution_store.update_execution(execution)
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

    async def approve(self, *, execution_id: str, tool_id: str, reason: str | None = None) -> bool:
        future = self._pending.get(self._key(execution_id, tool_id))
        if future is None or future.done():
            return False
        request_id = self._request_ids.pop(self._key(execution_id, tool_id), None)
        if request_id and self.execution_store is not None and hasattr(self.execution_store, "update_approval_request"):
            await self.execution_store.update_approval_request(
                request_id,
                status="approved",
                response_payload={"reason": reason, "granted": True},
                responded_by="manual",
            )
        future.set_result(ApprovalDecision(granted=True, reason=reason, metadata={"mode": "manual"}))
        return True

    async def reject(self, *, execution_id: str, tool_id: str, reason: str | None = None) -> bool:
        future = self._pending.get(self._key(execution_id, tool_id))
        if future is None or future.done():
            return False
        request_id = self._request_ids.pop(self._key(execution_id, tool_id), None)
        if request_id and self.execution_store is not None and hasattr(self.execution_store, "update_approval_request"):
            await self.execution_store.update_approval_request(
                request_id,
                status="rejected",
                response_payload={"reason": reason or "Rejected", "granted": False},
                responded_by="manual",
            )
        future.set_result(ApprovalDecision(granted=False, reason=reason or "Rejected", metadata={"mode": "manual"}))
        return True
