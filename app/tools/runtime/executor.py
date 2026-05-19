from __future__ import annotations

import ast
import json
import re
from typing import TYPE_CHECKING
from typing import Any
from uuid import uuid4

from app.core.config import get_settings
from app.domain import (
    ApprovalRequest,
    ApprovalTargetType,
    ApprovalType,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    UserDefinition,
)
from app.runtime.native.errors import ExecutionNotFoundError, ToolExecutionError, WorkflowNotFoundError
from app.services.agent_tools import (
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    SYSTEM_EXECUTION_EVENTS_TOOL_ID,
    SYSTEM_EXECUTION_GET_TOOL_ID,
)
from app.services.conversations import ConversationNotFoundError, ConversationService
from app.services.execution_classification import classify_execution_staleness
from app.services.executions import ExecutionService
from app.services.memory import MemoryPermissionError, MemoryPolicyError, MemoryService
from app.tools.contracts.models import FileChanged, ToolRunResponse
from app.tools.contracts.registry import ToolContractRegistry, get_default_contract_registry
from app.tools.contracts.validator import validate_tool_input, validate_tool_output
from app.tools.cli_discovery import list_builtin_tool_definitions, resolve_tool
from app.tools.executors import ShellCommandToolExecutor, ToolExecutionContext
from app.tools.implementations.browser import (
    click_element,
    open_browser,
    screenshot,
    screenshot_and_analyse,
    screenshot_and_extract,
    scroll_page,
    select_dropdown,
    send_keys,
    terminate_browser,
    verify_content,
)
from app.tools.implementations.custom.files import write_text_file
from app.tools.implementations.documents import save_markdown_to_word
from app.tools.implementations.human_input import request_human_input
from app.tools.implementations.http_integrations import execute_custom_api
from app.tools.implementations.spreadsheets import write_excel_image, write_excel_json, write_excel_text
from app.tools.policies import PolicyEngine
from app.tools.sandbox import apply_patch_dry_run, ensure_allowed_git_repo, summarize_files_changed

from .events import publish_tool_runtime_event
from .responses import build_tool_run_response
from .store import JsonlToolRunStore

if TYPE_CHECKING:
    from app.api.context import ApiContext


class ToolRuntimeExecutor:
    def __init__(
            self,
            *,
            context: "ApiContext | None" = None,
            contract_registry: ToolContractRegistry | None = None,
            policy_engine: PolicyEngine | None = None,
            run_store: JsonlToolRunStore | None = None,
    ):
        self.context = context
        self.contract_registry = contract_registry or get_default_contract_registry()
        self.policy_engine = policy_engine or PolicyEngine()
        self.run_store = run_store or JsonlToolRunStore()

    def run(self, tool_name: str, payload: dict[str, Any], *, actor: str | None = None) -> ToolRunResponse:
        contract = self.contract_registry.get_contract(tool_name)
        if contract is None:
            raise KeyError(f"Tool contract '{tool_name}' not found")
        validate_tool_input(contract, payload)
        publish_tool_runtime_event(lifecycle_type="tool.run.started", tool_name=contract.name, actor=actor)
        if tool_name == "sandbox-edit":
            response = self._run_sandbox_edit(payload, actor=actor)
        elif tool_name == "agency.http.request":
            response = self._run_http_request(payload, actor=actor)
        elif tool_name == "agency.workflow.list":
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name == "agency.workflow.get":
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name == "agency.tool.get":
            response = self._run_tool_get(payload, actor=actor)
        elif tool_name in {"agency.memory.list", "agency.memory.remember", "agency.memory.update", "agency.memory.delete"}:
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name == "agency.workflow.run":
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name == "agency.human.ask":
            response = self._run_human_ask(payload, actor=actor)
        elif tool_name in {
            "agency.workflow.propose-create",
            "agency.workflow.propose-update",
            "agency.tool.propose-create",
            "agency.tool.propose-update",
        }:
            response = self._conversation_context_required_response(tool_name, actor=actor)
        elif tool_name == "agency.tool.list":
            response = self._run_tool_list(actor=actor)
        elif tool_name == "agency.command.run":
            response = self._run_command_run(payload, actor=actor)
        elif tool_name == "agency.file.write-text":
            response = self._run_file_write_text(payload, actor=actor)
        elif tool_name == "agency.document.markdown-to-word":
            response = self._run_markdown_to_word(payload, actor=actor)
        elif tool_name in {"agency.excel.write-text", "agency.excel.write-json", "agency.excel.write-image"}:
            response = self._run_spreadsheet_write(tool_name, payload, actor=actor)
        elif tool_name.startswith("agency.browser."):
            response = self._run_browser_tool(tool_name, payload, actor=actor)
        else:
            response = self._runtime_context_required_response(tool_name, actor=actor)
        validate_tool_output(contract, response.model_dump(mode="json"))
        publish_tool_runtime_event(
            lifecycle_type="tool.run.completed",
            tool_name=contract.name,
            actor=actor,
            verdict=response.verdict,
            metadata={"signature": response.signature},
        )
        self.run_store.append(
            tool_name=contract.name,
            tool_version=contract.version,
            actor=actor,
            input_payload=payload,
            output=response,
        )
        return response

    async def run_async(self, tool_name: str, payload: dict[str, Any], *, actor: str | None = None) -> ToolRunResponse:
        if tool_name not in {
            "agency.workflow.list",
            "agency.workflow.get",
            "agency.execution.get",
            "agency.execution.events",
            "agency.execution.artifacts",
            "agency.tool.get",
            "agency.workflow.run",
            "agency.memory.list",
            "agency.memory.remember",
            "agency.memory.update",
            "agency.memory.delete",
            "agency.workflow.propose-create",
            "agency.workflow.propose-update",
            "agency.tool.propose-create",
            "agency.tool.propose-update",
        }:
            return self.run(tool_name, payload, actor=actor)
        contract = self.contract_registry.get_contract(tool_name)
        if contract is None:
            raise KeyError(f"Tool contract '{tool_name}' not found")
        validate_tool_input(contract, payload)
        publish_tool_runtime_event(lifecycle_type="tool.run.started", tool_name=contract.name, actor=actor)
        if tool_name == "agency.workflow.list":
            response = await self._run_workflow_list(actor=actor)
        elif tool_name == "agency.workflow.get":
            response = await self._run_workflow_get(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_GET_TOOL_ID:
            response = await self._run_execution_get(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_EVENTS_TOOL_ID:
            response = await self._run_execution_events(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID:
            response = await self._run_execution_artifacts(payload, actor=actor)
        elif tool_name == "agency.tool.get":
            response = await self._run_tool_get_async(payload, actor=actor)
        elif tool_name == "agency.workflow.run":
            response = await self._run_workflow_run(payload, actor=actor)
        elif tool_name in {
            "agency.workflow.propose-create",
            "agency.workflow.propose-update",
            "agency.tool.propose-create",
            "agency.tool.propose-update",
        }:
            response = await self._run_conversation_proposal(tool_name, payload, actor=actor)
        elif tool_name == "agency.memory.list":
            response = await self._run_memory_list(payload, actor=actor)
        elif tool_name == "agency.memory.remember":
            response = await self._run_memory_remember(payload, actor=actor)
        elif tool_name == "agency.memory.update":
            response = await self._run_memory_update(payload, actor=actor)
        elif tool_name == "agency.memory.delete":
            response = await self._run_memory_delete(payload, actor=actor)
        else:
            response = self.run(tool_name, payload, actor=actor)
            return response
        validate_tool_output(contract, response.model_dump(mode="json"))
        publish_tool_runtime_event(
            lifecycle_type="tool.run.completed",
            tool_name=contract.name,
            actor=actor,
            verdict=response.verdict,
            metadata={"signature": response.signature},
        )
        self.run_store.append(
            tool_name=contract.name,
            tool_version=contract.version,
            actor=actor,
            input_payload=payload,
            output=response,
        )
        return response

    def _run_sandbox_edit(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        dry_run = bool(payload.get("dryRun", True))
        policy_verdict = self.policy_engine.evaluate("sandbox-edit", payload, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="sandbox-edit",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        if policy_verdict.outcome == "deny":
            return build_tool_run_response(
                verdict="deny",
                policy_verdict=policy_verdict,
                patch=None,
                errors=[rule.reason for rule in policy_verdict.rules if rule.outcome == "deny" and rule.reason],
                dry_run=dry_run,
                actor=actor,
            )
        try:
            repo = ensure_allowed_git_repo(payload["repo"], self.policy_engine.allowed_repos)
            changes = list(payload.get("changes") or [])
            patch = apply_patch_dry_run(repo, changes)
            files_changed = summarize_files_changed(changes)
            return build_tool_run_response(
                verdict=policy_verdict.outcome,
                policy_verdict=policy_verdict,
                patch=patch,
                files_changed=files_changed,
                dry_run=dry_run,
                actor=actor,
            )
        except Exception as exc:
            return build_tool_run_response(
                verdict="warn" if policy_verdict.outcome != "deny" else "deny",
                policy_verdict=policy_verdict,
                patch=None,
                errors=[str(exc)],
                dry_run=dry_run,
                actor=actor,
            )

    def _run_http_request(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.http.request", payload, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="agency.http.request",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        if policy_verdict.outcome == "deny":
            return build_tool_run_response(
                verdict="deny",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[rule.reason for rule in policy_verdict.rules if rule.outcome == "deny" and rule.reason],
                dry_run=False,
                actor=actor,
            )

        try:
            result = execute_custom_api(
                url=str(payload["url"]),
                method=str(payload["method"]),
                headers=payload.get("headers"),
                query_params=payload.get("query_params"),
                body=payload.get("body"),
                verify_ssl=bool(payload.get("verify_ssl", True)),
            )
            result = {**result, "method": str(payload["method"]).upper(), "url": str(payload["url"])}
            return build_tool_run_response(
                verdict=policy_verdict.outcome,
                policy_verdict=policy_verdict,
                result=result,
                patch=None,
                dry_run=False,
                actor=actor,
            )
        except Exception as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )

    def _run_tool_list(self, *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.tool.list", {}, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="agency.tool.list",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        tools = [tool.model_dump(mode="json") for tool in list_builtin_tool_definitions()]
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"items": tools, "count": len(tools)},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    def _run_tool_get(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.tool.get", payload, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="agency.tool.get",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        tool_id = str(payload.get("tool_id") or "")
        tool = resolve_tool(tool_id, list_builtin_tool_definitions())
        if tool is None:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": f"Tool '{tool_id}' was not found."},
                patch=None,
                errors=[f"Tool '{tool_id}' was not found."],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "tool": tool.model_dump(mode="json")},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    def _context_required_response(self, tool_name: str, *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(tool_name, {}, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name=tool_name,
            actor=actor,
            verdict="warn",
            metadata={"policyScore": policy_verdict.score},
        )
        return build_tool_run_response(
            verdict="warn",
            policy_verdict=policy_verdict,
            result={"status": "error", "error": f"{tool_name} requires ApiContext; use the API runtime route."},
            patch=None,
            errors=[f"{tool_name} requires ApiContext; use the API runtime route."],
            dry_run=True,
            actor=actor,
        )

    def _conversation_context_required_response(self, tool_name: str, *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(tool_name, {}, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name=tool_name,
            actor=actor,
            verdict="warn",
            metadata={"policyScore": policy_verdict.score},
        )
        message = (
            f"{tool_name} requires conversation/profile/origin-message approval context; "
            "use the conversation tool execution path until the approval-runtime bridge is implemented."
        )
        return build_tool_run_response(
            verdict="warn",
            policy_verdict=policy_verdict,
            result={"status": "requires_conversation_context", "error": message},
            patch=None,
            errors=[message],
            dry_run=True,
            actor=actor,
        )

    async def _run_conversation_proposal(
            self,
            tool_name: str,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._conversation_context_required_response(tool_name, actor=actor)
        conversation_id = _string_value(payload.get("conversation_id"))
        if not conversation_id:
            return self._conversation_context_required_response(tool_name, actor=actor)
        try:
            service = ConversationService(self.context)
            profile = await service._resolve_main_profile(conversation_id)
            origin_message_id = await self._ensure_contract_origin_message(
                conversation_id=conversation_id,
                origin_message_id=_string_value(payload.get("origin_message_id")),
                tool_name=tool_name,
            )
            proposal_payload = _without_contract_context(payload)
            if tool_name == "agency.workflow.propose-create":
                result = await service._create_workflow_create_proposal(
                    profile=profile,
                    conversation_id=conversation_id,
                    origin_message_id=origin_message_id,
                    request=proposal_payload,
                )
            elif tool_name == "agency.workflow.propose-update":
                result = await service._create_workflow_update_proposal(
                    profile=profile,
                    conversation_id=conversation_id,
                    origin_message_id=origin_message_id,
                    request=proposal_payload,
                )
            elif tool_name == "agency.tool.propose-create":
                result = await service._create_tool_create_proposal(
                    profile=profile,
                    conversation_id=conversation_id,
                    origin_message_id=origin_message_id,
                    request=proposal_payload,
                )
            else:
                result = await service._create_tool_update_proposal(
                    profile=profile,
                    conversation_id=conversation_id,
                    origin_message_id=origin_message_id,
                    request=proposal_payload,
                )
            approval = result.get("approval_request")
            status = "approval_requested" if approval else "error"
            error = _assistant_error(result) if approval is None else None
            return build_tool_run_response(
                verdict=policy_verdict.outcome if approval is not None else "warn",
                policy_verdict=policy_verdict,
                result={"status": status, **result, **({"error": error} if error else {})},
                patch=None,
                errors=[error] if error else [],
                dry_run=True,
                actor=actor,
            )
        except ConversationNotFoundError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=True,
                actor=actor,
            )

    def _run_human_ask(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.human.ask", payload, actor=actor)
        self._publish_policy_event("agency.human.ask", policy_verdict, actor=actor)
        try:
            timeout = int(payload.get("timeout_seconds") or payload.get("timeout") or 60)
            result = request_human_input(str(payload["query"]), process_id=payload.get("process_id"), timeout=timeout)
            verdict = "ok" if result.get("status") == "received" else "warn"
            return build_tool_run_response(
                verdict=verdict if policy_verdict.outcome != "deny" else "deny",
                policy_verdict=policy_verdict,
                result=result,
                patch=None,
                errors=[] if result.get("status") == "received" else ["Human input timed out before a reply was received."],
                dry_run=False,
                actor=actor,
            )
        except Exception as exc:
            return build_tool_run_response(
                verdict="warn" if policy_verdict.outcome != "deny" else "deny",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )

    def _run_browser_tool(self, tool_name: str, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if policy_verdict.outcome == "deny":
            return build_tool_run_response(
                verdict="deny",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[rule.reason for rule in policy_verdict.rules if rule.outcome == "deny" and rule.reason],
                dry_run=False,
                actor=actor,
            )
        try:
            raw_result = _execute_browser_tool(tool_name, payload)
            error = _tool_result_error(raw_result)
            return build_tool_run_response(
                verdict="warn" if error else policy_verdict.outcome,
                policy_verdict=policy_verdict,
                result=_browser_result_payload(tool_name, raw_result, error=error),
                patch=None,
                errors=[error] if error else [],
                dry_run=False,
                actor=actor,
            )
        except Exception as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "tool": tool_name, "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )

    def _runtime_context_required_response(self, tool_name: str, *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(tool_name, {}, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name=tool_name,
            actor=actor,
            verdict="warn",
            metadata={"policyScore": policy_verdict.score},
        )
        message = (
            f"{tool_name} is contractized for discovery, validation, signed responses, and audit persistence, "
            "but requires a specialized runtime context before direct execution is safe. Use the existing "
            "browser, human, workflow, or adapter execution path until that executor is bridged into the "
            "contract runtime."
        )
        return build_tool_run_response(
            verdict="warn",
            policy_verdict=policy_verdict,
            result={"status": "requires_runtime_context", "error": message},
            patch=None,
            errors=[message],
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_list(self, *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.workflow.list", {}, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="agency.workflow.list",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        if self.context is None:
            return self._context_required_response("agency.workflow.list", actor=actor)
        workflows = await self.context.workflow_repo.list()
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
                "status": "ok",
                "workflows": [_workflow_summary(workflow.model_dump(mode="json")) for workflow in workflows],
            },
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_get(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.workflow.get", payload, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="agency.workflow.get",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        if self.context is None:
            return self._context_required_response("agency.workflow.get", actor=actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": f"Workflow '{workflow_id}' was not found."},
                patch=None,
                errors=[f"Workflow '{workflow_id}' was not found."],
                dry_run=True,
                actor=actor,
            )
        workflow_payload = workflow.model_dump(mode="json")
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
                "status": "ok",
                "workflow": workflow_payload,
                "summary": _workflow_summary(workflow_payload),
            },
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_execution_get(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_EXECUTION_GET_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_EXECUTION_GET_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_EXECUTION_GET_TOOL_ID, actor=actor)
        execution_id = str(payload.get("execution_id") or "")
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": f"Execution '{execution_id}' was not found."},
                patch=None,
                errors=[f"Execution '{execution_id}' was not found."],
                dry_run=True,
                actor=actor,
            )
        execution_payload = execution.model_dump(mode="json")
        execution_payload["stale_classification"] = classify_execution_staleness(
            execution,
            stale_after_seconds=get_settings().main_agent_workflow_monitor_stale_after_seconds,
        )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "execution": execution_payload},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_execution_events(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_EXECUTION_EVENTS_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_EXECUTION_EVENTS_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_EXECUTION_EVENTS_TOOL_ID, actor=actor)
        execution_id = str(payload.get("execution_id") or "")
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            error = f"Execution '{execution_id}' was not found."
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=True,
                actor=actor,
            )
        after_sequence = int(payload.get("after_sequence") or 0)
        limit = max(1, min(int(payload.get("limit") or 200), 1000))
        events = await self.context.execution_store.list_events_after(execution_id, after_sequence)
        event_types = payload.get("event_types")
        if isinstance(event_types, list) and event_types:
            allowed = {str(item) for item in event_types}
            events = [event for event in events if event.event_type.value in allowed]
        agent_id = payload.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            events = [event for event in events if event.agent_id == agent_id]
        task_id = payload.get("task_id")
        if isinstance(task_id, str) and task_id:
            events = [event for event in events if event.task_id == task_id]
        total = len(events)
        events = events[:limit]
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
                "status": "ok",
                "items": [event.model_dump(mode="json") for event in events],
                "count": len(events),
                "total": total,
            },
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_execution_artifacts(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID, actor=actor)
        execution_id = str(payload.get("execution_id") or "")
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            error = f"Execution '{execution_id}' was not found."
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=True,
                actor=actor,
            )
        include_content = bool(payload.get("include_content", True))
        max_content_chars = max(0, min(int(payload.get("max_content_chars") or 4000), 20000))
        artifacts = await self.context.execution_store.list_artifacts(execution_id)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
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
            },
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_run(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.workflow.run", payload, actor=actor)
        self._publish_policy_event("agency.workflow.run", policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response("agency.workflow.run", actor=actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            error = f"Workflow '{workflow_id}' was not found."
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=False,
                actor=actor,
            )
        if workflow.metadata.get("protected_execution") is True:
            conversation_id = _string_value(payload.get("conversation_id"))
            if conversation_id:
                return await self._request_workflow_execution_approval(
                    workflow=workflow,
                    payload=payload,
                    conversation_id=conversation_id,
                    policy_verdict=policy_verdict,
                    actor=actor,
                )
            error = (
                f"Workflow '{workflow_id}' requires approval context before execution; use a conversation approval "
                "path or provide an approved execution bridge."
            )
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "requires_approval_context", "workflow_id": workflow_id, "error": error},
                patch=None,
                errors=[error],
                dry_run=True,
                actor=actor,
            )
        try:
            service = ExecutionService(self.context)
            execution = await service.create_execution(
                workflow_id=workflow_id,
                input_payload=payload.get("input_payload") if isinstance(payload.get("input_payload"), dict) else {},
                trigger={"type": "contract_tool", "tool": "agency.workflow.run", "actor": actor},
                runtime_adapter_id=payload.get("runtime_adapter_id") if isinstance(payload.get("runtime_adapter_id"), str) else None,
            )
            queued = await service.queue_start(execution["id"])
            return build_tool_run_response(
                verdict=policy_verdict.outcome,
                policy_verdict=policy_verdict,
                result={
                    "status": queued.get("status", "queued"),
                    "workflow_id": workflow_id,
                    "workflow_name": workflow.name,
                    "execution_id": queued["id"],
                    "execution_status": queued.get("status", "queued"),
                    "execution": queued,
                },
                patch=None,
                dry_run=False,
                actor=actor,
            )
        except (ExecutionNotFoundError, WorkflowNotFoundError, KeyError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "workflow_id": workflow_id, "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )

    async def _request_workflow_execution_approval(
            self,
            *,
            workflow,
            payload: dict[str, Any],
            conversation_id: str,
            policy_verdict,
            actor: str | None,
    ) -> ToolRunResponse:
        try:
            service = ConversationService(self.context)
            profile = await service._resolve_main_profile(conversation_id)
            origin_message_id = await self._ensure_contract_origin_message(
                conversation_id=conversation_id,
                origin_message_id=_string_value(payload.get("origin_message_id")),
                tool_name="agency.workflow.run",
            )
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
                    "input_payload": payload.get("input_payload") if isinstance(payload.get("input_payload"), dict) else {},
                    "runtime_adapter_id": payload.get("runtime_adapter_id"),
                },
                metadata={"action": "workflow_execution", "source_tool": "agency.workflow.run"},
            )
            created = await self.context.conversation_approval_repo.create(approval)
            approval_message = await service._append_approval_request_message(
                conversation_id=conversation_id,
                profile_id=profile.id,
                approval=created,
                target={"type": created.target_type.value, "id": created.target_id, "name": workflow.name},
            )
            await service.publish_approval_requested(conversation_id, created.model_dump(mode="json"))
            return build_tool_run_response(
                verdict=policy_verdict.outcome,
                policy_verdict=policy_verdict,
                result={
                    "status": "approval_requested",
                    "workflow_id": workflow.id,
                    "workflow_name": workflow.name,
                    "approval_request": created.model_dump(mode="json"),
                    "assistant_message": approval_message.model_dump(mode="json"),
                },
                patch=None,
                dry_run=True,
                actor=actor,
            )
        except ConversationNotFoundError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=True,
                actor=actor,
            )

    async def _ensure_contract_origin_message(
            self,
            *,
            conversation_id: str,
            origin_message_id: str | None,
            tool_name: str,
    ) -> str:
        if origin_message_id:
            return origin_message_id
        message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text=f"Contract request from {tool_name}.",
                metadata={"source": "contract_runtime", "tool_name": tool_name},
            )
        )
        return message.id

    async def _run_tool_get_async(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.tool.get", payload, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="agency.tool.get",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        tool_id = str(payload.get("tool_id") or "")
        tool = await self.context.tool_repo.get(tool_id) if self.context is not None else None
        if tool is None:
            fallback = resolve_tool(tool_id, list_builtin_tool_definitions())
            tool_payload = fallback.model_dump(mode="json") if fallback is not None else None
        else:
            tool_payload = tool.model_dump(mode="json")
        if tool_payload is None:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": f"Tool '{tool_id}' was not found."},
                patch=None,
                errors=[f"Tool '{tool_id}' was not found."],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "tool": tool_payload},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_memory_list(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.memory.list", payload, actor=actor)
        self._publish_policy_event("agency.memory.list", policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response("agency.memory.list", actor=actor)
        current_user = await self._current_user(actor)
        service = MemoryService(self.context)
        memories = await service.list_memories(
            scope=payload.get("scope") if isinstance(payload.get("scope"), str) else None,
            q=payload.get("query") if isinstance(payload.get("query"), str) else None,
            limit=int(payload.get("limit") or 20),
            current_user=current_user,
        )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "memories": [item.model_dump(mode="json") for item in memories]},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_memory_remember(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.memory.remember", payload, actor=actor)
        self._publish_policy_event("agency.memory.remember", policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response("agency.memory.remember", actor=actor)
        current_user = await self._current_user(actor)
        memory_payload = {
            "scope": payload.get("scope"),
            "content": payload.get("content"),
            "summary": payload.get("summary"),
            "tags": payload.get("tags") if isinstance(payload.get("tags"), list) else [],
            "sensitive": payload.get("sensitive"),
            "created_by_user_id": payload.get("created_by_user_id"),
            "workspace_id": payload.get("workspace_id"),
            "conversation_id": payload.get("conversation_id"),
            "workflow_id": payload.get("workflow_id"),
            "source": "contract_runtime",
            "metadata": {"contract_tool": "agency.memory.remember"},
        }
        service = MemoryService(self.context)
        try:
            created = await service.create_memory(
                memory_payload,
                confirmed=bool(payload.get("confirmed")),
                current_user=current_user,
                trusted_actor=bool(actor and actor.startswith("approved/")),
            )
        except (MemoryPolicyError, MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response("agency.memory.remember", policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "memory": created.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_memory_update(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.memory.update", payload, actor=actor)
        self._publish_policy_event("agency.memory.update", policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response("agency.memory.update", actor=actor)
        current_user = await self._current_user(actor)
        patch = {
            key: value
            for key, value in {
                "content": payload.get("content"),
                "summary": payload.get("summary"),
                "tags": payload.get("tags"),
                "sensitive": payload.get("sensitive"),
                "metadata": {"contract_tool": "agency.memory.update"},
            }.items()
            if value is not None
        }
        service = MemoryService(self.context)
        try:
            updated = await service.update_memory(
                str(payload.get("memory_id") or ""),
                patch,
                confirmed=bool(payload.get("confirmed")),
                current_user=current_user,
                trusted_actor=bool(actor and actor.startswith("approved/")),
            )
        except (MemoryPolicyError, MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response("agency.memory.update", policy_verdict, str(exc), actor=actor)
        if updated is None:
            return self._memory_error_response(
                "agency.memory.update",
                policy_verdict,
                f"Memory '{payload.get('memory_id')}' was not found.",
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "memory": updated.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_memory_delete(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.memory.delete", payload, actor=actor)
        self._publish_policy_event("agency.memory.delete", policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response("agency.memory.delete", actor=actor)
        current_user = await self._current_user(actor)
        service = MemoryService(self.context)
        try:
            deleted = await service.delete_memory(
                str(payload.get("memory_id") or ""),
                current_user=current_user,
                trusted_actor=bool(actor and actor.startswith("approved/")),
            )
        except MemoryPermissionError as exc:
            return self._memory_error_response("agency.memory.delete", policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome if deleted else "warn",
            policy_verdict=policy_verdict,
            result={"status": "ok" if deleted else "error", "deleted": deleted, "memory_id": payload.get("memory_id")},
            patch=None,
            errors=[] if deleted else [f"Memory '{payload.get('memory_id')}' was not found."],
            dry_run=False,
            actor=actor,
        )

    async def _current_user(self, actor: str | None):
        if not actor:
            return None
        if self.context is not None and hasattr(self.context.user_repo, "get"):
            persisted = await self.context.user_repo.get(actor)
            if persisted is not None:
                return persisted
        return UserDefinition(
            id=actor,
            email=actor if "@" in actor else f"{actor}@agency.local",
            roles=["admin"],
            provider="local",
            provider_subject=actor,
        )

    def _publish_policy_event(self, tool_name: str, policy_verdict, *, actor: str | None) -> None:
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name=tool_name,
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )

    def _memory_error_response(
            self,
            tool_name: str,
            policy_verdict,
            error: str,
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        return build_tool_run_response(
            verdict="warn" if policy_verdict.outcome != "deny" else "deny",
            policy_verdict=policy_verdict,
            result={"status": "error", "error": error},
            patch=None,
            errors=[error],
            dry_run=False,
            actor=actor,
        )

    def _run_command_run(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.command.run", payload, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="agency.command.run",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        if policy_verdict.outcome == "deny":
            return build_tool_run_response(
                verdict="deny",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[rule.reason for rule in policy_verdict.rules if rule.outcome == "deny" and rule.reason],
                dry_run=False,
                actor=actor,
            )

        tool = resolve_tool("agency.command.run", list_builtin_tool_definitions())
        if tool is None:
            return build_tool_run_response(
                verdict="deny",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=["agency.command.run definition is not registered"],
                dry_run=False,
                actor=actor,
            )

        try:
            result = ShellCommandToolExecutor().execute(
                tool,
                payload,
                ToolExecutionContext(execution_id=f"contract-command-{uuid4().hex}"),
            )
            verdict = policy_verdict.outcome
            if result.get("status") == "error" and verdict == "ok":
                verdict = "warn"
            return build_tool_run_response(
                verdict=verdict,
                policy_verdict=policy_verdict,
                result=result,
                patch=None,
                dry_run=False,
                actor=actor,
            )
        except ToolExecutionError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )

    def _run_file_write_text(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.file.write-text", payload, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="agency.file.write-text",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        if policy_verdict.outcome == "deny":
            return build_tool_run_response(
                verdict="deny",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[rule.reason for rule in policy_verdict.rules if rule.outcome == "deny" and rule.reason],
                dry_run=False,
                actor=actor,
            )

        try:
            result = write_text_file(
                content=str(payload["content"]),
                mode=str(payload["mode"]),
                base_folder=str(payload["base_folder"]),
                filename=str(payload["filename"]),
            )
            return build_tool_run_response(
                verdict=policy_verdict.outcome,
                policy_verdict=policy_verdict,
                result=result,
                patch=None,
                files_changed=[FileChanged(path=str(result["path"]), op="modify")],
                dry_run=False,
                actor=actor,
            )
        except Exception as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )

    def _run_markdown_to_word(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.document.markdown-to-word", payload, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="agency.document.markdown-to-word",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        if policy_verdict.outcome == "deny":
            return build_tool_run_response(
                verdict="deny",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[rule.reason for rule in policy_verdict.rules if rule.outcome == "deny" and rule.reason],
                dry_run=False,
                actor=actor,
            )

        try:
            raw_result = save_markdown_to_word(
                markdown_text=str(payload["markdown_text"]),
                filename=str(payload["filename"]),
                img_directory=str(payload["img_directory"]),
                process_id=payload.get("process_id"),
                run_by=payload.get("run_by") or actor,
            )
            result = _normalize_markdown_to_word_result(payload, raw_result)
            errors = [result["message"]] if result["status"] == "error" else []
            verdict = "warn" if errors or policy_verdict.outcome == "warn" else "ok"
            return build_tool_run_response(
                verdict=verdict,
                policy_verdict=policy_verdict,
                result=result,
                patch=None,
                files_changed=[],
                errors=errors,
                dry_run=False,
                actor=actor,
            )
        except Exception as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )

    def _run_spreadsheet_write(self, tool_name: str, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name=tool_name,
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        if policy_verdict.outcome == "deny":
            return build_tool_run_response(
                verdict="deny",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[rule.reason for rule in policy_verdict.rules if rule.outcome == "deny" and rule.reason],
                dry_run=False,
                actor=actor,
            )

        try:
            raw_result = self._execute_spreadsheet_tool(tool_name, payload)
            result = _normalize_spreadsheet_result(tool_name, payload, raw_result)
            errors = [result["message"]] if result["status"] == "error" else []
            verdict = "warn" if errors or policy_verdict.outcome == "warn" else "ok"
            return build_tool_run_response(
                verdict=verdict,
                policy_verdict=policy_verdict,
                result=result,
                patch=None,
                files_changed=[FileChanged(path=str(payload["excel_file_path"]), op="modify")] if not errors else [],
                errors=errors,
                dry_run=False,
                actor=actor,
            )
        except Exception as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )

    def _execute_spreadsheet_tool(self, tool_name: str, payload: dict[str, Any]) -> Any:
        if tool_name == "agency.excel.write-text":
            return write_excel_text(
                text_file_path=str(payload["text_file_path"]),
                sheet_name=str(payload["sheet_name"]),
                excel_file_path=str(payload["excel_file_path"]),
                serial_number=int(payload["serial_number"]),
                header_title=payload.get("header_title"),
            )
        if tool_name == "agency.excel.write-json":
            return write_excel_json(
                json_file_path=str(payload["json_file_path"]),
                sheet_name=str(payload["sheet_name"]),
                excel_file_path=str(payload["excel_file_path"]),
                serial_number=int(payload["serial_number"]),
            )
        if tool_name == "agency.excel.write-image":
            return write_excel_image(
                sheet_name=str(payload["sheet_name"]),
                excel_file_path=str(payload["excel_file_path"]),
                image_path=str(payload["image_path"]),
                serial_number=int(payload["serial_number"]),
                header_title=payload.get("header_title"),
            )
        raise KeyError(f"Spreadsheet runtime '{tool_name}' is not implemented")


def _normalize_spreadsheet_result(tool_name: str, payload: dict[str, Any], raw_result: Any) -> dict[str, Any]:
    parsed = _parse_legacy_spreadsheet_result(raw_result)
    error_message = parsed.get("Error Message")
    success_message = parsed.get("Success Message")
    source_key = {
        "agency.excel.write-text": "text_file_path",
        "agency.excel.write-json": "json_file_path",
        "agency.excel.write-image": "image_path",
    }[tool_name]
    return {
        "status": "error" if error_message else "success",
        "message": str(error_message or success_message or raw_result),
        "workbook_path": str(payload["excel_file_path"]),
        "source_path": str(payload[source_key]),
        "sheet_name": str(payload["sheet_name"]),
        "serial_number": int(payload["serial_number"]),
        "raw": raw_result,
    }


def _parse_legacy_spreadsheet_result(raw_result: Any) -> dict[str, Any]:
    if isinstance(raw_result, dict):
        return raw_result
    if isinstance(raw_result, str):
        try:
            parsed = ast.literal_eval(raw_result)
        except (SyntaxError, ValueError):
            return {"Success Message": raw_result}
        if isinstance(parsed, dict):
            return parsed
    return {"Success Message": str(raw_result)}


def _normalize_markdown_to_word_result(payload: dict[str, Any], raw_result: str) -> dict[str, Any]:
    status = "error" if raw_result.startswith("Error") else "success"
    storage_uri = _extract_storage_uri(raw_result)
    return {
        "status": status,
        "message": raw_result,
        "filename": str(payload["filename"]),
        "artifact_directory": str(payload["img_directory"]),
        "storage_uri": storage_uri,
        "raw": raw_result,
    }


def _extract_storage_uri(message: str) -> str | None:
    match = re.search(r"s3://\S+", message)
    return match.group(0).rstrip(".") if match else None


def _execute_browser_tool(tool_name: str, payload: dict[str, Any]) -> Any:
    handlers = {
        "agency.browser.open": open_browser,
        "agency.browser.screenshot": screenshot,
        "agency.browser.analyze-screenshot": screenshot_and_analyse,
        "agency.browser.extract-screenshot": screenshot_and_extract,
        "agency.browser.scroll": scroll_page,
        "agency.browser.click": click_element,
        "agency.browser.select-option": select_dropdown,
        "agency.browser.type-text": send_keys,
        "agency.browser.verify-content": verify_content,
        "agency.browser.close": terminate_browser,
    }
    handler = handlers.get(tool_name)
    if handler is None:
        raise KeyError(f"Browser tool '{tool_name}' is not implemented")
    return handler(**payload)


def _tool_result_error(raw_result: Any) -> str | None:
    if isinstance(raw_result, dict):
        value = raw_result.get("Error Message") or raw_result.get("error")
        return str(value) if value else None
    if isinstance(raw_result, str) and raw_result.lower().startswith("error"):
        return raw_result
    return None


def _browser_result_payload(tool_name: str, raw_result: Any, *, error: str | None) -> dict[str, Any]:
    return {
        "status": "error" if error else "ok",
        "tool": tool_name,
        "output": raw_result,
        **({"error": error} if error else {}),
    }


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _without_contract_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in {"conversation_id", "origin_message_id"}}


def _assistant_error(result: dict[str, Any]) -> str:
    assistant = result.get("assistant_message") if isinstance(result.get("assistant_message"), dict) else {}
    return str(assistant.get("plain_text") or "Approval request could not be created.")


def _artifact_payload(payload: dict[str, Any], *, include_content: bool, max_content_chars: int) -> dict[str, Any]:
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
        payload["content_json_size_chars"] = len(json.dumps(content_json, sort_keys=True, default=str))
    return payload


def _workflow_summary(workflow: dict[str, Any]) -> dict[str, Any]:
    metadata = workflow.get("metadata") if isinstance(workflow.get("metadata"), dict) else {}
    monitoring = metadata.get("main_agent_monitoring") if isinstance(metadata.get("main_agent_monitoring"), dict) else {}
    monitoring_level = str(monitoring.get("level") or ("off" if monitoring.get("enabled") is False else "standard"))
    monitoring_enabled = monitoring.get("enabled") is not False and monitoring_level.lower() != "off"
    return {
        "id": workflow.get("id"),
        "name": workflow.get("name"),
        "description": workflow.get("description"),
        "input_keys": _workflow_input_keys(workflow),
        "protected_execution": any(
            bool(task.get("human_approval_required")) for task in workflow.get("task_definitions", [])
            if isinstance(task, dict)
        ),
        "mutable_by_agent": bool(workflow.get("metadata", {}).get("mutable_by_agent", True)),
        "monitoring": {
            "enabled": monitoring_enabled,
            "level": "off" if not monitoring_enabled else monitoring_level,
            "exempted": monitoring.get("enabled") is False,
            "reason": monitoring.get("reason"),
        },
    }


def _workflow_input_keys(workflow: dict[str, Any]) -> list[str]:
    metadata = workflow.get("metadata") if isinstance(workflow.get("metadata"), dict) else {}
    metadata_inputs = metadata.get("inputs")
    if isinstance(metadata_inputs, list):
        return sorted({item for item in metadata_inputs if isinstance(item, str) and item.strip()})
    keys: set[str] = set()
    for task in workflow.get("task_definitions", []):
        if not isinstance(task, dict):
            continue
        input_schema = task.get("input_schema") if isinstance(task.get("input_schema"), dict) else {}
        properties = input_schema.get("properties")
        if isinstance(properties, dict):
            keys.update(str(key) for key in properties)
    return sorted(keys)
