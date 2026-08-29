from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.domain import (
    SecuritySettings,
    ToolDefinition,
    ToolImplementationReference,
    ToolType,
    WorkflowDefinition,
)
from app.runtime.native.approvals import ApprovalDecision
from app.runtime.native.state import NativeExecutionState
from app.runtime.native.tool_executor import ToolExecutor


class _RecordingStore:
    def __init__(self) -> None:
        self.created: dict | None = None
        self.updated: dict | None = None

    async def create_tool_invocation(self, **kwargs) -> None:
        self.created = kwargs

    async def update_tool_invocation(self, invocation_id: str, **kwargs) -> None:
        self.updated = {"invocation_id": invocation_id, **kwargs}


class _RecordingEmitter:
    def __init__(self) -> None:
        self.store = _RecordingStore()
        self.events: list[dict] = []

    async def emit(self, state, event_type, **kwargs):
        self.events.append({"event_type": event_type, **kwargs})
        return SimpleNamespace(id=f"event-{len(self.events)}")

    async def record_artifact(self, state, **kwargs) -> None:
        raise AssertionError("the redaction fixture must not emit an artifact")


class _ApprovingManager:
    def __init__(self) -> None:
        self.request: dict | None = None

    async def request_approval(self, **kwargs) -> ApprovalDecision:
        self.request = kwargs
        return ApprovalDecision(granted=True, reason="approved for regression test")


class ToolExecutorRedactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_executor_normalizes_single_string_for_string_array_input(self) -> None:
        tool = ToolDefinition(
            id="repo-inspect",
            name="inspect_repo",
            description="Inspect a repository",
            tool_type=ToolType.PYTHON_FUNCTION,
            input_schema={
                "type": "object",
                "properties": {
                    "focus_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(
                implementation_type="python_function",
                target="tests.native_test_tools",
                callable_name="echo_tool",
            ),
        )
        workflow = WorkflowDefinition(
            id="workflow-repo-inspect-normalization",
            name="Repo inspect normalization",
            entrypoint="unused",
            tool_definitions=[tool],
        )
        approval_manager = _ApprovingManager()
        executor = ToolExecutor(approval_manager)
        executor.tool_registry.execute = AsyncMock(return_value={"status": "ok"})
        emitter = _RecordingEmitter()
        state = NativeExecutionState(execution_id="execution-repo-inspect-normalization", workflow_id=workflow.id)

        await executor.execute(
            workflow,
            state,
            emitter,
            tool_id="repo-inspect",
            arguments={"focus_paths": "lib/api/backend/graphStream.ts"},
        )

        executor.tool_registry.execute.assert_awaited_once()
        self.assertEqual(
            executor.tool_registry.execute.await_args.args[1],
            {"focus_paths": ["lib/api/backend/graphStream.ts"]},
        )

    async def test_tool_executor_redacts_arguments_and_output_before_persistence(self) -> None:
        secret = "typed-password-value"
        tool = ToolDefinition(
            id="mcp:computer:type",
            name="type",
            description="Type text into the active application",
            tool_type=ToolType.MCP_TOOL,
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(
                implementation_type="mcp_tool",
                target="computer-use",
                callable_name="Type",
                config={"tool_family": "computer_use", "canonical_tool_name": "type"},
            ),
            security=SecuritySettings(
                requires_approval=True,
                redaction_enabled=True,
                redaction_rules=["request", "raw_result", "data", "text", "content"],
            ),
        )
        workflow = WorkflowDefinition(
            id="workflow-redaction",
            name="Redaction regression",
            entrypoint="unused",
            tool_definitions=[tool],
        )
        raw_result = {
            "status": "ok",
            "request": {"text": secret},
            "raw_result": {"content": [{"type": "text", "text": secret}]},
            "data": {"text": secret},
        }
        approval_manager = _ApprovingManager()
        executor = ToolExecutor(approval_manager)
        executor.tool_registry.execute = AsyncMock(return_value=raw_result)
        emitter = _RecordingEmitter()
        state = NativeExecutionState(execution_id="execution-redaction", workflow_id=workflow.id)

        returned = await executor.execute(
            workflow,
            state,
            emitter,
            tool_id=tool.id,
            arguments={"text": secret},
        )

        # The immediate caller still receives the actual tool response; records
        # persisted directly by ToolExecutor cross the redaction boundary.
        self.assertEqual(returned, raw_result)
        assert approval_manager.request is not None
        self.assertEqual(approval_manager.request["payload"], {"text": secret})
        self.assertEqual(approval_manager.request["redacted_payload"], {"text": "[REDACTED]"})
        assert emitter.store.created is not None
        self.assertEqual(emitter.store.created["input_json"], {"text": "[REDACTED]"})
        assert emitter.store.updated is not None
        self.assertEqual(
            emitter.store.updated["output_json"],
            {
                "status": "ok",
                "request": "[REDACTED]",
                "raw_result": "[REDACTED]",
                "data": "[REDACTED]",
            },
        )
        self.assertNotIn(secret, repr(emitter.events))


if __name__ == "__main__":
    unittest.main()
