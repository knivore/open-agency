from __future__ import annotations

from app.domain import ToolDefinition, ToolType
from app.runtime.native.errors import ToolExecutionError
from .base import ToolExecutionContext


class HumanApprovalToolExecutor:
    tool_type = ToolType.HUMAN_APPROVAL
    async_execution = True

    async def aexecute(self, tool: ToolDefinition, arguments: dict[str, object], context: ToolExecutionContext) -> dict[
        str, object]:
        if context.approval_manager is None:
            raise ToolExecutionError(f"Human approval tool '{tool.id}' requires an approval manager")
        decision = await context.approval_manager.request_approval(
            execution_id=context.execution_id,
            tool_id=tool.id,
            payload=arguments,
        )
        return {
            "approved": decision.granted,
            "reason": decision.reason,
            "metadata": decision.metadata or {},
        }
