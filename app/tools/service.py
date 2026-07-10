from __future__ import annotations

from uuid import uuid4

from app.core.time import utc_now
from app.domain import Execution, ExecutionEventType, ExecutionStatus, ToolDefinition
from app.runtime.native.approvals import ApprovalManager
from app.runtime.native.errors import ToolExecutionError
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import ExecutionStore, NativeExecutionState
from .registry import ToolRegistry
from .validation import ToolValidationService


class ToolService:
    def __init__(
            self,
            *,
            tool_registry: ToolRegistry,
            execution_store: ExecutionStore,
            approval_manager: ApprovalManager,
    ):
        self.tool_registry = tool_registry
        self.execution_store = execution_store
        self.approval_manager = approval_manager
        self.validation_service = ToolValidationService()
        self.emitter = ExecutionEventEmitter(execution_store)

    def validate_definition(self, tool: ToolDefinition):
        return self.validation_service.validate(tool)

    async def test_tool(self, tool: ToolDefinition, arguments: dict):
        execution = Execution(
            id=f"tool-test-{uuid4()}",
            workflow_id="tool-test",
            runtime_adapter_id="native",
            status=ExecutionStatus.RUNNING,
            trigger_type="manual",
            trigger_payload={"mode": "tool_test"},
            input_payload=arguments,
            metadata={"mode": "tool_test", "tool_id": tool.id},
            started_at=utc_now(),
        )
        state = NativeExecutionState(execution_id=execution.id, workflow_id=execution.workflow_id)
        await self.execution_store.save_execution(execution)
        await self.emitter.emit(
            state,
            ExecutionEventType.EXECUTION_CREATED,
            payload={"workflow_id": execution.workflow_id, "mode": "tool_test"},
        )

        await self.emitter.emit(
            state,
            ExecutionEventType.TOOL_CALL_STARTED,
            payload={"tool_id": tool.id, "tool_name": tool.name, "arguments": arguments, "audit": True},
        )
        try:
            output = await self.tool_registry.execute(
                tool,
                arguments,
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
            )
            execution.status = ExecutionStatus.COMPLETED
            execution.output_payload = output
            execution.completed_at = utc_now()
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(
                state,
                ExecutionEventType.TOOL_CALL_COMPLETED,
                payload={"tool_id": tool.id, "tool_name": tool.name, "output": output, "audit": True},
            )
            return {
                "execution_id": execution.id,
                "output": output,
                "events": [event.model_dump(mode="json") for event in
                           await self.execution_store.list_events(execution.id)],
            }
        except Exception as exc:
            execution.status = ExecutionStatus.FAILED
            execution.error = str(exc)
            execution.completed_at = utc_now()
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(
                state,
                ExecutionEventType.TOOL_CALL_FAILED,
                payload={"tool_id": tool.id, "tool_name": tool.name, "error": str(exc), "audit": True},
            )
            if isinstance(exc, ToolExecutionError):
                raise
            raise ToolExecutionError(str(exc)) from exc
