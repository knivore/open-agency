from __future__ import annotations

import json

from app.domain import ToolDefinition, ToolType
from app.runtime.native.errors import ToolExecutionError
from app.services.agent_tools import (
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    SYSTEM_EXECUTION_EVENTS_TOOL_ID,
    SYSTEM_EXECUTION_GET_TOOL_ID,
    SYSTEM_EXECUTION_TOOL_TARGET,
    SYSTEM_WORKFLOW_GET_TOOL_ID,
    SYSTEM_WORKFLOW_LIST_TOOL_ID,
    SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID,
    SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID,
    SYSTEM_WORKFLOW_RUN_TOOL_ID,
    SYSTEM_WORKFLOW_TOOL_TARGET,
)
from .base import ToolExecutionContext


class WorkflowToolExecutor:
    tool_type = ToolType.WORKFLOW_TOOL
    async_execution = True

    async def aexecute(self, tool: ToolDefinition, arguments: dict[str, object], context: ToolExecutionContext) -> dict[
        str, object]:
        if tool.implementation.target == SYSTEM_EXECUTION_TOOL_TARGET or tool.id in {
            SYSTEM_EXECUTION_GET_TOOL_ID,
            SYSTEM_EXECUTION_EVENTS_TOOL_ID,
            SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
        }:
            return await self._execute_system_execution_tool(tool, arguments, context)
        if tool.implementation.target == SYSTEM_WORKFLOW_TOOL_TARGET or tool.id in {
            SYSTEM_WORKFLOW_LIST_TOOL_ID,
            SYSTEM_WORKFLOW_GET_TOOL_ID,
            SYSTEM_WORKFLOW_RUN_TOOL_ID,
            SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID,
            SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID,
        }:
            return await self._execute_system_workflow_tool(tool, arguments, context)
        if context.runtime_registry is None:
            raise ToolExecutionError(f"Workflow tool '{tool.id}' cannot execute without a runtime registry")
        workflow_id = arguments.get("workflow_id") or tool.implementation.config.get(
            "workflow_id") or tool.implementation.target
        trigger = {"created_by": "workflow_tool", "parent_execution_id": context.execution_id}
        input_payload = arguments.get("input_payload")
        if not isinstance(input_payload, dict):
            input_payload = arguments.get("input") if isinstance(arguments.get("input"), dict) else {}
        execution = await context.runtime_registry.create_execution(str(workflow_id), input_payload, trigger)
        result = await context.runtime_registry.start_execution(execution.id)
        return {
            "execution_id": result.id,
            "status": result.status.value,
            "output": result.output_payload,
            "error": result.error,
        }

    async def _execute_system_workflow_tool(
            self,
            tool: ToolDefinition,
            arguments: dict[str, object],
            context: ToolExecutionContext,
    ) -> dict[str, object]:
        if context.runtime_registry is None:
            raise ToolExecutionError(f"Workflow tool '{tool.id}' cannot execute without a runtime registry")
        repository = context.runtime_registry.workflow_repository
        if tool.id == SYSTEM_WORKFLOW_LIST_TOOL_ID:
            list_method = getattr(repository, "list", None)
            if list_method is None:
                return {"status": "error", "error": "Workflow repository does not support list()."}
            workflows = await list_method()
            return {
                "status": "ok",
                "workflows": [workflow.model_dump(mode="json") for workflow in workflows],
                "count": len(workflows),
            }
        if tool.id == SYSTEM_WORKFLOW_GET_TOOL_ID:
            workflow_id = str(arguments.get("workflow_id") or "")
            if not workflow_id:
                return {"status": "error", "error": "workflow_id is required."}
            workflow = await repository.get_workflow(workflow_id)
            if workflow is None:
                return {"status": "error", "error": f"Workflow '{workflow_id}' was not found."}
            return {"status": "ok", "workflow": workflow.model_dump(mode="json")}
        if tool.id in {SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID, SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID}:
            return {
                "status": "requires_conversation_context",
                "error": f"{tool.id} requires a conversation approval context.",
            }

        workflow_id = arguments.get("workflow_id") or tool.implementation.config.get(
            "workflow_id") or tool.implementation.target
        trigger = {"created_by": "workflow_tool", "parent_execution_id": context.execution_id}
        execution = await context.runtime_registry.create_execution(str(workflow_id), arguments.get("input", {}),
                                                                    trigger)
        result = await context.runtime_registry.start_execution(execution.id)
        return {
            "execution_id": result.id,
            "status": result.status.value,
            "output": result.output_payload,
            "error": result.error,
        }

    async def _execute_system_execution_tool(
            self,
            tool: ToolDefinition,
            arguments: dict[str, object],
            context: ToolExecutionContext,
    ) -> dict[str, object]:
        if context.execution_store is None:
            raise ToolExecutionError(f"Execution inspection tool '{tool.id}' cannot execute without an execution store")
        execution_id = str(arguments.get("execution_id") or "")
        if not execution_id:
            return {"status": "error", "error": "execution_id is required."}
        execution = await context.execution_store.get_execution(execution_id)
        if execution is None:
            return {"status": "error", "error": f"Execution '{execution_id}' was not found."}
        if tool.id == SYSTEM_EXECUTION_GET_TOOL_ID:
            return {"status": "ok", "execution": execution.model_dump(mode="json")}
        if tool.id == SYSTEM_EXECUTION_EVENTS_TOOL_ID:
            after_sequence = int(arguments.get("after_sequence") or 0)
            limit = max(1, min(int(arguments.get("limit") or 200), 1000))
            events = await context.execution_store.list_events_after(execution_id, after_sequence)
            event_types = arguments.get("event_types")
            if isinstance(event_types, list) and event_types:
                allowed = {str(item) for item in event_types}
                events = [event for event in events if event.event_type.value in allowed]
            agent_id = arguments.get("agent_id")
            if isinstance(agent_id, str) and agent_id:
                events = [event for event in events if event.agent_id == agent_id]
            task_id = arguments.get("task_id")
            if isinstance(task_id, str) and task_id:
                events = [event for event in events if event.task_id == task_id]
            total = len(events)
            events = events[:limit]
            return {
                "status": "ok",
                "items": [event.model_dump(mode="json") for event in events],
                "count": len(events),
                "total": total,
            }
        if tool.id == SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID:
            include_content = bool(arguments.get("include_content", True))
            max_content_chars = max(0, min(int(arguments.get("max_content_chars") or 4000), 20000))
            artifacts = await context.execution_store.list_artifacts(execution_id)
            return {
                "status": "ok",
                "items": [
                    _artifact_payload(
                        artifact.model_dump(mode="json"),
                        include_content=include_content,
                        max_content_chars=max_content_chars,
                    )
                    for artifact in artifacts
                ],
                "count": len(artifacts),
            }
        return {"status": "error", "error": f"Unknown execution inspection tool '{tool.id}'."}


def _artifact_payload(payload: dict[str, object], *, include_content: bool, max_content_chars: int) -> dict[str, object]:
    if not include_content:
        payload.pop("content_json", None)
        payload.pop("content_text", None)
        payload["content_omitted"] = True
        return payload
    text = payload.get("content_text")
    if isinstance(text, str) and len(text) > max_content_chars:
        payload["content_text"] = text[:max_content_chars].rstrip() + "..."
        payload["content_text_truncated"] = True
    content_json = payload.get("content_json")
    if content_json is not None:
        serialized = json.dumps(content_json, sort_keys=True, default=str)
        payload["content_json_size_chars"] = len(serialized)
    return payload
