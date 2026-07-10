from __future__ import annotations

import json

from app.domain import ToolDefinition, ToolType
from app.runtime.native.errors import ToolExecutionError
from app.runtime.native.state import (
    add_graph_working_set_items,
    create_graph_working_set,
    prune_expired_graph_working_sets,
    remove_graph_working_set_items,
)
from app.services.agent_tools import (
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    SYSTEM_EXECUTION_EVENTS_TOOL_ID,
    SYSTEM_EXECUTION_GET_TOOL_ID,
    SYSTEM_EXECUTION_LIST_TOOL_ID,
    SYSTEM_EXECUTION_TOOL_TARGET,
    SYSTEM_GRAPH_TOOL_TARGET,
    SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID,
    SYSTEM_WORKFLOW_GET_TOOL_ID,
    SYSTEM_WORKFLOW_LIST_TOOL_ID,
    SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID,
    SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID,
    SYSTEM_WORKFLOW_RUN_TOOL_ID,
    SYSTEM_WORKFLOW_TOOL_TARGET,
)
from .base import ToolExecutionContext

_GRAPH_WORKING_SET_TOOL_IDS = {
    SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID,
}


class WorkflowToolExecutor:
    tool_type = ToolType.WORKFLOW_TOOL
    async_execution = True

    async def aexecute(self, tool: ToolDefinition, arguments: dict[str, object], context: ToolExecutionContext) -> dict[
        str, object]:
        if tool.implementation.target == SYSTEM_EXECUTION_TOOL_TARGET or tool.id in {
            SYSTEM_EXECUTION_LIST_TOOL_ID,
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
        if tool.implementation.target == SYSTEM_GRAPH_TOOL_TARGET and tool.id in _GRAPH_WORKING_SET_TOOL_IDS:
            return await self._execute_graph_working_set_tool(tool, arguments, context)
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

    async def _execute_graph_working_set_tool(
            self,
            tool: ToolDefinition,
            arguments: dict[str, object],
            context: ToolExecutionContext,
    ) -> dict[str, object]:
        state, error = await _graph_working_set_state(arguments, context)
        if error:
            return {"status": "error", "error": error}
        if tool.id == SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID:
            working_set = create_graph_working_set(
                state,
                working_set_id=_optional_string(arguments.get("working_set_id")),
                owner_agent_id=_optional_string(arguments.get("owner_agent_id")) or context.agent_id,
                conversation_id=_optional_string(arguments.get("conversation_id")),
                workflow_id=_optional_string(arguments.get("workflow_id")) or context.workflow_id or state.workflow_id,
                run_id=_optional_string(arguments.get("run_id")) or context.execution_id,
                execution_id=_optional_string(arguments.get("execution_id")) or context.execution_id,
                anchors=_dict_list(arguments.get("anchors")),
                notes=_dict_list(arguments.get("notes")),
                ttl_seconds=_bounded_int(arguments.get("ttl_seconds") or 21600, minimum=60, maximum=86400),
            )
            return {"status": "ok", "working_set": working_set.to_dict()}
        working_set_id = str(arguments.get("working_set_id") or "")
        working_set = state.graph_working_sets.get(working_set_id)
        if working_set is None:
            return {"status": "error", "error": f"Graph working set '{working_set_id}' was not found."}
        if tool.id == SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID:
            add_graph_working_set_items(
                working_set,
                anchors=_dict_list(arguments.get("anchors")),
                visited_nodes=_dict_list(arguments.get("visited_nodes")),
                selected_nodes=_dict_list(arguments.get("selected_nodes")),
                notes=_dict_list(arguments.get("notes")),
                ttl_seconds=_bounded_int(arguments.get("ttl_seconds") or 21600, minimum=60, maximum=86400),
            )
            return {"status": "ok", "working_set": working_set.to_dict()}
        if tool.id == SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID:
            remove_graph_working_set_items(
                working_set,
                anchor_ids=_string_list(arguments.get("anchor_ids")),
                visited_node_ids=_string_list(arguments.get("visited_node_ids")),
                selected_node_ids=_string_list(arguments.get("selected_node_ids")),
                clear_notes=bool(arguments.get("clear_notes") is True),
                ttl_seconds=_bounded_int(arguments.get("ttl_seconds") or 21600, minimum=60, maximum=86400),
            )
            return {"status": "ok", "working_set": working_set.to_dict()}
        if tool.id == SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID:
            return {
                "status": "ok",
                "summary": (
                    f"Graph working set {working_set.working_set_id}: {len(working_set.anchors)} anchors, "
                    f"{len(working_set.visited_nodes)} visited nodes, "
                    f"{len(working_set.selected_nodes)} selected nodes, {len(working_set.notes)} notes."
                ),
                "working_set": working_set.to_dict(),
                "counts": {
                    "anchors": len(working_set.anchors),
                    "visited_nodes": len(working_set.visited_nodes),
                    "selected_nodes": len(working_set.selected_nodes),
                    "notes": len(working_set.notes),
                },
            }
        if tool.id == SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID:
            removed = state.graph_working_sets.pop(working_set_id, None)
            return {"status": "ok", "working_set_id": working_set_id, "cleared": removed is not None}
        if tool.id == SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID:
            return {
                "status": "requires_api_context",
                "error": f"{tool.id} requires ApiContext-backed ToolRuntimeExecutor to write durable memory.",
            }
        return {"status": "error", "error": f"Unknown graph working set tool '{tool.id}'."}

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
        if tool.id == SYSTEM_EXECUTION_LIST_TOOL_ID:
            workflow_id = _optional_string(arguments.get("workflow_id"))
            agent_id = _optional_string(arguments.get("agent_id"))
            active_only = bool(arguments.get("active_only"))
            statuses = {
                item.strip().lower()
                for item in arguments.get("status", [])
                if isinstance(item, str) and item.strip()
            } if isinstance(arguments.get("status"), list) else set()
            limit = _bounded_int(arguments.get("limit") or 20, minimum=1, maximum=200)

            if workflow_id:
                executions = await context.execution_store.list_executions_by_workflow(workflow_id)
            elif agent_id:
                executions = await context.execution_store.list_executions_by_agent(agent_id)
            elif active_only:
                executions = await context.execution_store.list_active_executions()
            else:
                executions = await context.execution_store.list_executions()

            if statuses:
                executions = [execution for execution in executions if execution.status.value.lower() in statuses]
            executions = sorted(executions, key=lambda execution: execution.created_at, reverse=True)[:limit]
            return {
                "status": "ok",
                "items": [execution.model_dump(mode="json") for execution in executions],
                "count": len(executions),
                "filters": {
                    "workflow_id": workflow_id,
                    "agent_id": agent_id,
                    "status": sorted(statuses),
                    "active_only": active_only,
                    "limit": limit,
                },
            }
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


async def _graph_working_set_state(arguments: dict[str, object], context: ToolExecutionContext):
    if context.runtime_registry is None:
        return None, "Runtime registry is unavailable."
    execution_id = _optional_string(arguments.get("execution_id")) or context.execution_id
    if not execution_id:
        return None, "execution_id is required."
    try:
        snapshot = await context.runtime_registry.get_execution_state(execution_id)
    except Exception as exc:
        return None, str(exc)
    state = getattr(snapshot, "state", None)
    if state is None:
        return None, f"Execution '{execution_id}' has no active native runtime state."
    prune_expired_graph_working_sets(state)
    return state, None


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(min(parsed, maximum), minimum)


def _artifact_payload(payload: dict[str, object], *, include_content: bool, max_content_chars: int) -> dict[
    str, object]:
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
