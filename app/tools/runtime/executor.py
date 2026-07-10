"""Contract-mediated tool execution for agents and external callers.

This module validates tool contract payloads, applies policy checks, requests
approval when required, dispatches to the appropriate implementation, persists a
tool-run audit record, and returns structured responses that can be correlated
with execution events.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

from app.core.config import get_settings
from app.domain import (
    ApprovalRequest,
    ApprovalTargetType,
    ApprovalType,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    MemoryScope,
    MemoryType,
)
from app.graph.neo4j_read import Neo4jGraphReadError
from app.graph.service import (
    GRAPH_NEIGHBORHOOD_MODES,
    GRAPH_NEIGHBORHOOD_PRESETS,
    GraphReadUnavailableError,
    close_graph_reader_if_needed,
    graph_document_payload,
    graph_neighbors_payload,
    resolve_graph_reader,
)
from app.integrations.onecli import build_onecli_proxy_url
from app.modules.registry import (
    optional_module_key_for_runtime_tool,
    optional_module_runtime_tool_handler_class,
    optional_module_runtime_tool_names,
)
from app.observability.redaction import Redactor
from app.observability.service import ObservabilityService
from app.runtime.native.errors import ExecutionNotFoundError, ToolExecutionError, WorkflowNotFoundError
from app.runtime.native.state import (
    add_graph_working_set_items,
    create_graph_working_set,
    prune_expired_graph_working_sets,
    remove_graph_working_set_items,
)
from app.scheduler.scheduler import ScheduleConcurrencyError
from app.services.agency_graph_context import AgencyGraphContextService
from app.services.agent_tools import (
    SYSTEM_AGENT_GET_TOOL_ID,
    SYSTEM_AGENT_LIST_TOOL_ID,
    SYSTEM_AGENT_PROPOSE_UPDATE_TOOL_ID,
    SYSTEM_DOCUMENTS_DELETE_TOOL_ID,
    SYSTEM_DOCUMENTS_GET_TOOL_ID,
    SYSTEM_DOCUMENTS_LIST_TOOL_ID,
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    SYSTEM_EXECUTION_APPROVALS_TOOL_ID,
    SYSTEM_EXECUTION_APPROVE_TOOL_ID,
    SYSTEM_EXECUTION_CANCEL_TOOL_ID,
    SYSTEM_EXECUTION_EVENTS_TOOL_ID,
    SYSTEM_EXECUTION_GET_TOOL_ID,
    SYSTEM_EXECUTION_LIST_TOOL_ID,
    SYSTEM_EXECUTION_PAUSE_TOOL_ID,
    SYSTEM_EXECUTION_REJECT_TOOL_ID,
    SYSTEM_EXECUTION_RESUME_TOOL_ID,
    SYSTEM_GOAL_CANCEL_TOOL_ID,
    SYSTEM_GOAL_COMPLETE_TOOL_ID,
    SYSTEM_GOAL_CREATE_TOOL_ID,
    SYSTEM_GOAL_EVALUATE_TOOL_ID,
    SYSTEM_GOAL_EVIDENCE_ATTACH_TOOL_ID,
    SYSTEM_GOAL_GET_TOOL_ID,
    SYSTEM_GOAL_LIST_TOOL_ID,
    SYSTEM_GOAL_PAUSE_TOOL_ID,
    SYSTEM_GOAL_PLAN_TOOL_ID,
    SYSTEM_GOAL_REPLAN_TOOL_ID,
    SYSTEM_GOAL_RESUME_TOOL_ID,
    SYSTEM_GOAL_SUPERVISOR_DECISION_RECORD_TOOL_ID,
    SYSTEM_GOAL_SUPERVISOR_FINDINGS_TOOL_ID,
    SYSTEM_GOAL_UPDATE_TOOL_ID,
    SYSTEM_GRAPH_CONTEXT_TOOL_ID,
    SYSTEM_GRAPH_EXPAND_TOOL_ID,
    SYSTEM_GRAPH_NEIGHBORS_TOOL_ID,
    SYSTEM_GRAPH_PATH_TOOL_ID,
    SYSTEM_GRAPH_SEARCH_TOOL_ID,
    SYSTEM_GRAPH_SUMMARIZE_SUBGRAPH_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID,
    SYSTEM_MAIN_AGENT_MONITOR_GET_TOOL_ID,
    SYSTEM_MAIN_AGENT_MONITOR_UPDATE_ROUTES_TOOL_ID,
    SYSTEM_MEMORY_CATALOG_TOOL_ID,
    SYSTEM_MEMORY_DELETE_TOOL_ID,
    SYSTEM_MEMORY_EXCLUSIONS_ADD_TOOL_ID,
    SYSTEM_MEMORY_EXCLUSIONS_DELETE_TOOL_ID,
    SYSTEM_MEMORY_EXCLUSIONS_LIST_TOOL_ID,
    SYSTEM_MEMORY_LIST_TOOL_ID,
    SYSTEM_MEMORY_REMEMBER_TOOL_ID,
    SYSTEM_MEMORY_UPDATE_TOOL_ID,
    SYSTEM_OBSERVABILITY_EXECUTION_TIMELINE_TOOL_ID,
    SYSTEM_OBSERVABILITY_WORKFLOW_METRICS_TOOL_ID,
    SYSTEM_SCHEDULE_CREATE_TOOL_ID,
    SYSTEM_SCHEDULE_DELETE_TOOL_ID,
    SYSTEM_SCHEDULE_GET_TOOL_ID,
    SYSTEM_SCHEDULE_LIST_TOOL_ID,
    SYSTEM_SCHEDULE_TRIGGER_NOW_TOOL_ID,
    SYSTEM_SCHEDULE_UPDATE_TOOL_ID,
    SYSTEM_TOOL_WORKSPACE_LIST_TOOL_ID,
    SYSTEM_TOOL_WORKSPACE_PUBLISH_TOOL_ID,
    SYSTEM_TOOL_WORKSPACE_SCAFFOLD_TOOL_ID,
    SYSTEM_WORKFLOW_DOCUMENT_LINKS_TOOL_ID,
    SYSTEM_WORKFLOW_DOCUMENT_LINK_ADD_TOOL_ID,
    SYSTEM_WORKFLOW_DOCUMENT_LINK_DELETE_TOOL_ID,
    SYSTEM_WORKFLOW_DOCUMENT_SUMMARY_TOOL_ID,
    SYSTEM_WORKFLOW_GOVERNANCE_AUDIT_TOOL_ID,
    SYSTEM_WORKFLOW_GOVERNANCE_ACTION_TOOL_ID,
    SYSTEM_WORKFLOW_GOVERNANCE_BUNDLE_TOOL_ID,
    SYSTEM_WORKFLOW_GOVERNANCE_REMEDIATE_TOOL_ID,
    SYSTEM_WORKFLOW_GOVERNANCE_DOCUMENT_SUGGEST_TOOL_ID,
    SYSTEM_WORKFLOW_GOVERNANCE_REPAIR_TOOL_ID,
    SYSTEM_WORKFLOW_GOVERNANCE_REVIEW_QUEUE_TOOL_ID,
    SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSALS_TOOL_ID,
    SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_CREATE_TOOL_ID,
    SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_REQUEST_APPROVAL_TOOL_ID,
    SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_UPDATE_TOOL_ID,
    SYSTEM_WORKFLOW_MONITORING_EVENTS_TOOL_ID,
    SYSTEM_WORKFLOW_MONITOR_PROPOSAL_DISPATCH_TOOL_ID,
    SYSTEM_WORKFLOW_MEMORY_LINKS_ADD_TOOL_ID,
    SYSTEM_WORKFLOW_MEMORY_LINKS_DELETE_TOOL_ID,
    SYSTEM_WORKFLOW_MEMORY_LINKS_LIST_TOOL_ID,
    SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_GET_TOOL_ID,
    SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_UPDATE_TOOL_ID,
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACES_TOOL_ID,
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_CREATE_TOOL_ID,
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_UPDATE_TOOL_ID,
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_DELETE_TOOL_ID,
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORIES_TOOL_ID,
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_ADD_TOOL_ID,
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_REMOVE_TOOL_ID,
    SYSTEM_WORKFLOW_STEERING_APPROVALS_TOOL_ID,
    SYSTEM_WORKFLOW_STEERING_APPROVAL_CREATE_TOOL_ID,
    SYSTEM_WORKFLOW_STEERING_APPROVAL_REQUEST_APPROVAL_TOOL_ID,
    SYSTEM_WORKFLOW_STEERING_APPROVAL_UPDATE_TOOL_ID,
)
from app.services.conversations.core import ConversationNotFoundError, ConversationService
from app.services.document_ingestion import DocumentIngestionService
from app.services.execution_classification import classify_execution_staleness
from app.services.executions import ExecutionService
from app.services.generated_tool_workspace import GeneratedToolWorkspaceError, GeneratedToolWorkspaceService
from app.services.goals import GoalNotFoundError, GoalService, GoalTransitionError
from app.services.memory import MemoryPermissionError, MemoryPolicyError, MemoryService
from app.services.onecli import OneCLIIdentityMappingService
from app.services.schedules import ScheduleService
from app.services.workflows import WorkflowService
from app.tools.cli_discovery import list_builtin_tool_definitions, resolve_tool
from app.tools.contracts.models import FileChanged, ToolRunResponse
from app.tools.contracts.registry import ToolContractRegistry, get_default_contract_registry
from app.tools.contracts.validator import validate_tool_input, validate_tool_output
from app.tools.executors.base import ToolExecutionContext
from app.tools.executors.shell_command import ShellCommandToolExecutor
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
from app.tools.implementations.http_integrations import build_onecli_correlation_headers, execute_custom_api
from app.tools.implementations.human_input import request_human_input
from app.tools.implementations.spreadsheets import write_excel_image, write_excel_json, write_excel_text
from app.tools.policies.engine import PolicyEngine
from app.tools.risk import risk_metadata_for_contract_run
from app.tools.sandbox.git_sandbox import ensure_allowed_git_repo
from app.tools.sandbox.patch_apply import apply_patch_dry_run, summarize_files_changed
from .events import publish_tool_runtime_event
from .responses import build_tool_run_response
from .store import JsonlToolRunStore

if TYPE_CHECKING:
    from app.api.context import ApiContext

logger = logging.getLogger(__name__)
_GRAPH_OBSERVABILITY_REDACTOR = Redactor()

MEMORY_RUNTIME_TOOL_IDS = {
    SYSTEM_MEMORY_LIST_TOOL_ID,
    SYSTEM_MEMORY_CATALOG_TOOL_ID,
    SYSTEM_MEMORY_REMEMBER_TOOL_ID,
    SYSTEM_MEMORY_UPDATE_TOOL_ID,
    SYSTEM_MEMORY_DELETE_TOOL_ID,
    SYSTEM_MEMORY_EXCLUSIONS_LIST_TOOL_ID,
    SYSTEM_MEMORY_EXCLUSIONS_ADD_TOOL_ID,
    SYSTEM_MEMORY_EXCLUSIONS_DELETE_TOOL_ID,
    SYSTEM_WORKFLOW_MEMORY_LINKS_LIST_TOOL_ID,
    SYSTEM_WORKFLOW_MEMORY_LINKS_ADD_TOOL_ID,
    SYSTEM_WORKFLOW_MEMORY_LINKS_DELETE_TOOL_ID,
}

GRAPH_RUNTIME_TOOL_IDS = {
    SYSTEM_GRAPH_CONTEXT_TOOL_ID,
    SYSTEM_GRAPH_SEARCH_TOOL_ID,
    SYSTEM_GRAPH_EXPAND_TOOL_ID,
    SYSTEM_GRAPH_NEIGHBORS_TOOL_ID,
    SYSTEM_GRAPH_PATH_TOOL_ID,
    SYSTEM_GRAPH_SUMMARIZE_SUBGRAPH_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID,
    SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID,
}


def _optional_module_runtime_tool_ids() -> set[str]:
    return optional_module_runtime_tool_names()


WORKFLOW_MEMORY_LINK_TARGET_TYPES = {"workflow", "agent", "task"}
WORKFLOW_MEMORY_LINK_REF_TYPES = {"memory", "memory_collection"}
WORKFLOW_MEMORY_LINK_ACCESS_MODES = {"read", "read_write"}
WORKFLOW_MEMORY_LINK_METADATA_KEY = "memory_links"


class ToolRuntimeExecutor:
    def __init__(
            self,
            *,
            context: ApiContext | None = None,
            contract_registry: ToolContractRegistry | None = None,
            registry: Any | None = None,
            policy_engine: PolicyEngine | None = None,
            run_store: JsonlToolRunStore | None = None,
            tool_run_store: JsonlToolRunStore | None = None,
    ):
        self.context = context
        legacy_contract_registry = getattr(registry, "contract_registry", None)
        self.contract_registry = contract_registry or legacy_contract_registry or get_default_contract_registry()
        self.policy_engine = policy_engine or PolicyEngine()
        self.run_store = run_store or tool_run_store or JsonlToolRunStore()

    def _redacted_tool_run_record(
            self,
            tool_name: str,
            payload: dict[str, Any],
            response: ToolRunResponse,
    ) -> tuple[dict[str, Any], ToolRunResponse]:
        tool = resolve_tool(tool_name, list_builtin_tool_definitions())
        if tool is None or not tool.security.redaction_enabled:
            return payload, response

        # Persist a redacted audit view for tools that may carry credentials while leaving the
        # caller-visible response unchanged.
        redacted_input, _ = Redactor().redact_value(payload)
        redacted_output, _ = Redactor().redact_value(response.model_dump(mode="json"))
        return redacted_input, ToolRunResponse.model_validate(redacted_output)

    def run(self, tool_name: str, payload: dict[str, Any], *, actor: str | None = None) -> ToolRunResponse:
        contract = self.contract_registry.get_contract(tool_name)
        if contract is None:
            raise KeyError(f"Tool contract '{tool_name}' not found")
        validate_tool_input(contract, payload)
        risk_metadata = risk_metadata_for_contract_run(contract.name, payload)
        publish_tool_runtime_event(
            lifecycle_type="tool.run.started",
            tool_name=contract.name,
            actor=actor,
            metadata=risk_metadata,
        )
        if tool_name == "sandbox-edit":
            response = self._run_sandbox_edit(payload, actor=actor)
        elif tool_name == "agency.http.request":
            response = self._run_http_request(payload, actor=actor)
        elif tool_name == "agency.media.publish":
            response = self._run_media_publish(payload, actor=actor)
        elif tool_name == "agency.media.send":
            response = self._run_media_send(payload, actor=actor)
        elif tool_name == "agency.voice.generate":
            response = self._run_voice_generate(payload, actor=actor)
        elif tool_name == "agency.workflow.list":
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name == "agency.workflow.get":
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name in {
            SYSTEM_GOAL_LIST_TOOL_ID,
            SYSTEM_GOAL_GET_TOOL_ID,
            SYSTEM_GOAL_CREATE_TOOL_ID,
            SYSTEM_GOAL_UPDATE_TOOL_ID,
            SYSTEM_GOAL_PLAN_TOOL_ID,
            SYSTEM_GOAL_REPLAN_TOOL_ID,
            SYSTEM_GOAL_PAUSE_TOOL_ID,
            SYSTEM_GOAL_RESUME_TOOL_ID,
            SYSTEM_GOAL_CANCEL_TOOL_ID,
            SYSTEM_GOAL_EVIDENCE_ATTACH_TOOL_ID,
            SYSTEM_GOAL_EVALUATE_TOOL_ID,
            SYSTEM_GOAL_SUPERVISOR_FINDINGS_TOOL_ID,
            SYSTEM_GOAL_SUPERVISOR_DECISION_RECORD_TOOL_ID,
            SYSTEM_GOAL_COMPLETE_TOOL_ID,
        }:
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name == SYSTEM_AGENT_LIST_TOOL_ID:
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name == SYSTEM_AGENT_GET_TOOL_ID:
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name == "agency.tool.get":
            response = self._run_tool_get(payload, actor=actor)
        elif tool_name in MEMORY_RUNTIME_TOOL_IDS:
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name in GRAPH_RUNTIME_TOOL_IDS:
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name in _optional_module_runtime_tool_ids():
            response = self._runtime_context_required_response(tool_name, actor=actor)
        elif tool_name == "agency.workflow.run":
            response = self._context_required_response(tool_name, actor=actor)
        elif tool_name == "agency.human.ask":
            response = self._run_human_ask(payload, actor=actor)
        elif tool_name in {
            "agency.workflow.propose-create",
            "agency.workflow.propose-update",
            "agency.tool.propose-create",
            "agency.tool.propose-update",
            SYSTEM_AGENT_PROPOSE_UPDATE_TOOL_ID,
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
            metadata={**risk_metadata, "signature": response.signature},
        )
        stored_payload, stored_response = self._redacted_tool_run_record(contract.name, payload, response)
        self.run_store.append(
            tool_name=contract.name,
            tool_version=contract.version,
            actor=actor,
            input_payload=stored_payload,
            output=stored_response,
            risk_labels=risk_metadata["riskLabels"],
            risk_metadata=risk_metadata,
        )
        return response

    async def run_async(self, tool_name: str, payload: dict[str, Any], *, actor: str | None = None) -> ToolRunResponse:
        if tool_name not in {
            "agency.workflow.list",
            "agency.workflow.get",
            SYSTEM_GOAL_LIST_TOOL_ID,
            SYSTEM_GOAL_GET_TOOL_ID,
            SYSTEM_GOAL_CREATE_TOOL_ID,
            SYSTEM_GOAL_UPDATE_TOOL_ID,
            SYSTEM_GOAL_PLAN_TOOL_ID,
            SYSTEM_GOAL_REPLAN_TOOL_ID,
            SYSTEM_GOAL_PAUSE_TOOL_ID,
            SYSTEM_GOAL_RESUME_TOOL_ID,
            SYSTEM_GOAL_CANCEL_TOOL_ID,
            SYSTEM_GOAL_EVIDENCE_ATTACH_TOOL_ID,
            SYSTEM_GOAL_EVALUATE_TOOL_ID,
            SYSTEM_GOAL_SUPERVISOR_FINDINGS_TOOL_ID,
            SYSTEM_GOAL_SUPERVISOR_DECISION_RECORD_TOOL_ID,
            SYSTEM_GOAL_COMPLETE_TOOL_ID,
            SYSTEM_SCHEDULE_LIST_TOOL_ID,
            SYSTEM_SCHEDULE_GET_TOOL_ID,
            SYSTEM_SCHEDULE_CREATE_TOOL_ID,
            SYSTEM_SCHEDULE_UPDATE_TOOL_ID,
            SYSTEM_SCHEDULE_DELETE_TOOL_ID,
            SYSTEM_SCHEDULE_TRIGGER_NOW_TOOL_ID,
            SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_GET_TOOL_ID,
            SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_UPDATE_TOOL_ID,
            SYSTEM_MAIN_AGENT_MONITOR_GET_TOOL_ID,
            SYSTEM_MAIN_AGENT_MONITOR_UPDATE_ROUTES_TOOL_ID,
            SYSTEM_WORKFLOW_MONITORING_EVENTS_TOOL_ID,
            SYSTEM_WORKFLOW_MONITOR_PROPOSAL_DISPATCH_TOOL_ID,
            SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSALS_TOOL_ID,
            SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_CREATE_TOOL_ID,
            SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_UPDATE_TOOL_ID,
            SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_REQUEST_APPROVAL_TOOL_ID,
            SYSTEM_WORKFLOW_GOVERNANCE_AUDIT_TOOL_ID,
            SYSTEM_WORKFLOW_GOVERNANCE_REPAIR_TOOL_ID,
            SYSTEM_WORKFLOW_GOVERNANCE_REMEDIATE_TOOL_ID,
            SYSTEM_WORKFLOW_GOVERNANCE_REVIEW_QUEUE_TOOL_ID,
            SYSTEM_WORKFLOW_GOVERNANCE_ACTION_TOOL_ID,
            SYSTEM_WORKFLOW_GOVERNANCE_DOCUMENT_SUGGEST_TOOL_ID,
            SYSTEM_WORKFLOW_GOVERNANCE_BUNDLE_TOOL_ID,
            SYSTEM_WORKFLOW_STEERING_APPROVALS_TOOL_ID,
            SYSTEM_WORKFLOW_STEERING_APPROVAL_CREATE_TOOL_ID,
            SYSTEM_WORKFLOW_STEERING_APPROVAL_UPDATE_TOOL_ID,
            SYSTEM_WORKFLOW_STEERING_APPROVAL_REQUEST_APPROVAL_TOOL_ID,
            SYSTEM_OBSERVABILITY_WORKFLOW_METRICS_TOOL_ID,
            SYSTEM_OBSERVABILITY_EXECUTION_TIMELINE_TOOL_ID,
            SYSTEM_DOCUMENTS_LIST_TOOL_ID,
            SYSTEM_DOCUMENTS_GET_TOOL_ID,
            SYSTEM_DOCUMENTS_DELETE_TOOL_ID,
            SYSTEM_AGENT_LIST_TOOL_ID,
            SYSTEM_AGENT_GET_TOOL_ID,
            SYSTEM_EXECUTION_LIST_TOOL_ID,
            "agency.execution.get",
            "agency.execution.events",
            "agency.execution.artifacts",
            SYSTEM_EXECUTION_PAUSE_TOOL_ID,
            SYSTEM_EXECUTION_RESUME_TOOL_ID,
            SYSTEM_EXECUTION_CANCEL_TOOL_ID,
            SYSTEM_EXECUTION_APPROVALS_TOOL_ID,
            SYSTEM_EXECUTION_APPROVE_TOOL_ID,
            SYSTEM_EXECUTION_REJECT_TOOL_ID,
            "agency.tool.get",
            SYSTEM_TOOL_WORKSPACE_LIST_TOOL_ID,
            SYSTEM_TOOL_WORKSPACE_SCAFFOLD_TOOL_ID,
            SYSTEM_TOOL_WORKSPACE_PUBLISH_TOOL_ID,
            SYSTEM_WORKFLOW_DOCUMENT_LINKS_TOOL_ID,
            SYSTEM_WORKFLOW_DOCUMENT_LINK_ADD_TOOL_ID,
            SYSTEM_WORKFLOW_DOCUMENT_LINK_DELETE_TOOL_ID,
            SYSTEM_WORKFLOW_DOCUMENT_SUMMARY_TOOL_ID,
            SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACES_TOOL_ID,
            SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_CREATE_TOOL_ID,
            SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_UPDATE_TOOL_ID,
            SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_DELETE_TOOL_ID,
            SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORIES_TOOL_ID,
            SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_ADD_TOOL_ID,
            SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_REMOVE_TOOL_ID,
            "agency.workflow.run",
            *MEMORY_RUNTIME_TOOL_IDS,
            *GRAPH_RUNTIME_TOOL_IDS,
            "agency.http.request",
            "agency.voice.generate",
            "agency.workflow.propose-create",
            "agency.workflow.propose-update",
            "agency.tool.propose-create",
            "agency.tool.propose-update",
            SYSTEM_AGENT_PROPOSE_UPDATE_TOOL_ID,
            *_optional_module_runtime_tool_ids(),
            "agency.media.send",
        }:
            return self.run(tool_name, payload, actor=actor)
        contract = self.contract_registry.get_contract(tool_name)
        if contract is None:
            raise KeyError(f"Tool contract '{tool_name}' not found")
        validate_tool_input(contract, payload)
        started_at = perf_counter()
        risk_metadata = risk_metadata_for_contract_run(contract.name, payload)
        publish_tool_runtime_event(
            lifecycle_type="tool.run.started",
            tool_name=contract.name,
            actor=actor,
            metadata=risk_metadata,
        )
        if tool_name == "agency.workflow.list":
            response = await self._run_workflow_list(actor=actor)
        elif tool_name == "agency.workflow.get":
            response = await self._run_workflow_get(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_LIST_TOOL_ID:
            response = await self._run_goal_list(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_GET_TOOL_ID:
            response = await self._run_goal_get(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_CREATE_TOOL_ID:
            response = await self._run_goal_create(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_UPDATE_TOOL_ID:
            response = await self._run_goal_update(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_PLAN_TOOL_ID:
            response = await self._run_goal_plan(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_REPLAN_TOOL_ID:
            response = await self._run_goal_replan(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_PAUSE_TOOL_ID:
            response = await self._run_goal_pause(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_RESUME_TOOL_ID:
            response = await self._run_goal_resume(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_CANCEL_TOOL_ID:
            response = await self._run_goal_cancel(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_EVIDENCE_ATTACH_TOOL_ID:
            response = await self._run_goal_evidence_attach(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_EVALUATE_TOOL_ID:
            response = await self._run_goal_evaluate(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_SUPERVISOR_FINDINGS_TOOL_ID:
            response = await self._run_goal_supervisor_findings(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_SUPERVISOR_DECISION_RECORD_TOOL_ID:
            response = await self._run_goal_supervisor_decision_record(payload, actor=actor)
        elif tool_name == SYSTEM_GOAL_COMPLETE_TOOL_ID:
            response = await self._run_goal_complete(payload, actor=actor)
        elif tool_name == SYSTEM_SCHEDULE_LIST_TOOL_ID:
            response = await self._run_schedule_list(payload, actor=actor)
        elif tool_name == SYSTEM_SCHEDULE_GET_TOOL_ID:
            response = await self._run_schedule_get(payload, actor=actor)
        elif tool_name == SYSTEM_SCHEDULE_CREATE_TOOL_ID:
            response = await self._run_schedule_create(payload, actor=actor)
        elif tool_name == SYSTEM_SCHEDULE_UPDATE_TOOL_ID:
            response = await self._run_schedule_update(payload, actor=actor)
        elif tool_name == SYSTEM_SCHEDULE_DELETE_TOOL_ID:
            response = await self._run_schedule_delete(payload, actor=actor)
        elif tool_name == SYSTEM_SCHEDULE_TRIGGER_NOW_TOOL_ID:
            response = await self._run_schedule_trigger_now(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_GET_TOOL_ID:
            response = await self._run_workflow_runtime_governance_get(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_UPDATE_TOOL_ID:
            response = await self._run_workflow_runtime_governance_update(payload, actor=actor)
        elif tool_name == SYSTEM_MAIN_AGENT_MONITOR_GET_TOOL_ID:
            response = await self._run_main_agent_monitor_get(actor=actor)
        elif tool_name == SYSTEM_MAIN_AGENT_MONITOR_UPDATE_ROUTES_TOOL_ID:
            response = await self._run_main_agent_monitor_update_routes(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_MONITORING_EVENTS_TOOL_ID:
            response = await self._run_workflow_monitoring_events(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_MONITOR_PROPOSAL_DISPATCH_TOOL_ID:
            response = await self._run_workflow_monitor_proposal_dispatch(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSALS_TOOL_ID:
            response = await self._run_workflow_improvement_proposals(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_CREATE_TOOL_ID:
            response = await self._run_workflow_improvement_proposal_create(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_UPDATE_TOOL_ID:
            response = await self._run_workflow_improvement_proposal_update(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_REQUEST_APPROVAL_TOOL_ID:
            response = await self._run_workflow_improvement_proposal_request_approval(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_GOVERNANCE_AUDIT_TOOL_ID:
            response = await self._run_workflow_governance_audit(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_GOVERNANCE_REPAIR_TOOL_ID:
            response = await self._run_workflow_governance_repair(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_GOVERNANCE_REMEDIATE_TOOL_ID:
            response = await self._run_workflow_governance_remediate(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_GOVERNANCE_REVIEW_QUEUE_TOOL_ID:
            response = await self._run_workflow_governance_review_queue(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_GOVERNANCE_ACTION_TOOL_ID:
            response = await self._run_workflow_governance_action(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_GOVERNANCE_DOCUMENT_SUGGEST_TOOL_ID:
            response = await self._run_workflow_governance_document_suggest(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_GOVERNANCE_BUNDLE_TOOL_ID:
            response = await self._run_workflow_governance_bundle(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_STEERING_APPROVALS_TOOL_ID:
            response = await self._run_workflow_steering_approvals(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_STEERING_APPROVAL_CREATE_TOOL_ID:
            response = await self._run_workflow_steering_approval_create(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_STEERING_APPROVAL_UPDATE_TOOL_ID:
            response = await self._run_workflow_steering_approval_update(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_STEERING_APPROVAL_REQUEST_APPROVAL_TOOL_ID:
            response = await self._run_workflow_steering_approval_request_approval(payload, actor=actor)
        elif tool_name == SYSTEM_OBSERVABILITY_WORKFLOW_METRICS_TOOL_ID:
            response = await self._run_observability_workflow_metrics(payload, actor=actor)
        elif tool_name == SYSTEM_OBSERVABILITY_EXECUTION_TIMELINE_TOOL_ID:
            response = await self._run_observability_execution_timeline(payload, actor=actor)
        elif tool_name == SYSTEM_DOCUMENTS_LIST_TOOL_ID:
            response = await self._run_documents_list(payload, actor=actor)
        elif tool_name == SYSTEM_DOCUMENTS_GET_TOOL_ID:
            response = await self._run_documents_get(payload, actor=actor)
        elif tool_name == SYSTEM_DOCUMENTS_DELETE_TOOL_ID:
            response = await self._run_documents_delete(payload, actor=actor)
        elif tool_name == SYSTEM_AGENT_LIST_TOOL_ID:
            response = await self._run_agent_list(actor=actor)
        elif tool_name == SYSTEM_AGENT_GET_TOOL_ID:
            response = await self._run_agent_get(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_LIST_TOOL_ID:
            response = await self._run_execution_list(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_GET_TOOL_ID:
            response = await self._run_execution_get(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_EVENTS_TOOL_ID:
            response = await self._run_execution_events(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID:
            response = await self._run_execution_artifacts(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_PAUSE_TOOL_ID:
            response = await self._run_execution_pause(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_RESUME_TOOL_ID:
            response = await self._run_execution_resume(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_CANCEL_TOOL_ID:
            response = await self._run_execution_cancel(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_APPROVALS_TOOL_ID:
            response = await self._run_execution_approvals(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_APPROVE_TOOL_ID:
            response = await self._run_execution_approve(payload, actor=actor)
        elif tool_name == SYSTEM_EXECUTION_REJECT_TOOL_ID:
            response = await self._run_execution_reject(payload, actor=actor)
        elif tool_name == "agency.tool.get":
            response = await self._run_tool_get_async(payload, actor=actor)
        elif tool_name == "agency.tool.list":
            response = await self._run_tool_list_async(actor=actor)
        elif tool_name == SYSTEM_TOOL_WORKSPACE_LIST_TOOL_ID:
            response = await self._run_tool_workspace_list(payload, actor=actor)
        elif tool_name == SYSTEM_TOOL_WORKSPACE_SCAFFOLD_TOOL_ID:
            response = await self._run_tool_workspace_scaffold(payload, actor=actor)
        elif tool_name == SYSTEM_TOOL_WORKSPACE_PUBLISH_TOOL_ID:
            response = await self._run_tool_workspace_publish(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_DOCUMENT_LINKS_TOOL_ID:
            response = await self._run_workflow_document_links(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_DOCUMENT_LINK_ADD_TOOL_ID:
            response = await self._run_workflow_document_link_add(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_DOCUMENT_LINK_DELETE_TOOL_ID:
            response = await self._run_workflow_document_link_delete(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_DOCUMENT_SUMMARY_TOOL_ID:
            response = await self._run_workflow_document_summary(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACES_TOOL_ID:
            response = await self._run_workflow_shared_memory_namespaces(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_CREATE_TOOL_ID:
            response = await self._run_workflow_shared_memory_namespace_create(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_UPDATE_TOOL_ID:
            response = await self._run_workflow_shared_memory_namespace_update(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_DELETE_TOOL_ID:
            response = await self._run_workflow_shared_memory_namespace_delete(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORIES_TOOL_ID:
            response = await self._run_workflow_shared_memory_namespace_memories(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_ADD_TOOL_ID:
            response = await self._run_workflow_shared_memory_namespace_memory_add(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_REMOVE_TOOL_ID:
            response = await self._run_workflow_shared_memory_namespace_memory_remove(payload, actor=actor)
        elif tool_name == "agency.workflow.run":
            response = await self._run_workflow_run(payload, actor=actor)
        elif tool_name in {
            "agency.workflow.propose-create",
            "agency.workflow.propose-update",
            "agency.tool.propose-create",
            "agency.tool.propose-update",
            SYSTEM_AGENT_PROPOSE_UPDATE_TOOL_ID,
        }:
            response = await self._run_conversation_proposal(tool_name, payload, actor=actor)
        elif tool_name == SYSTEM_MEMORY_LIST_TOOL_ID:
            response = await self._run_memory_list(payload, actor=actor)
        elif tool_name == SYSTEM_MEMORY_CATALOG_TOOL_ID:
            response = await self._run_memory_catalog(payload, actor=actor)
        elif tool_name == SYSTEM_MEMORY_REMEMBER_TOOL_ID:
            response = await self._run_memory_remember(payload, actor=actor)
        elif tool_name == SYSTEM_MEMORY_UPDATE_TOOL_ID:
            response = await self._run_memory_update(payload, actor=actor)
        elif tool_name == SYSTEM_MEMORY_DELETE_TOOL_ID:
            response = await self._run_memory_delete(payload, actor=actor)
        elif tool_name == SYSTEM_MEMORY_EXCLUSIONS_LIST_TOOL_ID:
            response = await self._run_memory_exclusions_list(payload, actor=actor)
        elif tool_name == SYSTEM_MEMORY_EXCLUSIONS_ADD_TOOL_ID:
            response = await self._run_memory_exclusions_add(payload, actor=actor)
        elif tool_name == SYSTEM_MEMORY_EXCLUSIONS_DELETE_TOOL_ID:
            response = await self._run_memory_exclusions_delete(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_MEMORY_LINKS_LIST_TOOL_ID:
            response = await self._run_workflow_memory_links_list(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_MEMORY_LINKS_ADD_TOOL_ID:
            response = await self._run_workflow_memory_links_add(payload, actor=actor)
        elif tool_name == SYSTEM_WORKFLOW_MEMORY_LINKS_DELETE_TOOL_ID:
            response = await self._run_workflow_memory_links_delete(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_CONTEXT_TOOL_ID:
            response = await self._run_graph_context(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_SEARCH_TOOL_ID:
            response = await self._run_graph_search(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_EXPAND_TOOL_ID:
            response = await self._run_graph_expand(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_NEIGHBORS_TOOL_ID:
            response = await self._run_graph_neighbors(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_PATH_TOOL_ID:
            response = await self._run_graph_path(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_SUMMARIZE_SUBGRAPH_TOOL_ID:
            response = await self._run_graph_summarize_subgraph(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID:
            response = await self._run_graph_working_set_create(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID:
            response = await self._run_graph_working_set_add(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID:
            response = await self._run_graph_working_set_remove(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID:
            response = await self._run_graph_working_set_summarize(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID:
            response = await self._run_graph_working_set_clear(payload, actor=actor)
        elif tool_name == SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID:
            response = await self._run_graph_working_set_persist_context_pack(payload, actor=actor)
        elif tool_name == "agency.http.request":
            response = await self._run_http_request_async(payload, actor=actor)
        elif tool_name == "agency.media.send":
            response = await self._run_media_send_async(payload, actor=actor)
        elif tool_name in _optional_module_runtime_tool_ids():
            module_key = optional_module_key_for_runtime_tool(tool_name)
            if module_key is None:
                response = self._module_disabled_response(tool_name, actor=actor, module_key="optional_module")
            else:
                response = await self._run_optional_module_tool(module_key, tool_name, payload, actor=actor)
        else:
            response = self.run(tool_name, payload, actor=actor)
            return response
        validate_tool_output(contract, response.model_dump(mode="json"))
        duration_ms = _duration_ms(started_at)
        if tool_name in GRAPH_RUNTIME_TOOL_IDS:
            self._record_graph_tool_observability(
                tool_name,
                response.result if isinstance(response.result, dict) else {},
                actor=actor,
                duration_ms=duration_ms,
                verdict=response.verdict,
            )
        publish_tool_runtime_event(
            lifecycle_type="tool.run.completed",
            tool_name=contract.name,
            actor=actor,
            verdict=response.verdict,
            metadata={**risk_metadata, "signature": response.signature, "duration_ms": duration_ms},
        )
        stored_payload, stored_response = self._redacted_tool_run_record(contract.name, payload, response)
        self.run_store.append(
            tool_name=contract.name,
            tool_version=contract.version,
            actor=actor,
            input_payload=stored_payload,
            output=stored_response,
            risk_labels=risk_metadata["riskLabels"],
            risk_metadata=risk_metadata,
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

    async def _run_http_request_async(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        onecli_agent_token_context = None
        if self.context is not None:
            onecli_agent_token_context = await OneCLIIdentityMappingService(self.context).resolve_agent_token_context(
                owner_user_id=actor,
            )
        return self._run_http_request(
            payload,
            actor=actor,
            onecli_agent_token_context=onecli_agent_token_context,
        )

    def _run_http_request(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
            onecli_agent_token_secret_ref: str | None = None,
            onecli_agent_token_context: dict[str, Any] | None = None,
    ) -> ToolRunResponse:
        settings = get_settings()
        agent_token_secret_ref = onecli_agent_token_secret_ref
        agent_identity: dict[str, Any] = {
            "mapping": "none",
            "agency_actor": actor,
            "agent_token_secret_ref_configured": False,
        }
        if onecli_agent_token_context is not None:
            value = onecli_agent_token_context.get("agent_token_secret_ref")
            agent_token_secret_ref = value if isinstance(value, str) else None
            agent_identity = {
                "mapping": str(onecli_agent_token_context.get("source") or "none"),
                "agency_actor": actor,
                "mapping_id": onecli_agent_token_context.get("mapping_id"),
                "onecli_agent_id": onecli_agent_token_context.get("onecli_agent_id"),
                "owner_user_id": onecli_agent_token_context.get("owner_user_id"),
                "workflow_id": onecli_agent_token_context.get("workflow_id"),
                "agent_token_secret_ref_configured": bool(agent_token_secret_ref),
            }
        elif agent_token_secret_ref is not None:
            agent_identity = {
                "mapping": "server_configured_agent_token",
                "agency_actor": actor,
                "agent_token_secret_ref_configured": bool(agent_token_secret_ref),
            }
        elif settings.onecli_allow_global_agent_token_fallback and settings.onecli_agent_token_secret_ref:
            agent_token_secret_ref = settings.onecli_agent_token_secret_ref
            agent_identity = {
                "mapping": "development_global_fallback",
                "agency_actor": actor,
                "agent_token_secret_ref_configured": True,
            }
        credential_mode = str(payload.get("credential_mode") or "").strip().lower()
        if not credential_mode:
            credential_mode = "onecli" if settings.onecli_force_for_http_tools else "none"
        onecli_correlation_id = f"onecli-http:{uuid4()}" if credential_mode == "onecli" else None
        onecli_metadata = None
        if credential_mode == "onecli":
            onecli_metadata = self._onecli_http_metadata(
                url=str(payload.get("url") or ""),
                actor=actor,
                gateway_url=settings.onecli_gateway_url,
                agent_token_secret_ref_configured=bool(agent_token_secret_ref),
                correlation_id=onecli_correlation_id,
                agent_identity=agent_identity,
            )
        effective_payload = {**payload, "credential_mode": credential_mode}
        policy_verdict = self.policy_engine.evaluate("agency.http.request", effective_payload, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="agency.http.request",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        if policy_verdict.outcome == "deny":
            if onecli_metadata is not None:
                self._publish_onecli_http_event(
                    lifecycle_type="onecli.http.request.denied",
                    actor=actor,
                    verdict="deny",
                    onecli_metadata=onecli_metadata,
                    extra={
                        "denial_reasons": [
                            rule.reason for rule in policy_verdict.rules if rule.outcome == "deny" and rule.reason
                        ],
                    },
                )
            return build_tool_run_response(
                verdict="deny",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[rule.reason for rule in policy_verdict.rules if rule.outcome == "deny" and rule.reason],
                dry_run=False,
                actor=actor,
            )

        if credential_mode == "onecli" and settings.onecli_external_calls_disabled:
            if onecli_metadata is not None:
                self._publish_onecli_http_event(
                    lifecycle_type="onecli.http.request.denied",
                    actor=actor,
                    verdict="deny",
                    onecli_metadata=onecli_metadata,
                    extra={"denial_reasons": ["ONECLI_EXTERNAL_CALLS_DISABLED is true"]},
                )
            return build_tool_run_response(
                verdict="deny",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=["OneCLI-routed external calls are disabled by ONECLI_EXTERNAL_CALLS_DISABLED."],
                dry_run=False,
                actor=actor,
            )

        if credential_mode == "onecli" and not settings.onecli_enabled:
            if onecli_metadata is not None:
                self._publish_onecli_http_event(
                    lifecycle_type="onecli.http.request.denied",
                    actor=actor,
                    verdict="deny",
                    onecli_metadata=onecli_metadata,
                    extra={"denial_reasons": ["ONECLI_ENABLED is false"]},
                )
            return build_tool_run_response(
                verdict="deny",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=["OneCLI credential mode requested, but ONECLI_ENABLED is false."],
                dry_run=False,
                actor=actor,
            )

        try:
            proxies = None
            ca_bundle_path = None
            if credential_mode == "onecli":
                if onecli_metadata is not None:
                    self._publish_onecli_http_event(
                        lifecycle_type="onecli.http.request.started",
                        actor=actor,
                        verdict=None,
                        onecli_metadata=onecli_metadata,
                    )
                proxy_url = build_onecli_proxy_url(
                    settings.onecli_gateway_url,
                    agent_token_secret_ref,
                )
                proxies = {
                    "http": proxy_url,
                    "https": proxy_url,
                }
                ca_bundle_path = settings.onecli_gateway_ca_bundle_path
            result = execute_custom_api(
                url=str(payload["url"]),
                method=str(payload["method"]),
                headers=self._http_headers_with_onecli_correlation(
                    payload.get("headers"),
                    onecli_metadata=onecli_metadata,
                    actor=actor,
                ),
                query_params=payload.get("query_params"),
                body=payload.get("body"),
                verify_ssl=bool(payload.get("verify_ssl", True)),
                proxies=proxies,
                ca_bundle_path=ca_bundle_path,
                credential_mode=credential_mode,
                emit_onecli_events=False,
            )
            if onecli_metadata is not None:
                status_code = result.get("status_code")
                lifecycle_type = (
                    "onecli.http.request.rate_limited"
                    if status_code == 429
                    else "onecli.http.request.completed"
                )
                self._publish_onecli_http_event(
                    lifecycle_type=lifecycle_type,
                    actor=actor,
                    verdict="warn" if status_code == 429 else policy_verdict.outcome,
                    onecli_metadata=onecli_metadata,
                    extra={"status_code": status_code},
                )
            result = {
                **result,
                "method": str(payload["method"]).upper(),
                "url": str(payload["url"]),
                "credential_mode": credential_mode,
            }
            if credential_mode == "onecli":
                result["onecli"] = self._onecli_http_metadata(
                    url=str(payload["url"]),
                    actor=actor,
                    gateway_url=settings.onecli_gateway_url,
                    agent_token_secret_ref_configured=bool(agent_token_secret_ref),
                    correlation_id=onecli_correlation_id,
                    agent_identity=agent_identity,
                )
            return build_tool_run_response(
                verdict=policy_verdict.outcome,
                policy_verdict=policy_verdict,
                result=result,
                patch=None,
                dry_run=False,
                actor=actor,
            )
        except Exception as exc:
            if onecli_metadata is not None:
                fail_closed = credential_mode == "onecli" and settings.app_env == "production"
                self._publish_onecli_http_event(
                    lifecycle_type="onecli.http.request.failed",
                    actor=actor,
                    verdict="deny" if fail_closed else "warn",
                    onecli_metadata=onecli_metadata,
                    extra={"error_type": exc.__class__.__name__, "fail_closed": fail_closed},
                )
            else:
                fail_closed = False
            return build_tool_run_response(
                verdict="deny" if fail_closed else "warn",
                policy_verdict=policy_verdict,
                result=None,
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )

    def _onecli_http_metadata(
            self,
            *,
            url: str,
            actor: str | None,
            gateway_url: str,
            agent_token_secret_ref_configured: bool,
            correlation_id: str | None = None,
            agent_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parsed = urlparse(url)
        return {
            "correlation_id": correlation_id,
            "gateway_mode": "proxy",
            "gateway_url": gateway_url,
            "target_scheme": parsed.scheme,
            "target_host": parsed.hostname or "",
            "target_port": parsed.port,
            "agent_token_secret_ref_configured": agent_token_secret_ref_configured,
            "agency_context": {
                "execution_id": None,
                "workflow_id": None,
                "task_id": None,
                "agent_id": None,
                "tool_call_id": None,
            },
            "forwarded_headers": sorted([
                "X-Agency-OneCLI-Correlation-ID",
                "X-Agency-User-ID",
            ]),
            "agent_identity": agent_identity or {
                "mapping": "server_configured_agent_token",
                "agency_actor": actor,
                "agent_token_secret_ref_configured": agent_token_secret_ref_configured,
            },
        }

    def _http_headers_with_onecli_correlation(
            self,
            headers: Any,
            *,
            onecli_metadata: dict[str, Any] | None,
            actor: str | None,
    ) -> dict[str, Any] | None:
        if onecli_metadata is None:
            return headers
        base_headers = headers if isinstance(headers, dict) else {}
        return {
            **base_headers,
            **build_onecli_correlation_headers(onecli_metadata, actor=actor),
        }

    def _publish_onecli_http_event(
            self,
            *,
            lifecycle_type: str,
            actor: str | None,
            verdict: str | None,
            onecli_metadata: dict[str, Any],
            extra: dict[str, Any] | None = None,
    ) -> None:
        publish_tool_runtime_event(
            lifecycle_type=lifecycle_type,
            tool_name="agency.http.request",
            actor=actor,
            verdict=verdict,
            metadata={
                **onecli_metadata,
                **(extra or {}),
            },
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

    async def _run_tool_list_async(self, *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate("agency.tool.list", {}, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name="agency.tool.list",
            actor=actor,
            verdict=policy_verdict.outcome,
            metadata={"policyScore": policy_verdict.score},
        )
        tools = [tool.model_dump(mode="json") for tool in await self._visible_tool_definitions_async()]
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"items": tools, "count": len(tools)},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_media_send_async(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        if self.context is None:
            return self._run_media_send(payload, actor=actor)
        from app.tools.implementations.media import send_media_with_context

        policy_verdict = self.policy_engine.evaluate("agency.media.send", payload, actor=actor)
        self._publish_policy_event("agency.media.send", policy_verdict, actor=actor)
        try:
            result = await send_media_with_context(self.context, **payload)
        except Exception as exc:
            error = str(exc)
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=bool(payload.get("dry_run", True)),
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome if result.get("status") != "failed" else "warn",
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            dry_run=result.get("status") == "preview",
            actor=actor,
        )

    def _run_media_publish(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        from app.tools.implementations.media import publish_media

        policy_verdict = self.policy_engine.evaluate("agency.media.publish", payload, actor=actor)
        self._publish_policy_event("agency.media.publish", policy_verdict, actor=actor)
        try:
            result = publish_media(**payload)
        except Exception as exc:
            error = str(exc)
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=bool(payload.get("dry_run", True)),
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            dry_run=result.get("status") == "preview",
            actor=actor,
        )

    def _run_media_send(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        from app.tools.implementations.media import send_media

        policy_verdict = self.policy_engine.evaluate("agency.media.send", payload, actor=actor)
        self._publish_policy_event("agency.media.send", policy_verdict, actor=actor)
        try:
            result = send_media(**payload)
        except Exception as exc:
            error = str(exc)
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=bool(payload.get("dry_run", True)),
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome if result.get("status") != "requires_context" else "warn",
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            dry_run=result.get("status") == "preview",
            actor=actor,
        )

    def _run_voice_generate(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        from app.tools.implementations.voice import generate_voice

        policy_verdict = self.policy_engine.evaluate("agency.voice.generate", payload, actor=actor)
        self._publish_policy_event("agency.voice.generate", policy_verdict, actor=actor)
        try:
            result = generate_voice(**payload)
        except Exception as exc:
            error = str(exc)
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=bool(payload.get("dry_run", True)),
                actor=actor,
            )
        verdict = policy_verdict.outcome if result.get("status") != "setup_required" else "warn"
        return build_tool_run_response(
            verdict=verdict,
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            dry_run=result.get("status") == "preview",
            actor=actor,
        )

    async def _run_optional_module_tool(
            self,
            module_key: str,
            tool_name: str,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        handler_class = optional_module_runtime_tool_handler_class(module_key)
        if handler_class is None:
            return self._module_disabled_response(tool_name, actor=actor, module_key=module_key)
        handler = handler_class(
            context=self.context,
            policy_engine=self.policy_engine,
            current_user_loader=self._current_user,
            publish_policy_event=lambda name, verdict: self._publish_policy_event(name, verdict, actor=actor),
        )
        return await handler.run(tool_name, payload, actor=actor)

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
        tool = resolve_tool(tool_id, self._visible_tool_definitions())
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

    def _visible_tool_definitions(self) -> list[Any]:
        return list_builtin_tool_definitions()

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
            elif tool_name == SYSTEM_AGENT_PROPOSE_UPDATE_TOOL_ID:
                result = await service._create_agent_update_proposal(
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
                errors=[] if result.get("status") == "received" else [
                    "Human input timed out before a reply was received."],
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

    def _module_disabled_response(self, tool_name: str, *, actor: str | None, module_key: str) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(tool_name, {}, actor=actor)
        publish_tool_runtime_event(
            lifecycle_type="tool.policy.completed",
            tool_name=tool_name,
            actor=actor,
            verdict="deny",
            metadata={"policyScore": policy_verdict.score, "module": module_key},
        )
        message = f"{module_key.replace('_', '-')} module is disabled by backend configuration."
        return build_tool_run_response(
            verdict="deny",
            policy_verdict=policy_verdict,
            result={"status": "module_disabled", "module": module_key, "error": message},
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

    async def _run_goal_list(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_GOAL_LIST_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await GoalService(self.context).list_goals(
                status=_optional_string(payload.get("status")),
                parent_goal_id=_optional_string(payload.get("parent_goal_id")),
                active_only=bool(payload.get("active_only")),
            )
        except ValueError as exc:
            return self._goal_error_response(tool_name, policy_verdict, str(exc), actor=actor, dry_run=True)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_goal_get(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_GOAL_GET_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        goal_id = str(payload.get("goal_id") or "")
        try:
            goal = await GoalService(self.context).get_goal(goal_id)
        except GoalNotFoundError as exc:
            return self._goal_error_response(tool_name, policy_verdict, str(exc), actor=actor, dry_run=True)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "goal": goal.model_dump(mode="json")},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_goal_create(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_GOAL_CREATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        create_payload = {
            "objective": payload.get("objective"),
            "priority": payload.get("priority") or "normal",
            "owner_actor": actor,
            "parent_goal_id": _optional_string(payload.get("parent_goal_id")),
            "success_criteria": payload.get("success_criteria") if isinstance(payload.get("success_criteria"),
                                                                              list) else [],
            "constraints": payload.get("constraints") if isinstance(payload.get("constraints"), dict) else {},
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        }
        try:
            goal = await GoalService(self.context).create_goal(create_payload)
        except ValueError as exc:
            return self._goal_error_response(tool_name, policy_verdict, str(exc), actor=actor, dry_run=False)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "goal": goal.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_goal_update(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_GOAL_UPDATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        goal_id = str(payload.get("goal_id") or "")
        patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
        try:
            goal = await GoalService(self.context).update_goal(goal_id, patch)
        except (GoalNotFoundError, ValueError) as exc:
            return self._goal_error_response(tool_name, policy_verdict, str(exc), actor=actor, dry_run=False)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "goal": goal.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_goal_plan(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_GOAL_PLAN_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        goal_id = str(payload.get("goal_id") or "")
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
        reason = str(payload.get("reason") or "initial_plan")
        try:
            goal = await GoalService(self.context).plan_goal(goal_id, plan=plan, reason=reason, actor=actor)
        except (GoalNotFoundError, GoalTransitionError, ValueError) as exc:
            return self._goal_error_response(tool_name, policy_verdict, str(exc), actor=actor, dry_run=False)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "goal": goal.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_goal_replan(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_GOAL_REPLAN_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        goal_id = str(payload.get("goal_id") or "")
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
        reason = str(payload.get("reason") or "")
        try:
            goal = await GoalService(self.context).replan_goal(goal_id, plan=plan, reason=reason, actor=actor)
        except (GoalNotFoundError, GoalTransitionError, ValueError) as exc:
            return self._goal_error_response(tool_name, policy_verdict, str(exc), actor=actor, dry_run=False)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "goal": goal.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_goal_pause(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        return await self._run_goal_lifecycle(
            SYSTEM_GOAL_PAUSE_TOOL_ID,
            payload,
            actor=actor,
            action=lambda service, goal_id: service.pause_goal(goal_id),
        )

    async def _run_goal_resume(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        return await self._run_goal_lifecycle(
            SYSTEM_GOAL_RESUME_TOOL_ID,
            payload,
            actor=actor,
            action=lambda service, goal_id: service.resume_goal(goal_id),
        )

    async def _run_goal_cancel(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        return await self._run_goal_lifecycle(
            SYSTEM_GOAL_CANCEL_TOOL_ID,
            payload,
            actor=actor,
            action=lambda service, goal_id: service.cancel_goal(goal_id,
                                                                reason=_optional_string(payload.get("reason"))),
        )

    async def _run_goal_evidence_attach(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_GOAL_EVIDENCE_ATTACH_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        goal_id = str(payload.get("goal_id") or "")
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
        try:
            goal = await GoalService(self.context).attach_evidence(goal_id, evidence)
        except (GoalNotFoundError, ValueError) as exc:
            return self._goal_error_response(tool_name, policy_verdict, str(exc), actor=actor, dry_run=False)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "goal": goal.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_goal_evaluate(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_GOAL_EVALUATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        goal_id = str(payload.get("goal_id") or "")
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else None
        persist = payload.get("persist")
        try:
            evaluation = await GoalService(self.context).evaluate_goal(
                goal_id,
                evidence=evidence,
                persist=True if persist is None else bool(persist),
            )
        except (GoalNotFoundError, ValueError) as exc:
            return self._goal_error_response(tool_name, policy_verdict, str(exc), actor=actor, dry_run=False)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "goal_id": goal_id, "evaluation": evaluation},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_goal_supervisor_findings(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_GOAL_SUPERVISOR_FINDINGS_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        goal_id = str(payload.get("goal_id") or "")
        try:
            result = await GoalService(self.context).list_supervisor_findings(goal_id)
        except GoalNotFoundError as exc:
            return self._goal_error_response(tool_name, policy_verdict, str(exc), actor=actor, dry_run=True)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_goal_supervisor_decision_record(self, payload: dict[str, Any], *,
                                                   actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_GOAL_SUPERVISOR_DECISION_RECORD_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        goal_id = str(payload.get("goal_id") or "")
        decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
        try:
            goal = await GoalService(self.context).record_supervisor_decision(goal_id, decision, actor=actor)
        except (GoalNotFoundError, ValueError) as exc:
            return self._goal_error_response(tool_name, policy_verdict, str(exc), actor=actor, dry_run=False)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "goal": goal.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_goal_complete(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
        evaluation = payload.get("evaluation") if isinstance(payload.get("evaluation"), dict) else None
        return await self._run_goal_lifecycle(
            SYSTEM_GOAL_COMPLETE_TOOL_ID,
            payload,
            actor=actor,
            action=lambda service, goal_id: service.complete_goal(
                goal_id,
                evidence=evidence,
                evaluation=evaluation,
            ),
        )

    async def _run_goal_lifecycle(
            self,
            tool_name: str,
            payload: dict[str, Any],
            *,
            actor: str | None,
            action,
    ) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        goal_id = str(payload.get("goal_id") or "")
        try:
            goal = await action(GoalService(self.context), goal_id)
        except (GoalNotFoundError, GoalTransitionError, ValueError) as exc:
            return self._goal_error_response(tool_name, policy_verdict, str(exc), actor=actor, dry_run=False)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "goal": goal.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    @staticmethod
    def _goal_error_response(
            tool_name: str,
            policy_verdict,
            error: str,
            *,
            actor: str | None,
            dry_run: bool,
    ) -> ToolRunResponse:
        return build_tool_run_response(
            verdict="warn",
            policy_verdict=policy_verdict,
            result={"status": "error", "error": error},
            patch=None,
            errors=[error],
            dry_run=dry_run,
            actor=actor,
        )

    async def _run_agent_list(self, *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_AGENT_LIST_TOOL_ID, {}, actor=actor)
        self._publish_policy_event(SYSTEM_AGENT_LIST_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_AGENT_LIST_TOOL_ID, actor=actor)
        agents = await self.context.agent_repo.list()
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
                "status": "ok",
                "agents": [
                    {
                        "id": item.id,
                        "name": item.name,
                        "display_name": item.display_name,
                        "description": item.description,
                        "role": item.role,
                        "model_profile_id": item.model_profile_id,
                        "tool_ids": item.tool_ids,
                        "handoff_agent_ids": item.handoff_agent_ids,
                    }
                    for item in agents
                ],
            },
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_schedule_list(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_SCHEDULE_LIST_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        workflow_id = _optional_string(payload.get("workflow_id"))
        enabled_filter = payload.get("enabled") if "enabled" in payload else None
        limit = _bounded_int(payload.get("limit") or 50, minimum=1, maximum=200)
        schedules = await self.context.schedule_repo.list()
        items = []
        for schedule in schedules:
            if workflow_id and schedule.workflow_id != workflow_id:
                continue
            if enabled_filter is not None and bool(schedule.enabled) != bool(enabled_filter):
                continue
            items.append(schedule.model_dump(mode="json"))
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "items": items[:limit], "count": min(len(items), limit)},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_schedule_get(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_SCHEDULE_GET_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        schedule_id = str(payload.get("schedule_id") or "")
        schedule = await self.context.schedule_repo.get(schedule_id)
        if schedule is None:
            error = f"Schedule '{schedule_id}' was not found."
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "schedule": schedule.model_dump(mode="json")},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_schedule_create(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_SCHEDULE_CREATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            created = await ScheduleService(self.context).create_schedule(payload.get("schedule") or {})
        except Exception as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "schedule": created.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_schedule_update(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_SCHEDULE_UPDATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        schedule_id = str(payload.get("schedule_id") or "")
        try:
            updated = await ScheduleService(self.context).patch_schedule(
                schedule_id,
                payload.get("patch") if isinstance(payload.get("patch"), dict) else {},
            )
        except Exception as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        if updated is None:
            error = f"Schedule '{schedule_id}' was not found."
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "schedule": updated.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_schedule_delete(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_SCHEDULE_DELETE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        schedule_id = str(payload.get("schedule_id") or "")
        deleted = await self.context.schedule_repo.soft_delete(schedule_id)
        if not deleted:
            error = f"Schedule '{schedule_id}' was not found."
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "deleted": True, "schedule_id": schedule_id},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_schedule_trigger_now(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_SCHEDULE_TRIGGER_NOW_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        schedule_id = str(payload.get("schedule_id") or "")
        try:
            result = await ScheduleService(self.context).trigger_now(schedule_id)
        except (ValueError, ScheduleConcurrencyError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
                "status": "ok",
                "schedule": result.schedule.model_dump(mode="json"),
                "execution_id": result.execution_id,
                "triggered_at": result.triggered_at.isoformat(),
                "metadata": result.metadata,
            },
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_runtime_governance_get(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_GET_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
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
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
                "status": "ok",
                "workflow_id": workflow.id,
                "runtime_governance": WorkflowService(self.context).runtime_governance_operator_payload(workflow),
            },
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_runtime_governance_update(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_UPDATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).update_runtime_governance_controls(
                str(payload.get("workflow_id") or ""),
                payload.get("patch") if isinstance(payload.get("patch"), dict) else {},
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_main_agent_monitor_get(self, *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_MAIN_AGENT_MONITOR_GET_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, {}, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        result = await WorkflowService(self.context).main_agent_monitor_command_center()
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_main_agent_monitor_update_routes(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_MAIN_AGENT_MONITOR_UPDATE_ROUTES_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).update_main_agent_monitor_routes(
                payload.get("patch") if isinstance(payload.get("patch"), dict) else {},
            )
        except ValueError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_monitoring_events(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_MONITORING_EVENTS_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).workflow_monitoring_events(
                str(payload.get("workflow_id") or ""))
        except WorkflowNotFoundError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_monitor_proposal_dispatch(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_MONITOR_PROPOSAL_DISPATCH_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).dispatch_monitor_proposal_to_main_agent(
                str(payload.get("workflow_id") or ""),
                str(payload.get("proposal_event_id") or ""),
                actor_user_id=actor or "contract-runtime",
                operator_note=_optional_string(payload.get("operator_note")),
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_improvement_proposals(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSALS_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).workflow_improvement_proposals(
                str(payload.get("workflow_id") or ""),
                proposal_id=_optional_string(payload.get("proposal_id")),
                status=_optional_string(payload.get("status")),
                limit=payload.get("limit"),
            )
        except WorkflowNotFoundError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_improvement_proposal_create(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_CREATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).create_workflow_improvement_proposal(
                str(payload.get("workflow_id") or ""),
                payload,
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_improvement_proposal_update(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_UPDATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).update_workflow_improvement_proposal(
                str(payload.get("workflow_id") or ""),
                str(payload.get("proposal_id") or ""),
                payload.get("patch") if isinstance(payload.get("patch"), dict) else {},
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_improvement_proposal_request_approval(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_REQUEST_APPROVAL_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        if current_user is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).request_workflow_improvement_proposal_approval(
                str(payload.get("workflow_id") or ""),
                str(payload.get("proposal_id") or ""),
                actor_user_id=current_user.id,
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_governance_audit(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_GOVERNANCE_AUDIT_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).workflow_governance_audit(
                str(payload.get("workflow_id") or ""),
            )
        except WorkflowNotFoundError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_governance_repair(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_GOVERNANCE_REPAIR_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).repair_workflow_governance_record(
                str(payload.get("workflow_id") or ""),
                record_kind=str(payload.get("record_kind") or ""),
                record_id=str(payload.get("record_id") or ""),
                action=str(payload.get("action") or ""),
                approval_request_id=_optional_string(payload.get("approval_request_id")),
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_governance_remediate(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_GOVERNANCE_REMEDIATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        dry_run = bool(payload.get("dry_run"))
        try:
            result = await WorkflowService(self.context).remediate_workflow_governance(
                str(payload.get("workflow_id") or ""),
                dry_run=dry_run,
                sync_status_mismatches=payload.get("sync_status_mismatches"),
                clear_orphaned_references=payload.get("clear_orphaned_references"),
                adopt_orphaned_approvals=payload.get("adopt_orphaned_approvals"),
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=dry_run,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=dry_run,
            actor=actor,
        )

    async def _run_workflow_governance_review_queue(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_GOVERNANCE_REVIEW_QUEUE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).workflow_governance_review_queue(
                str(payload.get("workflow_id") or ""),
                limit=payload.get("limit"),
            )
        except WorkflowNotFoundError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_governance_action(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_GOVERNANCE_ACTION_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        if current_user is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).execute_workflow_governance_action(
                str(payload.get("workflow_id") or ""),
                action=str(payload.get("action") or ""),
                actor_user_id=current_user.id,
                record_kind=_optional_string(payload.get("record_kind")),
                record_id=_optional_string(payload.get("record_id")),
                document_id=_optional_string(payload.get("document_id")),
                label=_optional_string(payload.get("label")),
                summary=_optional_string(payload.get("summary")),
                linked_by=_optional_string(payload.get("linked_by")),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
                sync_status_mismatches=payload.get("sync_status_mismatches"),
                clear_orphaned_references=payload.get("clear_orphaned_references"),
                adopt_orphaned_approvals=payload.get("adopt_orphaned_approvals"),
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_governance_document_suggest(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_GOVERNANCE_DOCUMENT_SUGGEST_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        if current_user is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).suggest_workflow_governance_documents(
                str(payload.get("workflow_id") or ""),
                actor_user_id=current_user.id,
                record_kind=str(payload.get("record_kind") or ""),
                record_id=str(payload.get("record_id") or ""),
                limit=payload.get("limit"),
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_governance_bundle(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_GOVERNANCE_BUNDLE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        if current_user is None:
            return self._context_required_response(tool_name, actor=actor)
        dry_run = bool(payload.get("dry_run"))
        try:
            result = await WorkflowService(self.context).execute_workflow_governance_bundle(
                str(payload.get("workflow_id") or ""),
                actor_user_id=current_user.id,
                record_kind=str(payload.get("record_kind") or ""),
                record_id=str(payload.get("record_id") or ""),
                attach_top_suggestion=payload.get("attach_top_suggestion"),
                request_approval=payload.get("request_approval"),
                document_limit=payload.get("document_limit"),
                evidence_label=_optional_string(payload.get("evidence_label")),
                evidence_summary=_optional_string(payload.get("evidence_summary")),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
                dry_run=dry_run,
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=dry_run,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=dry_run,
            actor=actor,
        )

    async def _run_workflow_steering_approvals(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_STEERING_APPROVALS_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).workflow_steering_approvals(
                str(payload.get("workflow_id") or ""),
                approval_id=_optional_string(payload.get("approval_id")),
                status=_optional_string(payload.get("status")),
                limit=payload.get("limit"),
            )
        except WorkflowNotFoundError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_steering_approval_create(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_STEERING_APPROVAL_CREATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).create_workflow_steering_approval(
                str(payload.get("workflow_id") or ""),
                payload,
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_steering_approval_update(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_STEERING_APPROVAL_UPDATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).update_workflow_steering_approval(
                str(payload.get("workflow_id") or ""),
                str(payload.get("approval_id") or ""),
                payload.get("patch") if isinstance(payload.get("patch"), dict) else {},
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_steering_approval_request_approval(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_STEERING_APPROVAL_REQUEST_APPROVAL_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        if current_user is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await WorkflowService(self.context).request_workflow_steering_approval(
                str(payload.get("workflow_id") or ""),
                str(payload.get("approval_id") or ""),
                actor_user_id=current_user.id,
            )
        except (WorkflowNotFoundError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_observability_workflow_metrics(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_OBSERVABILITY_WORKFLOW_METRICS_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        result = await ObservabilityService(self.context).get_workflow_metrics(str(payload.get("workflow_id") or ""))
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "metrics": result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_observability_execution_timeline(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_OBSERVABILITY_EXECUTION_TIMELINE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        try:
            result = await ObservabilityService(self.context).get_execution_timeline(
                str(payload.get("execution_id") or ""))
        except ExecutionNotFoundError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "timeline": result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_documents_list(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_DOCUMENTS_LIST_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        if current_user is None:
            return self._context_required_response(tool_name, actor=actor)
        repo = getattr(self.context, "uploaded_document_repo", None)
        if repo is None or not hasattr(repo, "query"):
            result = {"status": "ok", "items": [], "count": 0}
        else:
            items = await repo.query(
                conversation_id=_optional_string(payload.get("conversation_id")),
                workflow_id=_optional_string(payload.get("workflow_id")),
                agent_id=_optional_string(payload.get("agent_id")),
                user_id=current_user.id,
                scope=_optional_string(payload.get("scope")),
                upload_mode=_optional_string(payload.get("upload_mode")),
                limit=_bounded_int(payload.get("limit") or 50, minimum=1, maximum=100),
            )
            result = {"status": "ok", "items": [item.model_dump(mode="json") for item in items], "count": len(items)}
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_documents_get(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_DOCUMENTS_GET_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        if current_user is None:
            return self._context_required_response(tool_name, actor=actor)
        repo = getattr(self.context, "uploaded_document_repo", None)
        document_id = str(payload.get("document_id") or "")
        item = await repo.get(document_id) if repo is not None and hasattr(repo, "get") else None
        if item is None or item.created_by_user_id != current_user.id:
            error = f"Uploaded document '{document_id}' was not found."
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "document": item.model_dump(mode="json")},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_documents_delete(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_DOCUMENTS_DELETE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        if current_user is None:
            return self._context_required_response(tool_name, actor=actor)
        document_id = str(payload.get("document_id") or "")
        try:
            result = await DocumentIngestionService(self.context).delete_uploaded_document(
                document_id,
                current_user=current_user,
            )
        except MemoryPermissionError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        if result is None:
            error = f"Uploaded document '{document_id}' was not found."
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": error},
                patch=None,
                errors=[error],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
                "status": "ok",
                "deleted": True,
                "document_id": result.document_id,
                "upload_mode": result.upload_mode,
                "document_status": result.document_status,
                "memory_ids": result.deleted_memory_ids,
                "deleted_memory_count": len(result.deleted_memory_ids),
            },
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_agent_get(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_AGENT_GET_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_AGENT_GET_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_AGENT_GET_TOOL_ID, actor=actor)
        agent_id = str(payload.get("agent_id") or "")
        agent = await self.context.agent_repo.get(agent_id)
        if agent is None:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": f"Agent '{agent_id}' was not found."},
                patch=None,
                errors=[f"Agent '{agent_id}' was not found."],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "agent": agent.model_dump(mode="json")},
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
        settings = get_settings()
        execution_payload["stale_classification"] = classify_execution_staleness(
            execution,
            stale_after_seconds=settings.main_agent_workflow_monitor_stale_after_seconds,
            idle_timeout_seconds=settings.agent_activity_idle_timeout_seconds,
            run_timeout_seconds=settings.agent_run_timeout_seconds,
        )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "execution": execution_payload},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_execution_list(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_EXECUTION_LIST_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_EXECUTION_LIST_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_EXECUTION_LIST_TOOL_ID, actor=actor)

        workflow_id = _optional_string(payload.get("workflow_id"))
        agent_id = _optional_string(payload.get("agent_id"))
        active_only = bool(payload.get("active_only"))
        limit = max(1, min(int(payload.get("limit") or 20), 200))
        statuses = payload.get("status")
        normalized_statuses = (
            [str(item).strip().lower() for item in statuses if str(item).strip()]
            if isinstance(statuses, list)
            else None
        )

        if workflow_id:
            executions = await self.context.execution_store.list_executions_by_workflow(workflow_id)
        elif agent_id:
            executions = await self.context.execution_store.list_executions_by_agent(agent_id)
        elif active_only:
            executions = await self.context.execution_store.list_active_executions()
        else:
            executions = await self.context.execution_store.list_executions()

        if normalized_statuses:
            allowed = set(normalized_statuses)
            executions = [execution for execution in executions if execution.status.value.lower() in allowed]

        settings = get_settings()
        items = []
        for execution in sorted(executions, key=lambda item: item.created_at, reverse=True)[:limit]:
            execution_payload = execution.model_dump(mode="json")
            execution_payload["stale_classification"] = classify_execution_staleness(
                execution,
                stale_after_seconds=settings.main_agent_workflow_monitor_stale_after_seconds,
                idle_timeout_seconds=settings.agent_activity_idle_timeout_seconds,
                run_timeout_seconds=settings.agent_run_timeout_seconds,
            )
            items.append(execution_payload)

        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
                "status": "ok",
                "items": items,
                "count": len(items),
                "filters": {
                    "workflow_id": workflow_id,
                    "agent_id": agent_id,
                    "status": normalized_statuses or [],
                    "active_only": active_only,
                    "limit": limit,
                },
            },
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
                runtime_adapter_id=payload.get("runtime_adapter_id") if isinstance(payload.get("runtime_adapter_id"),
                                                                                   str) else None,
                goal_id=_optional_string(payload.get("goal_id")),
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

    async def _run_execution_pause(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        return await self._run_execution_control_action("pause", payload, actor=actor)

    async def _run_execution_resume(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        return await self._run_execution_control_action("resume", payload, actor=actor)

    async def _run_execution_cancel(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        return await self._run_execution_control_action("cancel", payload, actor=actor)

    async def _run_execution_control_action(
            self,
            action: str,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name_by_action = {
            "pause": SYSTEM_EXECUTION_PAUSE_TOOL_ID,
            "resume": SYSTEM_EXECUTION_RESUME_TOOL_ID,
            "cancel": SYSTEM_EXECUTION_CANCEL_TOOL_ID,
        }
        tool_name = tool_name_by_action[action]
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        execution_id = str(payload.get("execution_id") or "")
        service = ExecutionService(self.context)
        try:
            if action == "pause":
                result = await service.pause(execution_id)
            elif action == "resume":
                result = await service.resume(execution_id)
            else:
                result = await service.cancel(execution_id)
        except (ExecutionNotFoundError, WorkflowNotFoundError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "execution": result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_execution_approvals(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_EXECUTION_APPROVALS_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        execution_id = str(payload.get("execution_id") or "")
        try:
            items = await ExecutionService(self.context).list_execution_approvals(execution_id)
        except ExecutionNotFoundError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=True,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "items": items, "count": len(items)},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_execution_approve(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        return await self._run_execution_approval_decision("approve", payload, actor=actor)

    async def _run_execution_reject(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        return await self._run_execution_approval_decision("reject", payload, actor=actor)

    async def _run_execution_approval_decision(
            self,
            action: str,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        tool_name = SYSTEM_EXECUTION_APPROVE_TOOL_ID if action == "approve" else SYSTEM_EXECUTION_REJECT_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        execution_id = str(payload.get("execution_id") or "")
        tool_id = str(payload.get("tool_id") or "")
        reason = _optional_string(payload.get("reason"))
        service = ExecutionService(self.context)
        try:
            result = (
                await service.approve(execution_id, tool_id, reason)
                if action == "approve"
                else await service.reject(execution_id, tool_id, reason)
            )
        except ExecutionNotFoundError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "decision": result},
            patch=None,
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
                    "input_payload": payload.get("input_payload") if isinstance(payload.get("input_payload"),
                                                                                dict) else {},
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
            fallback = resolve_tool(tool_id, await self._visible_tool_definitions_async())
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

    async def _visible_tool_definitions_async(self) -> list[Any]:
        tools = list_builtin_tool_definitions()
        if self.context is None or not hasattr(self.context, "tool_repo"):
            return tools
        persisted_tools = await self.context.tool_repo.list()
        merged: dict[str, Any] = {tool.id: tool for tool in tools}
        for tool in persisted_tools:
            merged[tool.id] = tool
        return list(merged.values())

    async def _run_tool_workspace_list(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_TOOL_WORKSPACE_LIST_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        result = await GeneratedToolWorkspaceService(self.context).list_packages_with_registry()
        package_id = _optional_string(payload.get("package_id"))
        if package_id:
            result["packages"] = [
                item for item in result.get("packages", []) if str(item.get("package_id") or "") == package_id
            ]
            result["count"] = len(result["packages"])
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_tool_workspace_scaffold(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_TOOL_WORKSPACE_SCAFFOLD_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        service = GeneratedToolWorkspaceService(self.context)
        try:
            result = service.scaffold_package(
                package_id=str(payload.get("package_id") or ""),
                name=str(payload.get("name") or ""),
                description=_optional_string(payload.get("description")),
                function_name=_optional_string(payload.get("function_name")),
                overwrite=bool(payload.get("overwrite", False)),
            )
        except GeneratedToolWorkspaceError as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "package": result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_tool_workspace_publish(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_TOOL_WORKSPACE_PUBLISH_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        service = GeneratedToolWorkspaceService(self.context)
        try:
            tool = await service.publish_tool(
                package_id=str(payload.get("package_id") or ""),
                tool_id=str(payload.get("tool_id") or ""),
                name=str(payload.get("name") or ""),
                display_name=_optional_string(payload.get("display_name")),
                description=str(payload.get("description") or ""),
                callable_name=str(payload.get("callable_name") or ""),
                input_schema=payload.get("input_schema") if isinstance(payload.get("input_schema"), dict) else {},
                output_schema=payload.get("output_schema") if isinstance(payload.get("output_schema"), dict) else {},
                tags=_string_list(payload.get("tags")),
                security=payload.get("security") if isinstance(payload.get("security"), dict) else None,
            )
        except (GeneratedToolWorkspaceError, ValueError) as exc:
            return build_tool_run_response(
                verdict="warn",
                policy_verdict=policy_verdict,
                result={"status": "error", "error": str(exc)},
                patch=None,
                errors=[str(exc)],
                dry_run=False,
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "tool": tool.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_document_links(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_DOCUMENT_LINKS_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(tool_name, policy_verdict, f"Workflow '{workflow_id}' was not found.",
                                               actor=actor)
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            result = await WorkflowService(self.context).workflow_document_links(
                workflow_id,
                link_id=_optional_string(payload.get("link_id")),
                target_type=_optional_string(payload.get("target_type")),
                target_id=_optional_string(payload.get("target_id")),
                document_id=_optional_string(payload.get("document_id")),
                limit=payload.get("limit"),
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(tool_name, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_document_link_add(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_DOCUMENT_LINK_ADD_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(tool_name, policy_verdict, f"Workflow '{workflow_id}' was not found.",
                                               actor=actor)
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            document = await self._owned_uploaded_document(str(payload.get("document_id") or ""),
                                                           current_user=current_user)
            if document is None:
                raise MemoryPermissionError(f"Uploaded document '{payload.get('document_id')}' was not found.")
            result = await WorkflowService(self.context).add_workflow_document_link(
                workflow_id,
                {
                    **payload,
                    "document_id": document.id,
                    "linked_by": _optional_string(payload.get("linked_by")) or actor,
                },
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(tool_name, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_document_link_delete(self, payload: dict[str, Any], *,
                                                 actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_DOCUMENT_LINK_DELETE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(tool_name, policy_verdict, f"Workflow '{workflow_id}' was not found.",
                                               actor=actor)
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            result = await WorkflowService(self.context).delete_workflow_document_link(
                workflow_id,
                str(payload.get("link_id") or ""),
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(tool_name, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome if result.get("deleted") else "warn",
            policy_verdict=policy_verdict,
            result={"status": "ok" if result.get("deleted") else "error", **result},
            patch=None,
            errors=[] if result.get("deleted") else [
                f"Workflow document link '{payload.get('link_id')}' was not found."],
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_document_summary(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_DOCUMENT_SUMMARY_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(tool_name, policy_verdict, f"Workflow '{workflow_id}' was not found.",
                                               actor=actor)
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            document = await self._owned_uploaded_document(str(payload.get("document_id") or ""),
                                                           current_user=current_user)
            if document is None:
                raise MemoryPermissionError(f"Uploaded document '{payload.get('document_id')}' was not found.")
            result = await WorkflowService(self.context).summarize_workflow_document(workflow_id, document.id)
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(tool_name, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", **result},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_shared_memory_namespaces(self, payload: dict[str, Any], *,
                                                     actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACES_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(tool_name, policy_verdict, f"Workflow '{workflow_id}' was not found.",
                                               actor=actor)
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            result = await WorkflowService(self.context).workflow_shared_memory_namespaces(
                workflow_id,
                namespace_id=_optional_string(payload.get("namespace_id")),
                limit=payload.get("limit"),
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(tool_name, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(verdict=policy_verdict.outcome, policy_verdict=policy_verdict,
                                       result={"status": "ok", **result}, patch=None, dry_run=True, actor=actor)

    async def _run_workflow_shared_memory_namespace_create(self, payload: dict[str, Any], *,
                                                           actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_CREATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(tool_name, policy_verdict, f"Workflow '{workflow_id}' was not found.",
                                               actor=actor)
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            result = await WorkflowService(self.context).create_workflow_shared_memory_namespace(workflow_id, payload)
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(tool_name, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(verdict=policy_verdict.outcome, policy_verdict=policy_verdict,
                                       result={"status": "ok", **result}, patch=None, dry_run=False, actor=actor)

    async def _run_workflow_shared_memory_namespace_update(self, payload: dict[str, Any], *,
                                                           actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_UPDATE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(tool_name, policy_verdict, f"Workflow '{workflow_id}' was not found.",
                                               actor=actor)
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            result = await WorkflowService(self.context).update_workflow_shared_memory_namespace(
                workflow_id,
                str(payload.get("namespace_id") or ""),
                payload.get("patch") if isinstance(payload.get("patch"), dict) else {},
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(tool_name, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(verdict=policy_verdict.outcome, policy_verdict=policy_verdict,
                                       result={"status": "ok", **result}, patch=None, dry_run=False, actor=actor)

    async def _run_workflow_shared_memory_namespace_delete(self, payload: dict[str, Any], *,
                                                           actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_DELETE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(tool_name, policy_verdict, f"Workflow '{workflow_id}' was not found.",
                                               actor=actor)
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            result = await WorkflowService(self.context).delete_workflow_shared_memory_namespace(
                workflow_id,
                str(payload.get("namespace_id") or ""),
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(tool_name, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(verdict=policy_verdict.outcome if result.get("deleted") else "warn",
                                       policy_verdict=policy_verdict,
                                       result={"status": "ok" if result.get("deleted") else "error", **result},
                                       patch=None, errors=[] if result.get("deleted") else [
                f"Shared memory namespace '{payload.get('namespace_id')}' was not found."], dry_run=False, actor=actor)

    async def _run_workflow_shared_memory_namespace_memories(self, payload: dict[str, Any], *,
                                                             actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORIES_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(tool_name, policy_verdict, f"Workflow '{workflow_id}' was not found.",
                                               actor=actor)
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            result = await WorkflowService(self.context).workflow_shared_memory_namespace_memories(
                workflow_id,
                str(payload.get("namespace_id") or ""),
                current_user=current_user,
                limit=payload.get("limit"),
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(tool_name, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(verdict=policy_verdict.outcome, policy_verdict=policy_verdict,
                                       result={"status": "ok", **result}, patch=None, dry_run=True, actor=actor)

    async def _run_workflow_shared_memory_namespace_memory_add(self, payload: dict[str, Any], *,
                                                               actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_ADD_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(tool_name, policy_verdict, f"Workflow '{workflow_id}' was not found.",
                                               actor=actor)
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            result = await WorkflowService(self.context).add_workflow_shared_memory_namespace_memory(
                workflow_id,
                str(payload.get("namespace_id") or ""),
                str(payload.get("memory_id") or ""),
                current_user=current_user,
                trusted_actor=bool(actor and actor.startswith("approved/")),
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(tool_name, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(verdict=policy_verdict.outcome, policy_verdict=policy_verdict,
                                       result={"status": "ok", **result}, patch=None, dry_run=False, actor=actor)

    async def _run_workflow_shared_memory_namespace_memory_remove(self, payload: dict[str, Any], *,
                                                                  actor: str | None) -> ToolRunResponse:
        tool_name = SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_REMOVE_TOOL_ID
        policy_verdict = self.policy_engine.evaluate(tool_name, payload, actor=actor)
        self._publish_policy_event(tool_name, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(tool_name, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(tool_name, policy_verdict, f"Workflow '{workflow_id}' was not found.",
                                               actor=actor)
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            result = await WorkflowService(self.context).remove_workflow_shared_memory_namespace_memory(
                workflow_id,
                str(payload.get("namespace_id") or ""),
                str(payload.get("memory_id") or ""),
                current_user=current_user,
                trusted_actor=bool(actor and actor.startswith("approved/")),
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(tool_name, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(verdict=policy_verdict.outcome if result.get("deleted") else "warn",
                                       policy_verdict=policy_verdict,
                                       result={"status": "ok" if result.get("deleted") else "error", **result},
                                       patch=None, errors=[] if result.get("deleted") else [
                f"Memory '{payload.get('memory_id')}' was not linked to namespace '{payload.get('namespace_id')}'."],
                                       dry_run=False, actor=actor)

    async def _run_graph_context(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        started_at = perf_counter()
        policy_verdict = self.policy_engine.evaluate(SYSTEM_GRAPH_CONTEXT_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_GRAPH_CONTEXT_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            response = self._context_required_response(SYSTEM_GRAPH_CONTEXT_TOOL_ID, actor=actor)
            self._publish_graph_context_event(
                response.result if isinstance(response.result, dict) else {},
                actor=actor,
                duration_ms=_duration_ms(started_at),
            )
            return response
        service_payload = self._graph_context_payload(payload, actor=actor)
        result = await AgencyGraphContextService(self.context).build_context(service_payload)
        status = str(result.get("status") or "error")
        errors = [] if status == "ok" else [str(result.get("summary") or status)]
        response = build_tool_run_response(
            verdict=policy_verdict.outcome if status == "ok" else "warn",
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            errors=errors,
            dry_run=True,
            actor=actor,
        )
        self._publish_graph_context_event(result, actor=actor, duration_ms=_duration_ms(started_at))
        return response

    async def _run_graph_search(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_GRAPH_SEARCH_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_GRAPH_SEARCH_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_GRAPH_SEARCH_TOOL_ID, actor=actor)
        reader = None
        close_after = False
        try:
            reader, close_after = resolve_graph_reader(self.context)
            if actor:
                setattr(reader, "access_user_id", actor)
            limit = _bounded_int(payload.get("limit") or 50, minimum=1, maximum=100)
            document = await reader.search_nodes(
                _optional_string(payload.get("query")),
                labels=_string_list(payload.get("labels")),
                node_types=_string_list(payload.get("node_types")),
                workflow_id=_optional_string(payload.get("workflow_id")),
                agent_id=_optional_string(payload.get("agent_id")),
                tool_id=_optional_string(payload.get("tool_id")),
                document_id=_optional_string(payload.get("document_id")),
                entity_id=_optional_string(payload.get("entity_id")),
                error_text=_optional_string(payload.get("error_text")),
                limit=limit,
            )
            result = graph_document_payload(
                document,
                query_meta={"query": "agency.graph.search"},
                limit=limit,
                max_edges=0,
            )
            response_errors: list[str] = []
            verdict = policy_verdict.outcome
        except (ValueError, GraphReadUnavailableError, Neo4jGraphReadError) as exc:
            result = {
                "nodes": [],
                "edges": [],
                "meta": {
                    "query": "agency.graph.search",
                    "status": "error",
                    "error": str(exc),
                    "projection_available": not isinstance(exc, GraphReadUnavailableError),
                },
            }
            response_errors = [str(exc)]
            verdict = "warn"
        finally:
            if reader is not None:
                await close_graph_reader_if_needed(reader, close_after)
        return build_tool_run_response(
            verdict=verdict,
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            errors=response_errors,
            dry_run=True,
            actor=actor,
        )

    async def _run_graph_expand(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_GRAPH_EXPAND_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_GRAPH_EXPAND_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_GRAPH_EXPAND_TOOL_ID, actor=actor)
        reader = None
        close_after = False
        try:
            preset_key = _optional_string(payload.get("preset"))
            mode_key = _optional_string(payload.get("mode"))
            preset_config = _graph_config_or_error(GRAPH_NEIGHBORHOOD_PRESETS, preset_key, "preset")
            mode_config = _graph_config_or_error(GRAPH_NEIGHBORHOOD_MODES, mode_key, "mode")
            base_config = preset_config or mode_config
            labels = _string_list(payload.get("labels")) or (base_config["labels"] if base_config else [])
            relationship_types = _string_list(payload.get("relationship_types")) or (
                base_config["relationship_types"] if base_config else []
            )
            depth = _bounded_int(payload.get("depth") or 1, minimum=1, maximum=2)
            limit = _bounded_int(payload.get("limit") or 50, minimum=1, maximum=100)
            include_deleted = bool(payload.get("include_deleted") is True)
            reader, close_after = resolve_graph_reader(self.context)
            if actor:
                setattr(reader, "access_user_id", actor)
            document = await reader.get_neighborhood(
                str(payload.get("node_id") or ""),
                labels=labels,
                relationship_types=relationship_types,
                depth=depth,
                limit=limit,
                include_deleted=include_deleted,
            )
            result = graph_document_payload(
                document,
                query_meta={
                    "query": "agency.graph.expand",
                    "node_id": str(payload.get("node_id") or ""),
                    "preset": preset_key,
                    "mode": mode_key,
                    "labels": labels,
                    "relationship_types": relationship_types,
                    "depth": depth,
                    "include_deleted": include_deleted,
                },
                limit=limit,
                max_edges=limit,
            )
            response_errors: list[str] = []
            verdict = policy_verdict.outcome
        except (ValueError, GraphReadUnavailableError, Neo4jGraphReadError) as exc:
            result = {
                "nodes": [],
                "edges": [],
                "meta": {
                    "query": "agency.graph.expand",
                    "status": "error",
                    "node_id": str(payload.get("node_id") or ""),
                    "error": str(exc),
                    "projection_available": not isinstance(exc, GraphReadUnavailableError),
                },
            }
            response_errors = [str(exc)]
            verdict = "warn"
        finally:
            if reader is not None:
                await close_graph_reader_if_needed(reader, close_after)
        return build_tool_run_response(
            verdict=verdict,
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            errors=response_errors,
            dry_run=True,
            actor=actor,
        )

    async def _run_graph_neighbors(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_GRAPH_NEIGHBORS_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_GRAPH_NEIGHBORS_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_GRAPH_NEIGHBORS_TOOL_ID, actor=actor)
        reader = None
        close_after = False
        node_id = str(payload.get("node_id") or "")
        try:
            preset_key = _optional_string(payload.get("preset"))
            mode_key = _optional_string(payload.get("mode"))
            preset_config = _graph_config_or_error(GRAPH_NEIGHBORHOOD_PRESETS, preset_key, "preset")
            mode_config = _graph_config_or_error(GRAPH_NEIGHBORHOOD_MODES, mode_key, "mode")
            base_config = preset_config or mode_config
            labels = _string_list(payload.get("labels")) or (base_config["labels"] if base_config else [])
            relationship_types = _string_list(payload.get("relationship_types")) or (
                base_config["relationship_types"] if base_config else []
            )
            limit = _bounded_int(payload.get("limit") or 50, minimum=1, maximum=100)
            include_deleted = bool(payload.get("include_deleted") is True)
            reader, close_after = resolve_graph_reader(self.context)
            if actor:
                setattr(reader, "access_user_id", actor)
            document = await reader.get_neighborhood(
                node_id,
                labels=labels,
                relationship_types=relationship_types,
                depth=1,
                limit=limit,
                include_deleted=include_deleted,
            )
            result = graph_neighbors_payload(
                document,
                center_id=node_id,
                query_meta={
                    "query": "agency.graph.neighbors",
                    "node_id": node_id,
                    "preset": preset_key,
                    "mode": mode_key,
                    "labels": labels,
                    "relationship_types": relationship_types,
                    "depth": 1,
                    "include_deleted": include_deleted,
                },
                limit=limit,
                max_edges=limit,
            )
            response_errors: list[str] = []
            verdict = policy_verdict.outcome
        except (ValueError, GraphReadUnavailableError, Neo4jGraphReadError) as exc:
            result = {
                "center": None,
                "groups": [],
                "nodes": [],
                "edges": [],
                "meta": {
                    "query": "agency.graph.neighbors",
                    "status": "error",
                    "node_id": node_id,
                    "error": str(exc),
                    "projection_available": not isinstance(exc, GraphReadUnavailableError),
                },
            }
            response_errors = [str(exc)]
            verdict = "warn"
        finally:
            if reader is not None:
                await close_graph_reader_if_needed(reader, close_after)
        return build_tool_run_response(
            verdict=verdict,
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            errors=response_errors,
            dry_run=True,
            actor=actor,
        )

    async def _run_graph_path(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_GRAPH_PATH_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_GRAPH_PATH_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_GRAPH_PATH_TOOL_ID, actor=actor)
        reader = None
        close_after = False
        path_type = str(payload.get("path_type") or "").strip()
        try:
            limit = _bounded_int(payload.get("limit") or 25, minimum=1, maximum=100)
            max_depth = _bounded_int(payload.get("max_depth") or 4, minimum=1, maximum=4)
            reader, close_after = resolve_graph_reader(self.context)
            if actor:
                setattr(reader, "access_user_id", actor)
            document = await _run_graph_path_reader(reader, payload, path_type=path_type, max_depth=max_depth,
                                                    limit=limit)
            result = graph_document_payload(
                document,
                query_meta={
                    "query": "agency.graph.path",
                    "path_type": path_type,
                    "max_depth": max_depth,
                    "limit": limit,
                },
                limit=limit,
                max_edges=limit,
            )
            response_errors: list[str] = []
            verdict = policy_verdict.outcome
        except (ValueError, GraphReadUnavailableError, Neo4jGraphReadError) as exc:
            result = {
                "nodes": [],
                "edges": [],
                "meta": {
                    "query": "agency.graph.path",
                    "status": "error",
                    "path_type": path_type,
                    "error": str(exc),
                    "projection_available": not isinstance(exc, GraphReadUnavailableError),
                },
            }
            response_errors = [str(exc)]
            verdict = "warn"
        finally:
            if reader is not None:
                await close_graph_reader_if_needed(reader, close_after)
        return build_tool_run_response(
            verdict=verdict,
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            errors=response_errors,
            dry_run=True,
            actor=actor,
        )

    async def _run_graph_summarize_subgraph(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_GRAPH_SUMMARIZE_SUBGRAPH_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_GRAPH_SUMMARIZE_SUBGRAPH_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_GRAPH_SUMMARIZE_SUBGRAPH_TOOL_ID, actor=actor)
        result = await AgencyGraphContextService(self.context).summarize_subgraph(
            self._graph_context_payload(payload, actor=actor)
        )
        status = str(result.get("status") or "error")
        errors = [] if status == "ok" else [str(result.get("summary") or status)]
        return build_tool_run_response(
            verdict=policy_verdict.outcome if status == "ok" else "warn",
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            errors=errors,
            dry_run=True,
            actor=actor,
        )

    async def _run_graph_working_set_create(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID, actor=actor)
        state, error = await self._graph_working_set_state(payload)
        if error:
            return self._graph_working_set_error_response(
                SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID,
                policy_verdict,
                error,
                actor=actor,
            )
        working_set = create_graph_working_set(
            state,
            working_set_id=_optional_string(payload.get("working_set_id")),
            owner_agent_id=_optional_string(payload.get("owner_agent_id")),
            conversation_id=_optional_string(payload.get("conversation_id")),
            workflow_id=_optional_string(payload.get("workflow_id")) or state.workflow_id,
            run_id=_optional_string(payload.get("run_id")) or state.execution_id,
            execution_id=_optional_string(payload.get("execution_id")) or state.execution_id,
            anchors=_dict_list(payload.get("anchors")),
            notes=_dict_list(payload.get("notes")),
            ttl_seconds=_bounded_int(payload.get("ttl_seconds") or 21600, minimum=60, maximum=86400),
        )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "working_set": working_set.to_dict()},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_graph_working_set_add(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID, actor=actor)
        state, error = await self._graph_working_set_state(payload)
        working_set = None if error else state.graph_working_sets.get(str(payload.get("working_set_id") or ""))
        if error or working_set is None:
            return self._graph_working_set_error_response(
                SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID,
                policy_verdict,
                error or f"Graph working set '{payload.get('working_set_id')}' was not found.",
                actor=actor,
            )
        add_graph_working_set_items(
            working_set,
            anchors=_dict_list(payload.get("anchors")),
            visited_nodes=_dict_list(payload.get("visited_nodes")),
            selected_nodes=_dict_list(payload.get("selected_nodes")),
            notes=_dict_list(payload.get("notes")),
            ttl_seconds=_bounded_int(payload.get("ttl_seconds") or 21600, minimum=60, maximum=86400),
        )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "working_set": working_set.to_dict()},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_graph_working_set_remove(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID, actor=actor)
        state, error = await self._graph_working_set_state(payload)
        working_set = None if error else state.graph_working_sets.get(str(payload.get("working_set_id") or ""))
        if error or working_set is None:
            return self._graph_working_set_error_response(
                SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID,
                policy_verdict,
                error or f"Graph working set '{payload.get('working_set_id')}' was not found.",
                actor=actor,
            )
        remove_graph_working_set_items(
            working_set,
            anchor_ids=_string_list(payload.get("anchor_ids")),
            visited_node_ids=_string_list(payload.get("visited_node_ids")),
            selected_node_ids=_string_list(payload.get("selected_node_ids")),
            clear_notes=bool(payload.get("clear_notes") is True),
            ttl_seconds=_bounded_int(payload.get("ttl_seconds") or 21600, minimum=60, maximum=86400),
        )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "working_set": working_set.to_dict()},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_graph_working_set_summarize(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID, actor=actor)
        state, error = await self._graph_working_set_state(payload)
        working_set = None if error else state.graph_working_sets.get(str(payload.get("working_set_id") or ""))
        if error or working_set is None:
            return self._graph_working_set_error_response(
                SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID,
                policy_verdict,
                error or f"Graph working set '{payload.get('working_set_id')}' was not found.",
                actor=actor,
            )
        result = {
            "status": "ok",
            "summary": (
                f"Graph working set {working_set.working_set_id}: "
                f"{len(working_set.anchors)} anchors, {len(working_set.visited_nodes)} visited nodes, "
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
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result=result,
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_graph_working_set_clear(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID, actor=actor)
        state, error = await self._graph_working_set_state(payload)
        if error:
            return self._graph_working_set_error_response(
                SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID,
                policy_verdict,
                error,
                actor=actor,
            )
        working_set_id = str(payload.get("working_set_id") or "")
        removed = state.graph_working_sets.pop(working_set_id, None)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "working_set_id": working_set_id, "cleared": removed is not None},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_graph_working_set_persist_context_pack(
            self,
            payload: dict[str, Any],
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(
            SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID,
            payload,
            actor=actor,
        )
        self._publish_policy_event(SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID, actor=actor)
        state, error = await self._graph_working_set_state(payload)
        working_set = None if error else state.graph_working_sets.get(str(payload.get("working_set_id") or ""))
        if error or working_set is None:
            return self._graph_working_set_error_response(
                SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID,
                policy_verdict,
                error or f"Graph working set '{payload.get('working_set_id')}' was not found.",
                actor=actor,
            )
        memory_payload, memory_error = _graph_working_set_context_pack_payload(
            working_set,
            payload,
            actor=actor,
        )
        if memory_error:
            return self._graph_working_set_error_response(
                SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID,
                policy_verdict,
                memory_error,
                actor=actor,
            )
        try:
            memory = await MemoryService(self.context).create_memory(
                memory_payload,
                confirmed=bool(payload.get("confirmed") is True),
                trusted_actor=True,
            )
        except (MemoryPolicyError, MemoryPermissionError, ValueError) as exc:
            return self._graph_working_set_error_response(
                SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID,
                policy_verdict,
                str(exc),
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
                "status": "ok",
                "memory": memory.model_dump(mode="json"),
                "context_pack_id": memory.id,
                "working_set_id": working_set.working_set_id,
                "graph_provenance": memory_payload["metadata"]["graph_provenance"],
            },
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _graph_working_set_state(self, payload: dict[str, Any]):
        execution_id = _optional_string(payload.get("execution_id"))
        if not execution_id:
            return None, "execution_id is required."
        runtime_registry = getattr(self.context, "runtime_registry", None) if self.context is not None else None
        if runtime_registry is None:
            return None, "Runtime registry is unavailable."
        try:
            snapshot = await runtime_registry.get_execution_state(execution_id)
        except Exception as exc:
            return None, str(exc)
        state = getattr(snapshot, "state", None)
        if state is None:
            return None, f"Execution '{execution_id}' has no active native runtime state."
        prune_expired_graph_working_sets(state)
        return state, None

    def _graph_working_set_error_response(
            self,
            tool_name: str,
            policy_verdict,
            error: str,
            *,
            actor: str | None,
    ) -> ToolRunResponse:
        return build_tool_run_response(
            verdict="warn",
            policy_verdict=policy_verdict,
            result={"status": "error", "error": error},
            patch=None,
            errors=[error],
            dry_run=True,
            actor=actor,
        )

    def _graph_context_payload(self, payload: dict[str, Any], *, actor: str | None) -> dict[str, Any]:
        if not actor:
            return payload
        scoped_payload = dict(payload)
        scope = dict(scoped_payload.get("scope") or {})
        runtime_context = dict(scope.get("runtime_context") or {})
        runtime_context.setdefault("current_user_id", actor)
        scope["runtime_context"] = runtime_context
        scoped_payload["scope"] = scope
        return scoped_payload

    def _record_graph_tool_observability(
            self,
            tool_name: str,
            result: dict[str, Any],
            *,
            actor: str | None,
            duration_ms: int,
            verdict: str,
    ) -> None:
        metadata = _graph_tool_observability_metadata(
            tool_name,
            result,
            actor=actor,
            duration_ms=duration_ms,
            verdict=verdict,
        )
        operations = getattr(self.context, "runtime_operations", None) if self.context is not None else None
        if operations is not None:
            _record_graph_tool_counters(operations, metadata)
            operations.record_action(
                "graph_tool.completed",
                tool_id=tool_name,
                actor=actor,
                status=metadata["status"],
                error_kind=metadata.get("error_kind"),
                intent=metadata.get("intent"),
                mode=metadata.get("mode"),
                duration_ms=duration_ms,
                node_count=metadata["node_count"],
                edge_count=metadata["edge_count"],
                output_bytes=metadata["output_bytes"],
            )
        publish_tool_runtime_event(
            lifecycle_type="agency.graph.tool.completed",
            tool_name=tool_name,
            actor=actor,
            verdict="ok" if metadata["success"] else "warn",
            metadata=metadata,
        )
        redacted, _ = _GRAPH_OBSERVABILITY_REDACTOR.redact_value(metadata)
        logger.debug("Agency Graph tool completed", extra={"agency_graph_tool": redacted})

    def _publish_graph_context_event(
            self,
            result: dict[str, Any],
            *,
            actor: str | None,
            duration_ms: int,
    ) -> None:
        query_meta = result.get("query_meta") if isinstance(result.get("query_meta"), dict) else {}
        omitted = result.get("omitted") if isinstance(result.get("omitted"), dict) else {}
        status = str(result.get("status") or "error")
        omitted_nodes = _int_meta_value(omitted.get("nodes"))
        omitted_edges = _int_meta_value(omitted.get("edges"))
        omitted_by_policy = _int_meta_value(query_meta.get("memory_nodes_omitted_by_policy"))
        protected_omitted_by_policy = _int_meta_value(query_meta.get("protected_nodes_omitted_by_policy"))
        projection_available = bool(query_meta.get("projection_available"))
        observability_metadata = _graph_tool_observability_metadata(
            SYSTEM_GRAPH_CONTEXT_TOOL_ID,
            result,
            actor=actor,
            duration_ms=duration_ms,
            verdict="ok" if status == "ok" else "warn",
        )
        publish_tool_runtime_event(
            lifecycle_type="agency.graph.context.completed",
            tool_name=SYSTEM_GRAPH_CONTEXT_TOOL_ID,
            actor=actor,
            verdict="ok" if status == "ok" else "warn",
            metadata={
                "tool_id": SYSTEM_GRAPH_CONTEXT_TOOL_ID,
                "actor": actor,
                "status": status,
                "intent": query_meta.get("intent"),
                "mode": query_meta.get("mode"),
                "anchor_type": query_meta.get("anchor_type"),
                "anchor_id": query_meta.get("anchor_id"),
                "depth": query_meta.get("depth"),
                "limit": query_meta.get("limit"),
                "budget": query_meta.get("budget"),
                "node_count": _int_meta_value(query_meta.get("node_count")),
                "edge_count": _int_meta_value(query_meta.get("edge_count")),
                "omitted_count": omitted_nodes + omitted_edges + omitted_by_policy + protected_omitted_by_policy,
                "omitted_nodes": omitted_nodes,
                "omitted_edges": omitted_edges,
                "memory_nodes_omitted_by_policy": omitted_by_policy,
                "protected_nodes_omitted_by_policy": protected_omitted_by_policy,
                "traversal_units": _int_meta_value(query_meta.get("traversal_units")),
                "traversal_budget_max_units": _int_meta_value(query_meta.get("traversal_budget_max_units")),
                "traversal_budget_window_seconds": query_meta.get("traversal_budget_window_seconds"),
                "traversal_units_remaining": _int_meta_value(query_meta.get("traversal_units_remaining")),
                "duration_ms": duration_ms,
                "graph_availability": "available" if projection_available else status,
                "projection_available": projection_available,
                "fallback_used": bool(query_meta.get("fallback_used")),
                "error_kind": observability_metadata.get("error_kind"),
                "graph_error_counters": observability_metadata["graph_error_counters"],
                "graph_success_metrics": observability_metadata["graph_success_metrics"],
            },
        )

    async def _run_memory_list(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_MEMORY_LIST_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_MEMORY_LIST_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_MEMORY_LIST_TOOL_ID, actor=actor)
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

    async def _run_memory_catalog(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_MEMORY_CATALOG_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_MEMORY_CATALOG_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_MEMORY_CATALOG_TOOL_ID, actor=actor)
        current_user = await self._current_user(actor)
        service = MemoryService(self.context)
        try:
            catalog = await service.list_memory_catalog(
                scope=payload.get("scope") if isinstance(payload.get("scope"), str) else None,
                workflow_id=payload.get("workflow_id") if isinstance(payload.get("workflow_id"), str) else None,
                agent_id=payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else None,
                conversation_id=payload.get("conversation_id") if isinstance(payload.get("conversation_id"),
                                                                             str) else None,
                target_type=payload.get("target_type") if isinstance(payload.get("target_type"), str) else None,
                target_id=payload.get("target_id") if isinstance(payload.get("target_id"), str) else None,
                q=payload.get("query") if isinstance(payload.get("query"), str) else None,
                include_sensitive=bool(payload.get("include_sensitive")),
                statuses=payload.get("status") if isinstance(payload.get("status"), list) else None,
                limit_per_group=int(payload.get("limit_per_group") or 20),
                current_user=current_user,
            )
        except ValueError as exc:
            return self._memory_error_response(SYSTEM_MEMORY_CATALOG_TOOL_ID, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "catalog": catalog},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_memory_remember(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_MEMORY_REMEMBER_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_MEMORY_REMEMBER_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_MEMORY_REMEMBER_TOOL_ID, actor=actor)
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
            return self._memory_error_response(SYSTEM_MEMORY_REMEMBER_TOOL_ID, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "memory": created.model_dump(mode="json")},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_memory_update(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_MEMORY_UPDATE_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_MEMORY_UPDATE_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_MEMORY_UPDATE_TOOL_ID, actor=actor)
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
            await self._assert_read_write_memory_link(
                memory_id=str(payload.get("memory_id") or ""),
                payload=payload,
                current_user=current_user,
            )
            updated = await service.update_memory(
                str(payload.get("memory_id") or ""),
                patch,
                confirmed=bool(payload.get("confirmed")),
                current_user=current_user,
                trusted_actor=bool(actor and actor.startswith("approved/")),
            )
        except (MemoryPolicyError, MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(SYSTEM_MEMORY_UPDATE_TOOL_ID, policy_verdict, str(exc), actor=actor)
        if updated is None:
            return self._memory_error_response(
                SYSTEM_MEMORY_UPDATE_TOOL_ID,
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
        policy_verdict = self.policy_engine.evaluate(SYSTEM_MEMORY_DELETE_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_MEMORY_DELETE_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_MEMORY_DELETE_TOOL_ID, actor=actor)
        current_user = await self._current_user(actor)
        service = MemoryService(self.context)
        try:
            await self._assert_read_write_memory_link(
                memory_id=str(payload.get("memory_id") or ""),
                payload=payload,
                current_user=current_user,
            )
            deleted = await service.delete_memory(
                str(payload.get("memory_id") or ""),
                current_user=current_user,
                trusted_actor=bool(actor and actor.startswith("approved/")),
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(SYSTEM_MEMORY_DELETE_TOOL_ID, policy_verdict, str(exc), actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome if deleted else "warn",
            policy_verdict=policy_verdict,
            result={"status": "ok" if deleted else "error", "deleted": deleted, "memory_id": payload.get("memory_id")},
            patch=None,
            errors=[] if deleted else [f"Memory '{payload.get('memory_id')}' was not found."],
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_memory_links_list(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_WORKFLOW_MEMORY_LINKS_LIST_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_WORKFLOW_MEMORY_LINKS_LIST_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_WORKFLOW_MEMORY_LINKS_LIST_TOOL_ID, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(
                SYSTEM_WORKFLOW_MEMORY_LINKS_LIST_TOOL_ID,
                policy_verdict,
                f"Workflow '{workflow_id}' was not found.",
                actor=actor,
            )
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
        except MemoryPermissionError as exc:
            return self._memory_error_response(SYSTEM_WORKFLOW_MEMORY_LINKS_LIST_TOOL_ID, policy_verdict, str(exc),
                                               actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
                "status": "ok",
                "workflow_id": workflow_id,
                "items": [
                    self._serialize_workflow_memory_link(workflow_id, link)
                    for link in self._workflow_memory_links(workflow)
                ],
            },
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_workflow_memory_links_add(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_WORKFLOW_MEMORY_LINKS_ADD_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_WORKFLOW_MEMORY_LINKS_ADD_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_WORKFLOW_MEMORY_LINKS_ADD_TOOL_ID, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(
                SYSTEM_WORKFLOW_MEMORY_LINKS_ADD_TOOL_ID,
                policy_verdict,
                f"Workflow '{workflow_id}' was not found.",
                actor=actor,
            )
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
            target_type = self._normalize_memory_link_value(
                str(payload.get("target_type") or ""),
                WORKFLOW_MEMORY_LINK_TARGET_TYPES,
                "target_type",
            )
            ref_type = self._normalize_memory_link_value(
                str(payload.get("ref_type") or ""),
                WORKFLOW_MEMORY_LINK_REF_TYPES,
                "ref_type",
            )
            access_mode = self._normalize_memory_link_value(
                str(payload.get("access_mode") or "read"),
                WORKFLOW_MEMORY_LINK_ACCESS_MODES,
                "access_mode",
            )
            target_id = payload.get("target_id") if isinstance(payload.get("target_id"), str) else None
            target_id = target_id.strip() if target_id and target_id.strip() else None
            if target_type == "workflow":
                target_id = None
            self._validate_memory_link_target(workflow, target_type, target_id)
            ref_id = str(payload.get("ref_id") or "").strip()
            if not ref_id:
                raise ValueError("ref_id is required for workflow memory links.")
            memory_ids, default_label = await self._resolve_memory_link_ref(
                ref_type=ref_type,
                ref_id=ref_id,
                current_user=current_user,
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(SYSTEM_WORKFLOW_MEMORY_LINKS_ADD_TOOL_ID, policy_verdict, str(exc),
                                               actor=actor)

        now = datetime.now(timezone.utc).isoformat()
        metadata = dict(workflow.metadata)
        links = self._workflow_memory_links(workflow)
        link_identity = (target_type, target_id, ref_type, ref_id, access_mode)
        existing = next((link for link in links if self._memory_link_identity(link) == link_identity), None)
        if existing is None:
            existing = {
                "id": f"workflow-memory-link-{uuid4().hex[:12]}",
                "created_at": now,
                "created_by": actor,
            }
            links.append(existing)
        existing.update(
            {
                "target_type": target_type,
                "target_id": target_id,
                "ref_type": ref_type,
                "ref_id": ref_id,
                "memory_ids": memory_ids,
                "access_mode": access_mode,
                "label": payload.get("label") if isinstance(payload.get("label"), str) else default_label,
                "updated_at": now,
                "updated_by": actor,
            }
        )
        metadata[WORKFLOW_MEMORY_LINK_METADATA_KEY] = links
        updated = await self.context.workflow_repo.update(workflow_id, {"metadata": metadata})
        if updated is None:
            return self._memory_error_response(
                SYSTEM_WORKFLOW_MEMORY_LINKS_ADD_TOOL_ID,
                policy_verdict,
                f"Workflow '{workflow_id}' was not found.",
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={
                "status": "ok",
                "workflow": updated.model_dump(mode="json"),
                "link": self._serialize_workflow_memory_link(workflow_id, existing),
                "items": [
                    self._serialize_workflow_memory_link(workflow_id, link)
                    for link in self._workflow_memory_links(updated)
                ],
            },
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_workflow_memory_links_delete(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_WORKFLOW_MEMORY_LINKS_DELETE_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_WORKFLOW_MEMORY_LINKS_DELETE_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_WORKFLOW_MEMORY_LINKS_DELETE_TOOL_ID, actor=actor)
        current_user = await self._current_user(actor)
        workflow_id = str(payload.get("workflow_id") or "")
        link_id = str(payload.get("link_id") or "")
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return self._memory_error_response(
                SYSTEM_WORKFLOW_MEMORY_LINKS_DELETE_TOOL_ID,
                policy_verdict,
                f"Workflow '{workflow_id}' was not found.",
                actor=actor,
            )
        try:
            self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
        except MemoryPermissionError as exc:
            return self._memory_error_response(SYSTEM_WORKFLOW_MEMORY_LINKS_DELETE_TOOL_ID, policy_verdict, str(exc),
                                               actor=actor)
        links = self._workflow_memory_links(workflow)
        remaining = [link for link in links if link.get("id") != link_id]
        deleted = len(remaining) != len(links)
        if deleted:
            metadata = dict(workflow.metadata)
            metadata[WORKFLOW_MEMORY_LINK_METADATA_KEY] = remaining
            await self.context.workflow_repo.update(workflow_id, {"metadata": metadata})
        return build_tool_run_response(
            verdict=policy_verdict.outcome if deleted else "warn",
            policy_verdict=policy_verdict,
            result={"status": "ok" if deleted else "error", "deleted": deleted, "workflow_id": workflow_id,
                    "link_id": link_id},
            patch=None,
            errors=[] if deleted else [f"Workflow memory link '{link_id}' was not found."],
            dry_run=False,
            actor=actor,
        )

    async def _run_memory_exclusions_list(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_MEMORY_EXCLUSIONS_LIST_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_MEMORY_EXCLUSIONS_LIST_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_MEMORY_EXCLUSIONS_LIST_TOOL_ID, actor=actor)
        current_user = await self._current_user(actor)
        service = MemoryService(self.context)
        try:
            items = await service.list_memory_exclusions(
                memory_id=payload.get("memory_id") if isinstance(payload.get("memory_id"), str) else None,
                target_type=payload.get("target_type") if isinstance(payload.get("target_type"), str) else None,
                target_id=payload.get("target_id") if isinstance(payload.get("target_id"), str) else None,
                current_user=current_user,
            )
        except (MemoryPermissionError, ValueError) as exc:
            return self._memory_error_response(SYSTEM_MEMORY_EXCLUSIONS_LIST_TOOL_ID, policy_verdict, str(exc),
                                               actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "items": items},
            patch=None,
            dry_run=True,
            actor=actor,
        )

    async def _run_memory_exclusions_add(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_MEMORY_EXCLUSIONS_ADD_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_MEMORY_EXCLUSIONS_ADD_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_MEMORY_EXCLUSIONS_ADD_TOOL_ID, actor=actor)
        current_user = await self._current_user(actor)
        service = MemoryService(self.context)
        try:
            exclusion = await service.add_memory_exclusion(
                str(payload.get("memory_id") or ""),
                target_type=str(payload.get("target_type") or ""),
                target_id=payload.get("target_id") if isinstance(payload.get("target_id"), str) else None,
                reason=payload.get("reason") if isinstance(payload.get("reason"), str) else None,
                current_user=current_user,
            )
        except (MemoryPermissionError, ValueError, KeyError) as exc:
            return self._memory_error_response(SYSTEM_MEMORY_EXCLUSIONS_ADD_TOOL_ID, policy_verdict, str(exc),
                                               actor=actor)
        return build_tool_run_response(
            verdict=policy_verdict.outcome,
            policy_verdict=policy_verdict,
            result={"status": "ok", "exclusion": exclusion},
            patch=None,
            dry_run=False,
            actor=actor,
        )

    async def _run_memory_exclusions_delete(self, payload: dict[str, Any], *, actor: str | None) -> ToolRunResponse:
        policy_verdict = self.policy_engine.evaluate(SYSTEM_MEMORY_EXCLUSIONS_DELETE_TOOL_ID, payload, actor=actor)
        self._publish_policy_event(SYSTEM_MEMORY_EXCLUSIONS_DELETE_TOOL_ID, policy_verdict, actor=actor)
        if self.context is None:
            return self._context_required_response(SYSTEM_MEMORY_EXCLUSIONS_DELETE_TOOL_ID, actor=actor)
        current_user = await self._current_user(actor)
        service = MemoryService(self.context)
        try:
            deleted = await service.delete_memory_exclusion(
                str(payload.get("memory_id") or ""),
                str(payload.get("exclusion_id") or ""),
                current_user=current_user,
            )
        except (MemoryPermissionError, ValueError, KeyError) as exc:
            return self._memory_error_response(
                SYSTEM_MEMORY_EXCLUSIONS_DELETE_TOOL_ID,
                policy_verdict,
                str(exc),
                actor=actor,
            )
        return build_tool_run_response(
            verdict=policy_verdict.outcome if deleted else "warn",
            policy_verdict=policy_verdict,
            result={
                "status": "ok" if deleted else "error",
                "deleted": deleted,
                "memory_id": payload.get("memory_id"),
                "exclusion_id": payload.get("exclusion_id"),
            },
            patch=None,
            errors=[] if deleted else [f"Memory exclusion '{payload.get('exclusion_id')}' was not found."],
            dry_run=False,
            actor=actor,
        )

    @staticmethod
    def _workflow_memory_links(workflow) -> list[dict[str, Any]]:
        raw_links = workflow.metadata.get(WORKFLOW_MEMORY_LINK_METADATA_KEY)
        if not isinstance(raw_links, list):
            return []
        return [dict(item) for item in raw_links if isinstance(item, dict)]

    @staticmethod
    def _serialize_workflow_memory_link(workflow_id: str, link: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(link.get("id") or ""),
            "workflowId": workflow_id,
            "targetType": str(link.get("target_type") or ""),
            "targetId": link.get("target_id"),
            "refType": str(link.get("ref_type") or ""),
            "refId": str(link.get("ref_id") or ""),
            "memoryIds": link.get("memory_ids") if isinstance(link.get("memory_ids"), list) else [],
            "accessMode": str(link.get("access_mode") or "read"),
            "label": link.get("label"),
            "createdAt": link.get("created_at"),
            "createdBy": link.get("created_by"),
            "updatedAt": link.get("updated_at"),
            "updatedBy": link.get("updated_by"),
        }

    @staticmethod
    def _normalize_memory_link_value(value: str, allowed: set[str], field_name: str) -> str:
        normalized = value.strip().lower()
        if normalized not in allowed:
            choices = ", ".join(sorted(allowed))
            raise ValueError(f"Unsupported {field_name} '{value}'. Choose one of: {choices}.")
        return normalized

    @staticmethod
    def _validate_memory_link_target(workflow, target_type: str, target_id: str | None) -> None:
        if target_type == "workflow":
            return
        if not target_id:
            raise ValueError("target_id is required for agent and task memory links.")
        if target_type == "agent" and target_id not in {agent.id for agent in workflow.agent_definitions}:
            raise ValueError(f"Agent '{target_id}' not found in workflow")
        if target_type == "task" and target_id not in {task.id for task in workflow.task_definitions}:
            raise ValueError(f"Task '{target_id}' not found in workflow")

    @staticmethod
    def _memory_link_identity(link: dict[str, Any]) -> tuple[str, str | None, str, str, str]:
        return (
            str(link.get("target_type") or ""),
            link.get("target_id") if isinstance(link.get("target_id"), str) else None,
            str(link.get("ref_type") or ""),
            str(link.get("ref_id") or ""),
            str(link.get("access_mode") or "read"),
        )

    @staticmethod
    def _document_id_from_memory_metadata(item: Any) -> str | None:
        metadata = getattr(item, "metadata", None)
        value = metadata.get("document_id") if isinstance(metadata, dict) else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _assert_workflow_owner_or_unclaimed(self, workflow, *, current_user) -> None:
        if current_user is None:
            raise MemoryPermissionError("Authenticated workflow memory access is required.")
        if "admin" in current_user.roles:
            return
        owner_ids = workflow.metadata.get("owner_ids")
        normalized_owner_ids = [item for item in owner_ids if isinstance(item, str)] if isinstance(owner_ids,
                                                                                                   list) else []
        created_by = workflow.metadata.get("created_by")
        if current_user.id in normalized_owner_ids or created_by == current_user.id:
            return
        if not normalized_owner_ids and not created_by:
            return
        raise MemoryPermissionError("Workflow owner access is required.")

    async def _resolve_memory_link_ref(
            self,
            *,
            ref_type: str,
            ref_id: str,
            current_user,
    ) -> tuple[list[str], str | None]:
        if self.context is None:
            raise MemoryPermissionError("API context is required.")
        memory_service = MemoryService(self.context)
        if ref_type == "memory":
            memory = await memory_service.get_memory(ref_id, current_user=current_user)
            if memory is None:
                raise ValueError(f"Memory '{ref_id}' not found")
            return [memory.id], memory.summary or memory.content[:80] or memory.id

        candidates = [
            item
            for item in await self.context.memory_repo.list()
            if self._document_id_from_memory_metadata(item) == ref_id
               and await memory_service.can_read(item, current_user=current_user)
        ]
        if not candidates:
            raise ValueError(f"Document memory collection '{ref_id}' not found")
        candidates.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        representative = candidates[0]
        filename = representative.metadata.get("filename") if isinstance(representative.metadata, dict) else None
        label = filename if isinstance(filename, str) and filename.strip() else f"Document {ref_id}"
        return [item.id for item in candidates], label

    async def _assert_read_write_memory_link(
            self,
            *,
            memory_id: str,
            payload: dict[str, Any],
            current_user,
    ) -> None:
        workflow_id = payload.get("workflow_id") if isinstance(payload.get("workflow_id"), str) else None
        target_type_value = payload.get("target_type") if isinstance(payload.get("target_type"), str) else None
        target_id = payload.get("target_id") if isinstance(payload.get("target_id"), str) else None
        if not workflow_id and not target_type_value and not target_id:
            return
        if self.context is None:
            raise MemoryPermissionError("API context is required.")
        if not workflow_id or not workflow_id.strip():
            raise MemoryPermissionError("workflow_id is required when mutating memory through a workflow link.")
        workflow = await self.context.workflow_repo.get(workflow_id.strip())
        if workflow is None:
            raise MemoryPermissionError(f"Workflow '{workflow_id}' was not found.")
        self._assert_workflow_owner_or_unclaimed(workflow, current_user=current_user)
        target_type = self._normalize_memory_link_value(
            target_type_value or "workflow",
            WORKFLOW_MEMORY_LINK_TARGET_TYPES,
            "target_type",
        )
        normalized_target_id = target_id.strip() if target_id and target_id.strip() else None
        if target_type == "workflow":
            normalized_target_id = None
        self._validate_memory_link_target(workflow, target_type, normalized_target_id)
        for link in self._workflow_memory_links(workflow):
            memory_ids = link.get("memory_ids") if isinstance(link.get("memory_ids"), list) else []
            if memory_id not in memory_ids:
                continue
            if str(link.get("target_type") or "") != target_type:
                continue
            link_target_id = link.get("target_id") if isinstance(link.get("target_id"), str) else None
            if link_target_id != normalized_target_id:
                continue
            if str(link.get("access_mode") or "read") == "read_write":
                return
        raise MemoryPermissionError(
            "Memory mutation through workflow links requires a matching read_write memory link."
        )

    async def _current_user(self, actor: str | None):
        if self.context is None or not actor:
            return None
        if hasattr(self.context.user_repo, "get"):
            return await self.context.user_repo.get(actor)
        return None

    async def _owned_uploaded_document(self, document_id: str, *, current_user):
        if self.context is None:
            return None
        repo = getattr(self.context, "uploaded_document_repo", None)
        if repo is None or not hasattr(repo, "get"):
            return None
        item = await repo.get(document_id)
        if item is None or current_user is None:
            return None
        if "admin" in getattr(current_user, "roles", []):
            return item
        return item if item.created_by_user_id == current_user.id else None

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


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _graph_working_set_context_pack_payload(working_set: Any, payload: dict[str, Any], *, actor: str | None):
    scope = str(payload.get("scope") or MemoryScope.WORKFLOW.value)
    if scope not in {
        MemoryScope.WORKFLOW.value,
        MemoryScope.CONVERSATION.value,
        MemoryScope.WORKSPACE.value,
        MemoryScope.USER.value,
    }:
        return None, f"Unsupported context-pack scope: {scope}"
    summary = _optional_string(payload.get("summary")) or (
        f"Agency Graph working set for {working_set.workflow_id or working_set.execution_id}"
    )
    content = _optional_string(payload.get("content")) or _graph_working_set_context_pack_content(working_set)
    tags = [
        "context_pack",
        "agency_graph",
        "graph_working_set",
        *[tag for tag in _string_list(payload.get("tags")) if tag not in {"context_pack", "agency_graph"}],
    ]
    metadata = {
        "schema_version": "graph_working_set.context_pack.v1",
        "mode": "graph_working_set",
        "graph_context_source": "graph_working_set",
        "graph_working_set_id": working_set.working_set_id,
        "working_set_id": working_set.working_set_id,
        "execution_id": working_set.execution_id,
        "run_id": working_set.run_id,
        "workflow_id": working_set.workflow_id,
        "agent_id": working_set.owner_agent_id,
        "conversation_id": working_set.conversation_id,
        "graph_provenance": _graph_working_set_provenance(working_set),
        "created_from_graph_working_set": True,
    }
    memory_payload = {
        "scope": scope,
        "content": content,
        "summary": summary,
        "source": "graph_working_set",
        "source_execution_id": working_set.execution_id,
        "memory_type": MemoryType.CONTEXT_PACK.value,
        "status": "active",
        "importance": _bounded_int(payload.get("importance") or 45, minimum=0, maximum=100),
        "tags": tags,
        "metadata": metadata,
        "agent_id": working_set.owner_agent_id,
        "sensitive": False,
    }
    if _graph_working_set_provenance_has_sensitive_nodes(metadata["graph_provenance"]):
        memory_payload["sensitive"] = True
        metadata["graph_provenance_contains_sensitive_nodes"] = True
    if scope == MemoryScope.WORKFLOW.value:
        workflow_id = _optional_string(payload.get("workflow_id")) or working_set.workflow_id
        if not workflow_id:
            return None, "workflow_id is required for workflow-scoped context packs."
        memory_payload["workflow_id"] = workflow_id
    elif scope == MemoryScope.CONVERSATION.value:
        conversation_id = _optional_string(payload.get("conversation_id")) or working_set.conversation_id
        if not conversation_id:
            return None, "conversation_id is required for conversation-scoped context packs."
        memory_payload["conversation_id"] = conversation_id
        memory_payload["source_conversation_id"] = conversation_id
    elif scope == MemoryScope.WORKSPACE.value:
        workspace_id = _optional_string(payload.get("workspace_id"))
        if not workspace_id:
            return None, "workspace_id is required for workspace-scoped context packs."
        memory_payload["workspace_id"] = workspace_id
    elif scope == MemoryScope.USER.value:
        created_by_user_id = _optional_string(payload.get("created_by_user_id")) or actor
        if not created_by_user_id:
            return None, "created_by_user_id or actor is required for user-scoped context packs."
        memory_payload["created_by_user_id"] = created_by_user_id
    if actor and "created_by_user_id" not in memory_payload:
        memory_payload["created_by_user_id"] = actor
    return memory_payload, None


def _graph_working_set_context_pack_content(working_set: Any) -> str:
    lines = [
        "# Agency Graph Working Set Context Pack",
        f"working_set_id={working_set.working_set_id}",
        f"workflow_id={working_set.workflow_id or 'none'}",
        f"execution_id={working_set.execution_id or 'none'}",
        f"owner_agent_id={working_set.owner_agent_id or 'none'}",
        "",
        "## Anchors",
        json.dumps(working_set.anchors, sort_keys=True, default=str),
        "",
        "## Selected Nodes",
        json.dumps(working_set.selected_nodes, sort_keys=True, default=str),
        "",
        "## Visited Nodes",
        json.dumps(working_set.visited_nodes, sort_keys=True, default=str),
        "",
        "## Notes",
        json.dumps(working_set.notes, sort_keys=True, default=str),
    ]
    return "\n".join(lines)


def _graph_working_set_provenance(working_set: Any) -> dict[str, Any]:
    return {
        "working_set_id": working_set.working_set_id,
        "anchors": list(working_set.anchors),
        "visited_nodes": list(working_set.visited_nodes),
        "selected_nodes": list(working_set.selected_nodes),
        "notes": list(working_set.notes),
        "node_ids": sorted(
            {
                str(node.get("id"))
                for node in [*working_set.visited_nodes, *working_set.selected_nodes]
                if isinstance(node, dict) and node.get("id")
            }
        ),
        "anchor_ids": sorted(
            {
                str(anchor.get("id"))
                for anchor in working_set.anchors
                if isinstance(anchor, dict) and anchor.get("id")
            }
        ),
    }


def _graph_working_set_provenance_has_sensitive_nodes(provenance: dict[str, Any]) -> bool:
    nodes = [*provenance.get("visited_nodes", []), *provenance.get("selected_nodes", [])]
    return any(
        isinstance(node, dict)
        and (
                node.get("sensitive") is True
                or str(node.get("sensitivity") or "").lower() == "sensitive"
                or str(node.get("type") or "").lower() in {"credential", "secret", "token", "apikey", "api_key"}
        )
        for node in nodes
    )


def _graph_config_or_error(configs: dict[str, dict[str, list[str]]], key: str | None, kind: str) -> dict[str, list[
    str]] | None:
    if not key:
        return None
    try:
        return configs[key]
    except KeyError as exc:
        raise ValueError(f"Unknown graph {kind}: {key}") from exc


async def _run_graph_path_reader(reader: Any, payload: dict[str, Any], *, path_type: str, max_depth: int, limit: int):
    if path_type == "shortest":
        source_id = _required_graph_string(payload, "source_id", path_type)
        target_id = _required_graph_string(payload, "target_id", path_type)
        return await reader.get_shortest_path(
            source_id,
            target_id,
            relationship_types=_string_list(payload.get("relationship_types")),
            max_depth=max_depth,
            limit=limit,
        )
    if path_type == "memory_source_run":
        return await reader.get_memory_source_run_path(
            _required_graph_string(payload, "memory_id", path_type),
            run_id=_optional_string(payload.get("run_id")),
            max_depth=max_depth,
            limit=limit,
        )
    if path_type == "failed_run_root_cause":
        return await reader.get_failed_run_root_cause_path(
            _required_graph_string(payload, "run_id", path_type),
            max_depth=max_depth,
            limit=limit,
        )
    if path_type == "influence":
        return await reader.get_influence_path(
            _required_graph_string(payload, "anchor_id", path_type),
            anchor_type=_required_graph_string(payload, "anchor_type", path_type).lower(),
            workflow_id=_optional_string(payload.get("workflow_id")),
            max_depth=max_depth,
            limit=limit,
        )
    if path_type == "agent_prior_runs":
        return await reader.get_agent_prior_runs_path(
            _required_graph_string(payload, "agent_id", path_type),
            run_id=_optional_string(payload.get("run_id")),
            max_depth=max_depth,
            limit=limit,
        )
    raise ValueError(f"Unknown graph path_type: {path_type}")


def _required_graph_string(payload: dict[str, Any], key: str, path_type: str) -> str:
    value = _optional_string(payload.get(key))
    if value:
        return value
    raise ValueError(f"Graph path '{path_type}' requires {key}")


def _bounded_int(value: Any, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(min(parsed, maximum), minimum)


def _duration_ms(started_at: float) -> int:
    return max(int((perf_counter() - started_at) * 1000), 0)


def _int_meta_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _graph_tool_observability_metadata(
        tool_name: str,
        result: dict[str, Any],
        *,
        actor: str | None,
        duration_ms: int,
        verdict: str,
) -> dict[str, Any]:
    query_meta = _graph_result_query_meta(result)
    status = _graph_result_status(result, query_meta, verdict)
    error = str(result.get("error") or query_meta.get("error") or result.get("summary") or "")
    node_count = _graph_result_count(result, query_meta, key="node_count", list_key="nodes")
    edge_count = _graph_result_count(result, query_meta, key="edge_count", list_key="edges")
    output_bytes = _int_meta_value(query_meta.get("output_bytes")) or _json_size_bytes(result)
    success = verdict == "ok" and status in {"ok", "no_data", ""}
    error_kind = None if success else _graph_error_kind(
        status=status,
        error=error,
        verdict=verdict,
        projection_available=query_meta.get("projection_available"),
    )
    context_pack_created = (
            success
            and tool_name == SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID
            and bool(result.get("context_pack_id"))
    )
    return {
        "tool_id": tool_name,
        "actor": actor,
        "status": status or ("ok" if success else "error"),
        "success": success,
        "error_kind": error_kind,
        "intent": query_meta.get("intent"),
        "mode": query_meta.get("mode"),
        "anchor_type": query_meta.get("anchor_type"),
        "anchor_id": query_meta.get("anchor_id"),
        "depth": query_meta.get("depth"),
        "limit": query_meta.get("limit"),
        "budget": query_meta.get("budget"),
        "node_count": node_count,
        "edge_count": edge_count,
        "output_bytes": output_bytes,
        "duration_ms": duration_ms,
        "projection_available": query_meta.get("projection_available"),
        "fallback_used": bool(query_meta.get("fallback_used")),
        "context_pack_created": context_pack_created,
        "context_pack_id": result.get("context_pack_id") if context_pack_created else None,
        "working_set_id": result.get("working_set_id") or _graph_working_set_id(result),
        "graph_error_counters": _graph_error_counter_payload(error_kind),
        "graph_success_metrics": {
            "calls_by_intent": {str(query_meta.get("intent")): 1} if query_meta.get("intent") else {},
            "calls_by_mode": {str(query_meta.get("mode")): 1} if query_meta.get("mode") else {},
            "node_count": node_count,
            "edge_count": edge_count,
            "output_bytes": output_bytes,
            "context_pack_created": int(context_pack_created),
        },
    }


def _record_graph_tool_counters(operations: Any, metadata: dict[str, Any]) -> None:
    tool_key = _metric_key(str(metadata.get("tool_id") or "unknown"))
    operations.increment("graph_tool.calls")
    operations.increment(f"graph_tool.calls.{tool_key}")
    if metadata.get("success"):
        operations.increment("graph_tool.success")
        operations.increment(f"graph_tool.success.{tool_key}")
    else:
        error_kind = str(metadata.get("error_kind") or "unknown")
        operations.increment("graph_tool.errors")
        operations.increment(f"graph_tool.errors.{error_kind}")
        operations.increment(f"graph_tool.errors.{tool_key}.{error_kind}")
    intent = metadata.get("intent")
    if intent:
        operations.increment(f"graph_tool.intent.{_metric_key(str(intent))}")
    mode = metadata.get("mode")
    if mode:
        operations.increment(f"graph_tool.mode.{_metric_key(str(mode))}")
    node_count = _int_meta_value(metadata.get("node_count"))
    edge_count = _int_meta_value(metadata.get("edge_count"))
    output_bytes = _int_meta_value(metadata.get("output_bytes"))
    operations.increment("graph_tool.output.samples")
    operations.increment("graph_tool.output.node_total", node_count)
    operations.increment("graph_tool.output.edge_total", edge_count)
    operations.increment("graph_tool.output.byte_total", output_bytes)
    if metadata.get("context_pack_created"):
        operations.increment("graph_tool.context_packs.created")


def _graph_result_query_meta(result: dict[str, Any]) -> dict[str, Any]:
    query_meta = result.get("query_meta")
    if isinstance(query_meta, dict):
        return query_meta
    meta = result.get("meta")
    if isinstance(meta, dict):
        return meta
    graph = result.get("graph")
    if isinstance(graph, dict) and isinstance(graph.get("meta"), dict):
        return graph["meta"]
    return {}


def _graph_result_status(result: dict[str, Any], query_meta: dict[str, Any], verdict: str) -> str:
    status = result.get("status") or query_meta.get("status")
    if status:
        return str(status)
    return "ok" if verdict == "ok" else "error"


def _graph_result_count(result: dict[str, Any], query_meta: dict[str, Any], *, key: str, list_key: str) -> int:
    count = _int_meta_value(query_meta.get(key))
    if count:
        return count
    value = result.get(list_key)
    if isinstance(value, list):
        return len(value)
    graph = result.get("graph")
    if isinstance(graph, dict) and isinstance(graph.get(list_key), list):
        return len(graph[list_key])
    return 0


def _graph_error_kind(
        *,
        status: str,
        error: str,
        verdict: str,
        projection_available: Any,
) -> str:
    normalized_status = status.lower()
    normalized_error = error.lower()
    if verdict == "deny" or "access denied" in normalized_error or "permission" in normalized_error:
        return "access_denied"
    if normalized_status == "budget_exceeded":
        return "budget_exceeded"
    if normalized_status == "timeout" or "timeout" in normalized_error or "timed out" in normalized_error:
        return "timeout"
    if normalized_status == "graph_disabled" or "disabled" in normalized_error or "not enabled" in normalized_error:
        return "graph_disabled"
    if normalized_status == "graph_unavailable" or projection_available is False or "unavailable" in normalized_error:
        return "graph_unavailable"
    if normalized_status.startswith(
            "invalid_") or "unknown graph" in normalized_error or "unsupported" in normalized_error:
        return "invalid_request"
    return "unknown"


def _graph_error_counter_payload(error_kind: str | None) -> dict[str, int]:
    counters = {
        "graph_disabled": 0,
        "graph_unavailable": 0,
        "timeout": 0,
        "invalid_request": 0,
        "access_denied": 0,
        "budget_exceeded": 0,
    }
    if error_kind in counters:
        counters[error_kind] = 1
    return counters


def _graph_working_set_id(result: dict[str, Any]) -> str | None:
    working_set = result.get("working_set")
    if isinstance(working_set, dict):
        return _optional_string(working_set.get("working_set_id"))
    return None


def _json_size_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _metric_key(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    return cleaned.strip("_") or "unknown"


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
    monitoring = metadata.get("main_agent_monitoring") if isinstance(metadata.get("main_agent_monitoring"),
                                                                     dict) else {}
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
