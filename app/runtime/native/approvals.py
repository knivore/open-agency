"""Human approval coordination for native runtime tool calls."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

from app.domain import ExecutionStatus, ExecutionWait, ExecutionWaitKind, ExecutionWaitStatus
from app.runtime.native.errors import ExecutionApprovalSuspendedError


@dataclass(slots=True)
class ApprovalDecision:
    """Result delivered back to a paused tool call after a human decision."""

    granted: bool
    reason: str | None = None
    metadata: Dict[str, Any] | None = None


class ApprovalManager:
    """Coordinate durable approvals, with live futures only as a legacy fallback."""

    DelegateDecisionProvider = Callable[..., Awaitable[ApprovalDecision | None]]

    def __init__(
            self,
            execution_store=None,
            delegate_decision_provider: DelegateDecisionProvider | None = None,
            poll_interval_seconds: float = 0.1,
    ):
        self.execution_store = execution_store
        self.delegate_decision_provider = delegate_decision_provider
        self.poll_interval_seconds = max(0.01, poll_interval_seconds)
        self._pending: Dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._request_ids: Dict[str, str] = {}

    def _key(self, execution_id: str, tool_id: str) -> str:
        return f"{execution_id}:{tool_id}"

    @staticmethod
    def _invocation_digest(payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def request_approval(
            self,
            *,
            execution_id: str,
            tool_id: str,
            payload: Dict[str, Any],
            redacted_payload: Dict[str, Any] | None = None,
            event_id: str | None = None,
            approval_metadata: Dict[str, Any] | None = None,
    ) -> ApprovalDecision:
        persisted_decision = await self._consume_resolved_decision(
            execution_id=execution_id,
            tool_id=tool_id,
            invocation_digest=self._invocation_digest(payload),
        )
        if persisted_decision is not None:
            return persisted_decision

        key = self._key(execution_id, tool_id)
        future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            persisted_payload = redacted_payload if redacted_payload is not None else payload
            request_payload = {
                "arguments": persisted_payload,
                "approval_metadata": approval_metadata or {},
                "invocation_digest": self._invocation_digest(payload),
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
            wait_id = await self._create_durable_approval_wait(
                execution_id=execution_id,
                tool_id=tool_id,
                request_id=self._request_ids.get(key),
                request_payload=request_payload,
            )
            if self.execution_store is not None:
                execution = await self.execution_store.get_execution(execution_id)
                if execution is not None:
                    execution.status = ExecutionStatus.WAITING_FOR_APPROVAL
                    execution.metadata = {
                        **execution.metadata,
                        "pending_approval": {
                            "request_id": self._request_ids.get(key),
                            "wait_id": wait_id,
                            "tool_id": tool_id,
                            "invocation_digest": request_payload["invocation_digest"],
                            "payload": persisted_payload,
                            "approval_metadata": approval_metadata or {},
                        },
                    }
                    if wait_id:
                        execution.metadata["active_wait"] = {
                            "wait_id": wait_id,
                            "kind": ExecutionWaitKind.APPROVAL.value,
                        }
                    await self.execution_store.update_execution(execution)
            delegated_decision = await self._request_delegated_decision(
                execution_id=execution_id,
                tool_id=tool_id,
                payload=persisted_payload,
                approval_metadata=approval_metadata or {},
            )
            if delegated_decision is not None:
                await self._resolve(
                    execution_id=execution_id,
                    tool_id=tool_id,
                    decision=delegated_decision,
                    responded_by="main_agent",
                )
                await self._finish_inline_decision(execution_id)
                return delegated_decision
            if wait_id:
                # The request, wait, and continuation are durable. Unwind the
                # coroutine so no local worker or isolated container is held
                # while an operator decides.
                raise ExecutionApprovalSuspendedError(
                    f"Execution '{execution_id}' is waiting for approval of tool '{tool_id}'."
                )
        except Exception:
            # A failed persistence/setup step must not leave a process-local
            # phantom waiter that a later API call could accidentally resolve.
            self._pending.pop(key, None)
            self._request_ids.pop(key, None)
            raise
        try:
            # API traffic and execution work may run in different processes. Polling the durable
            # row lets the owning worker resume even when another process records the decision.
            decision = await self._await_decision(future, self._request_ids.get(key))
            if self.execution_store is not None:
                execution = await self.execution_store.get_execution(execution_id)
                if execution is not None and execution.status == ExecutionStatus.WAITING_FOR_APPROVAL:
                    execution.status = ExecutionStatus.RUNNING
                    execution.metadata = {**execution.metadata}
                    execution.metadata.pop("pending_approval", None)
                    execution.metadata.pop("active_wait", None)
                    await self.execution_store.update_execution(execution)
            return decision
        finally:
            self._pending.pop(key, None)
            self._request_ids.pop(key, None)

    async def _consume_resolved_decision(
            self,
            *,
            execution_id: str,
            tool_id: str,
            invocation_digest: str,
    ) -> ApprovalDecision | None:
        if self.execution_store is None or not hasattr(self.execution_store, "get_execution"):
            return None
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            return None
        metadata = dict(execution.metadata or {})
        pending = metadata.get("pending_approval")
        if not isinstance(pending, dict) or pending.get("tool_id") != tool_id:
            return None
        if not hmac.compare_digest(str(pending.get("invocation_digest") or ""), invocation_digest):
            # A resumed/replanned call must obtain a fresh approval when its
            # arguments differ from the invocation the operator reviewed.
            return None
        request_id = pending.get("request_id")
        if not isinstance(request_id, str) or not hasattr(self.execution_store, "get_approval_request"):
            return None
        decision = self._persisted_decision(await self.execution_store.get_approval_request(request_id))
        if decision is None:
            return None
        metadata.pop("pending_approval", None)
        execution.metadata = metadata
        await self.execution_store.update_execution(execution)
        return decision

    async def _finish_inline_decision(self, execution_id: str) -> None:
        """Clear a delegated approval wait without scheduling a second worker."""
        if self.execution_store is None or not hasattr(self.execution_store, "get_execution"):
            return
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            return
        metadata = dict(execution.metadata or {})
        metadata.pop("pending_approval", None)
        metadata.pop("active_wait", None)
        execution.metadata = metadata
        if execution.status == ExecutionStatus.WAITING_FOR_APPROVAL:
            execution.status = ExecutionStatus.RUNNING
        await self.execution_store.update_execution(execution)

    async def _create_durable_approval_wait(
            self,
            *,
            execution_id: str,
            tool_id: str,
            request_id: str | None,
            request_payload: dict[str, Any],
    ) -> str | None:
        if self.execution_store is None or not hasattr(self.execution_store, "create_execution_wait"):
            return None
        execution = (
            await self.execution_store.get_execution(execution_id)
            if hasattr(self.execution_store, "get_execution")
            else None
        )
        wait = ExecutionWait(
            execution_id=execution_id,
            kind=ExecutionWaitKind.APPROVAL,
            idempotency_key=f"approval:{request_id or f'{execution_id}:{tool_id}'}"[:255],
            correlation_key=f"approval:{tool_id}",
            request_payload=request_payload,
            checkpoint=(
                deepcopy(execution.output_payload)
                if execution is not None and isinstance(execution.output_payload, dict)
                else {}
            ),
            metadata={"approval_request_id": request_id, "tool_id": tool_id, "source": "approval_manager"},
        )
        saved = await self.execution_store.create_execution_wait(wait)
        return saved.id

    async def _await_decision(
            self,
            future: asyncio.Future[ApprovalDecision],
            request_id: str | None,
    ) -> ApprovalDecision:
        if request_id is None or self.execution_store is None or not hasattr(
                self.execution_store,
                "get_approval_request",
        ):
            return await future
        while not future.done():
            done, _ = await asyncio.wait({future}, timeout=self.poll_interval_seconds)
            if done:
                break
            persisted = await self.execution_store.get_approval_request(request_id)
            decision = self._persisted_decision(persisted)
            if decision is not None and not future.done():
                future.set_result(decision)
        return await future

    @staticmethod
    def _persisted_decision(request: dict[str, Any] | None) -> ApprovalDecision | None:
        if not isinstance(request, dict):
            return None
        status = request.get("status")
        if status not in {"approved", "rejected", "expired", "cancelled"}:
            return None
        response = request.get("response_payload")
        response_payload = response if isinstance(response, dict) else {}
        metadata = response_payload.get("metadata")
        return ApprovalDecision(
            granted=status == "approved" and response_payload.get("granted", True) is not False,
            reason=(
                response_payload.get("reason")
                if isinstance(response_payload.get("reason"), str)
                else f"Approval request became {status}."
            ),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

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
        status = "approved" if decision.granted else "rejected"
        response_payload = {
            "reason": decision.reason,
            "granted": decision.granted,
            "metadata": decision.metadata or {},
        }
        persisted = None
        durable_resolution_supported = self.execution_store is not None and hasattr(
                self.execution_store,
                "resolve_pending_approval_request",
        )
        if durable_resolution_supported:
            store = self.execution_store
            assert store is not None
            persisted = await store.resolve_pending_approval_request(
                execution_id=execution_id,
                tool_id=tool_id,
                status=status,
                response_payload=response_payload,
                responded_by=responded_by,
            )
        else:
            request_id = self._request_ids.get(key)
            if request_id and self.execution_store is not None and hasattr(
                    self.execution_store,
                    "update_approval_request",
            ):
                await self.execution_store.update_approval_request(
                    request_id,
                    status=status,
                    response_payload=response_payload,
                    responded_by=responded_by,
                )
                persisted = {"id": request_id, "status": status}
        # A terminal durable row wins over an in-process waiter so concurrent
        # approve/reject requests cannot deliver conflicting decisions.
        if durable_resolution_supported and persisted is None:
            return False
        if persisted is not None:
            await self._resolve_durable_approval_wait(
                execution_id=execution_id,
                approval_request_id=str(persisted.get("id") or ""),
                status=status,
                response_payload=response_payload,
                responded_by=responded_by,
            )
        if future is not None and not future.done():
            future.set_result(decision)
        return persisted is not None or future is not None

    async def _resolve_durable_approval_wait(
            self,
            *,
            execution_id: str,
            approval_request_id: str,
            status: str,
            response_payload: dict[str, Any],
            responded_by: str,
    ) -> None:
        if self.execution_store is None or not all(
                hasattr(self.execution_store, method)
                for method in ("list_execution_waits", "resolve_execution_wait")
        ):
            return
        waits = await self.execution_store.list_execution_waits(
            execution_id,
            status=ExecutionWaitStatus.PENDING,
        )
        wait = next(
            (
                item for item in waits
                if item.kind == ExecutionWaitKind.APPROVAL
                and item.metadata.get("approval_request_id") == approval_request_id
            ),
            None,
        )
        if wait is None:
            return
        await self.execution_store.resolve_execution_wait(
            wait.id,
            # Approval and rejection are both decisions that wake the same
            # continuation. Rejection semantics are handled by the tool policy.
            status=ExecutionWaitStatus.RESOLVED,
            resolution_key=f"approval:{approval_request_id}:{status}",
            resolution_payload=response_payload,
            resolved_by=responded_by,
        )

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
