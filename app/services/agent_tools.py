"""System tool definitions granted to agents for workflow, memory, tool, command, and execution operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.domain import AgentDefinition, SecuritySettings, ToolDefinition, ToolImplementationReference, ToolType
from app.modules.registry import (
    optional_module_agent_tool_profile_builder,
    optional_module_raw_system_tool_definition_builder,
    optional_module_raw_system_tool_id_builder,
)
from app.tools.definitions import get_tool_catalog_specs
from app.tools.system_runtime_families import graph_runtime_tool_definitions, memory_runtime_tool_definitions
from app.tools.system_specs import (
    AGENT_MANAGEMENT_SYSTEM_TOOL_SPECS,
    COMMAND_SYSTEM_TOOL_SPECS,
    CONNECTOR_SYSTEM_TOOL_SPECS,
    EXECUTION_SYSTEM_TOOL_SPECS,
    GOAL_SYSTEM_TOOL_SPECS,
    TOOL_MANAGEMENT_SYSTEM_TOOL_SPECS,
    WORKFLOW_SYSTEM_TOOL_SPECS,
)

if TYPE_CHECKING:
    from app.api.context import ApiContext


def optional_module_tool_profile_ids(module_key: str, profile: str) -> list[str]:
    builder = optional_module_agent_tool_profile_builder(module_key)
    if builder is None:
        raise ValueError(f"Optional module '{module_key}' tool profile builder is not registered")
    return builder(profile)


def optional_module_system_tool_definitions(module_key: str, **kwargs) -> list[ToolDefinition]:
    builder = optional_module_raw_system_tool_definition_builder(module_key)
    if builder is None:
        return []
    return builder(**kwargs)


def optional_module_system_tool_ids(module_key: str, **kwargs) -> list[str]:
    builder = optional_module_raw_system_tool_id_builder(module_key)
    if builder is None:
        return []
    return builder(**kwargs)


SYSTEM_WORKFLOW_LIST_TOOL_ID = "agency.workflow.list"
SYSTEM_WORKFLOW_GET_TOOL_ID = "agency.workflow.get"
SYSTEM_WORKFLOW_RUN_TOOL_ID = "agency.workflow.run"
SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID = "agency.workflow.propose-create"
SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID = "agency.workflow.propose-update"
SYSTEM_GOAL_LIST_TOOL_ID = "agency.goal.list"
SYSTEM_GOAL_GET_TOOL_ID = "agency.goal.get"
SYSTEM_GOAL_CREATE_TOOL_ID = "agency.goal.create"
SYSTEM_GOAL_UPDATE_TOOL_ID = "agency.goal.update"
SYSTEM_GOAL_PLAN_TOOL_ID = "agency.goal.plan"
SYSTEM_GOAL_REPLAN_TOOL_ID = "agency.goal.replan"
SYSTEM_GOAL_PAUSE_TOOL_ID = "agency.goal.pause"
SYSTEM_GOAL_RESUME_TOOL_ID = "agency.goal.resume"
SYSTEM_GOAL_CANCEL_TOOL_ID = "agency.goal.cancel"
SYSTEM_GOAL_EVIDENCE_ATTACH_TOOL_ID = "agency.goal.evidence.attach"
SYSTEM_GOAL_EVALUATE_TOOL_ID = "agency.goal.evaluate"
SYSTEM_GOAL_SUPERVISOR_FINDINGS_TOOL_ID = "agency.goal.supervisor-findings"
SYSTEM_GOAL_SUPERVISOR_DECISION_RECORD_TOOL_ID = "agency.goal.supervisor-decision.record"
SYSTEM_GOAL_COMPLETE_TOOL_ID = "agency.goal.complete"
SYSTEM_GOAL_TOOL_TARGET = "agency.system.goal"
SYSTEM_SCHEDULE_LIST_TOOL_ID = "agency.schedule.list"
SYSTEM_SCHEDULE_GET_TOOL_ID = "agency.schedule.get"
SYSTEM_SCHEDULE_CREATE_TOOL_ID = "agency.schedule.create"
SYSTEM_SCHEDULE_UPDATE_TOOL_ID = "agency.schedule.update"
SYSTEM_SCHEDULE_DELETE_TOOL_ID = "agency.schedule.delete"
SYSTEM_SCHEDULE_TRIGGER_NOW_TOOL_ID = "agency.schedule.trigger-now"
SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_GET_TOOL_ID = "agency.workflow.runtime-governance.get"
SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_UPDATE_TOOL_ID = "agency.workflow.runtime-governance.update"
SYSTEM_MAIN_AGENT_MONITOR_GET_TOOL_ID = "agency.main-agent.monitor.get"
SYSTEM_MAIN_AGENT_MONITOR_UPDATE_ROUTES_TOOL_ID = "agency.main-agent.monitor.update-routes"
SYSTEM_WORKFLOW_MONITORING_EVENTS_TOOL_ID = "agency.workflow.monitoring.events"
SYSTEM_WORKFLOW_MONITOR_PROPOSAL_DISPATCH_TOOL_ID = "agency.workflow.monitor-proposal.dispatch"
SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSALS_TOOL_ID = "agency.workflow.improvement-proposals"
SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_CREATE_TOOL_ID = "agency.workflow.improvement-proposal.create"
SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_UPDATE_TOOL_ID = "agency.workflow.improvement-proposal.update"
SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_REQUEST_APPROVAL_TOOL_ID = "agency.workflow.improvement-proposal.request-approval"
SYSTEM_WORKFLOW_STEERING_APPROVALS_TOOL_ID = "agency.workflow.steering-approvals"
SYSTEM_WORKFLOW_STEERING_APPROVAL_CREATE_TOOL_ID = "agency.workflow.steering-approval.create"
SYSTEM_WORKFLOW_STEERING_APPROVAL_UPDATE_TOOL_ID = "agency.workflow.steering-approval.update"
SYSTEM_WORKFLOW_STEERING_APPROVAL_REQUEST_APPROVAL_TOOL_ID = "agency.workflow.steering-approval.request-approval"
SYSTEM_WORKFLOW_GOVERNANCE_AUDIT_TOOL_ID = "agency.workflow.governance.audit"
SYSTEM_WORKFLOW_GOVERNANCE_REPAIR_TOOL_ID = "agency.workflow.governance.repair"
SYSTEM_WORKFLOW_GOVERNANCE_REMEDIATE_TOOL_ID = "agency.workflow.governance.remediate"
SYSTEM_WORKFLOW_GOVERNANCE_REVIEW_QUEUE_TOOL_ID = "agency.workflow.governance.review-queue"
SYSTEM_WORKFLOW_GOVERNANCE_ACTION_TOOL_ID = "agency.workflow.governance.act"
SYSTEM_WORKFLOW_GOVERNANCE_DOCUMENT_SUGGEST_TOOL_ID = "agency.workflow.governance.document-suggest"
SYSTEM_WORKFLOW_GOVERNANCE_BUNDLE_TOOL_ID = "agency.workflow.governance.bundle"
SYSTEM_WORKFLOW_DOCUMENT_LINKS_TOOL_ID = "agency.workflow.document-links"
SYSTEM_WORKFLOW_DOCUMENT_LINK_ADD_TOOL_ID = "agency.workflow.document-link.add"
SYSTEM_WORKFLOW_DOCUMENT_LINK_DELETE_TOOL_ID = "agency.workflow.document-link.delete"
SYSTEM_WORKFLOW_DOCUMENT_SUMMARY_TOOL_ID = "agency.workflow.document-summary.get"
SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACES_TOOL_ID = "agency.workflow.shared-memory.namespaces"
SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_CREATE_TOOL_ID = "agency.workflow.shared-memory.namespace.create"
SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_UPDATE_TOOL_ID = "agency.workflow.shared-memory.namespace.update"
SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_DELETE_TOOL_ID = "agency.workflow.shared-memory.namespace.delete"
SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORIES_TOOL_ID = "agency.workflow.shared-memory.namespace.memories"
SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_ADD_TOOL_ID = "agency.workflow.shared-memory.namespace.memory.add"
SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_REMOVE_TOOL_ID = "agency.workflow.shared-memory.namespace.memory.remove"
SYSTEM_OBSERVABILITY_WORKFLOW_METRICS_TOOL_ID = "agency.observability.workflow.metrics"
SYSTEM_OBSERVABILITY_EXECUTION_TIMELINE_TOOL_ID = "agency.observability.execution.timeline"
SYSTEM_DOCUMENTS_LIST_TOOL_ID = "agency.documents.list"
SYSTEM_DOCUMENTS_GET_TOOL_ID = "agency.documents.get"
SYSTEM_DOCUMENTS_DELETE_TOOL_ID = "agency.documents.delete"
SYSTEM_WORKFLOW_TOOL_TARGET = "agency.system.workflow"
SYSTEM_TOOL_LIST_TOOL_ID = "agency.tool.list"
SYSTEM_TOOL_GET_TOOL_ID = "agency.tool.get"
SYSTEM_TOOL_PROPOSE_CREATE_TOOL_ID = "agency.tool.propose-create"
SYSTEM_TOOL_PROPOSE_UPDATE_TOOL_ID = "agency.tool.propose-update"
SYSTEM_TOOL_WORKSPACE_LIST_TOOL_ID = "agency.tool.workspace.list"
SYSTEM_TOOL_WORKSPACE_SCAFFOLD_TOOL_ID = "agency.tool.workspace.scaffold"
SYSTEM_TOOL_WORKSPACE_PUBLISH_TOOL_ID = "agency.tool.workspace.publish"
SYSTEM_TOOL_MANAGEMENT_TARGET = "agency.system.tool"
SYSTEM_AGENT_LIST_TOOL_ID = "agency.agent.list"
SYSTEM_AGENT_GET_TOOL_ID = "agency.agent.get"
SYSTEM_AGENT_PROPOSE_UPDATE_TOOL_ID = "agency.agent.propose-update"
SYSTEM_AGENT_MANAGEMENT_TARGET = "agency.system.agent"
SYSTEM_CONNECTOR_CAPABILITIES_TOOL_ID = "agency.connector.capabilities"
SYSTEM_CONNECTOR_CREDENTIALS_TOOL_ID = "agency.connector.credentials"
SYSTEM_CONNECTOR_RESOLVE_TOOL_ID = "agency.connector.resolve"
SYSTEM_CONNECTOR_HISTORY_TOOL_ID = "agency.connector.history"
SYSTEM_CONNECTOR_TEST_TOOL_ID = "agency.connector.test"
SYSTEM_CONNECTOR_TOOL_TARGET = "agency.system.connector"
SYSTEM_MEMORY_LIST_TOOL_ID = "agency.memory.list"
SYSTEM_MEMORY_CATALOG_TOOL_ID = "agency.memory.catalog"
SYSTEM_MEMORY_REMEMBER_TOOL_ID = "agency.memory.remember"
SYSTEM_MEMORY_UPDATE_TOOL_ID = "agency.memory.update"
SYSTEM_MEMORY_DELETE_TOOL_ID = "agency.memory.delete"
SYSTEM_MEMORY_EXCLUSIONS_LIST_TOOL_ID = "agency.memory.exclusions.list"
SYSTEM_MEMORY_EXCLUSIONS_ADD_TOOL_ID = "agency.memory.exclusions.add"
SYSTEM_MEMORY_EXCLUSIONS_DELETE_TOOL_ID = "agency.memory.exclusions.delete"
SYSTEM_WORKFLOW_MEMORY_LINKS_LIST_TOOL_ID = "agency.workflow.memory-links.list"
SYSTEM_WORKFLOW_MEMORY_LINKS_ADD_TOOL_ID = "agency.workflow.memory-links.add"
SYSTEM_WORKFLOW_MEMORY_LINKS_DELETE_TOOL_ID = "agency.workflow.memory-links.delete"
SYSTEM_MEMORY_TOOL_TARGET = "agency.system.memory"
SYSTEM_GRAPH_CONTEXT_TOOL_ID = "agency.graph.context"
SYSTEM_GRAPH_SEARCH_TOOL_ID = "agency.graph.search"
SYSTEM_GRAPH_EXPAND_TOOL_ID = "agency.graph.expand"
SYSTEM_GRAPH_NEIGHBORS_TOOL_ID = "agency.graph.neighbors"
SYSTEM_GRAPH_PATH_TOOL_ID = "agency.graph.path"
SYSTEM_GRAPH_SUMMARIZE_SUBGRAPH_TOOL_ID = "agency.graph.summarize-subgraph"
SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID = "agency.graph.working-set.create"
SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID = "agency.graph.working-set.add"
SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID = "agency.graph.working-set.remove"
SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID = "agency.graph.working-set.summarize"
SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID = "agency.graph.working-set.clear"
SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID = "agency.graph.working-set.persist-context-pack"
SYSTEM_GRAPH_TOOL_TARGET = "agency.system.graph"
SYSTEM_EXECUTION_GET_TOOL_ID = "agency.execution.get"
SYSTEM_EXECUTION_LIST_TOOL_ID = "agency.execution.list"
SYSTEM_EXECUTION_EVENTS_TOOL_ID = "agency.execution.events"
SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID = "agency.execution.artifacts"
SYSTEM_EXECUTION_PAUSE_TOOL_ID = "agency.execution.pause"
SYSTEM_EXECUTION_RESUME_TOOL_ID = "agency.execution.resume"
SYSTEM_EXECUTION_CANCEL_TOOL_ID = "agency.execution.cancel"
SYSTEM_EXECUTION_APPROVALS_TOOL_ID = "agency.execution.approvals"
SYSTEM_EXECUTION_APPROVE_TOOL_ID = "agency.execution.approve"
SYSTEM_EXECUTION_REJECT_TOOL_ID = "agency.execution.reject"

DEFAULT_MAIN_AGENT_SPEECH_TOOL_IDS = (
    "agency.speech.listen",
    "agency.speech.speak",
    "agency.speech.continue",
    "agency.voice.generate",
)
SYSTEM_EXECUTION_TOOL_TARGET = "agency.system.execution"
SYSTEM_COMMAND_RUN_TOOL_ID = "agency.command.run"
SYSTEM_COMMAND_TOOL_TARGET = "agency.system.command"
LEGACY_MAIN_AGENT_INTERNAL_TOOL_PREFIX = "__main_agent__:"
SYSTEM_SCHEMA_FILLED_BY_KEY = "x-agency-filled-by"
SYSTEM_SCHEMA_USER_VISIBLE_KEY = "x-agency-user-visible"

ITEMS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object"},
            "description": "List of matching records.",
        },
    },
    "required": ["items"],
    "additionalProperties": True,
}


def _spec_tool_ids(specs: list[dict[str, Any]]) -> list[str]:
    return [str(spec["id"]) for spec in specs if isinstance(spec, dict) and spec.get("id")]


RESULT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"description": "Tool-specific result payload."},
    },
    "required": ["result"],
    "additionalProperties": True,
}

PROPOSAL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "approval_request": {
            "type": "object",
            "description": "Approval request created for the proposed mutation.",
        },
        "preview": {
            "type": "object",
            "description": "Preview of the proposed workflow or tool mutation.",
        },
        "error": {
            "type": "string",
            "description": "Validation or policy error if the proposal was rejected before approval.",
        },
    },
    "additionalProperties": True,
}

COMMAND_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "description": "ok for exit code 0, error for non-zero exit."},
        "stdout": {"type": "string", "description": "Captured standard output, possibly truncated."},
        "stderr": {"type": "string", "description": "Captured standard error, possibly truncated."},
        "exit_code": {"type": "integer", "description": "Process exit code."},
        "duration_ms": {"type": "integer", "description": "Command runtime in milliseconds."},
        "output_text": {"type": "string", "description": "LLM-facing combined command output."},
        "truncated": {"type": "boolean", "description": "Whether output was truncated."},
        "overflow_path": {
            "type": ["string", "null"],
            "description": "Path to full captured output when truncation occurred.",
        },
    },
    "required": ["status", "stdout", "stderr", "exit_code", "duration_ms", "output_text", "truncated"],
    "additionalProperties": True,
}

GRAPH_CONTEXT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "description": "ok, validation error, or graph availability status."},
        "summary": {"type": "string", "description": "Compact summary of the graph context returned."},
        "facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Deterministic facts synthesized from graph nodes and relationships.",
        },
        "related_memories": {"type": "array", "items": {"type": "object"}},
        "related_documents": {"type": "array", "items": {"type": "object"}},
        "recent_events": {"type": "array", "items": {"type": "object"}},
        "prior_attempts": {"type": "array", "items": {"type": "object"}},
        "failures": {"type": "array", "items": {"type": "object"}},
        "decisions": {"type": "array", "items": {"type": "object"}},
        "constraints": {"type": "array", "items": {"type": "object"}},
        "open_questions": {"type": "array", "items": {"type": "object"}},
        "next_actions": {"type": "array", "items": {"type": "object"}},
        "provenance": {"type": "object"},
        "graph": {"type": ["object", "null"]},
        "omitted": {"type": "object"},
        "query_meta": {"type": "object"},
    },
    "required": ["status", "summary", "facts", "provenance", "omitted", "query_meta"],
    "additionalProperties": True,
}

GRAPH_DOCUMENT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "nodes": {"type": "array", "items": {"type": "object"}},
        "edges": {"type": "array", "items": {"type": "object"}},
        "meta": {"type": "object"},
    },
    "required": ["nodes", "edges", "meta"],
    "additionalProperties": True,
}

GRAPH_NEIGHBORS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "center": {"type": ["object", "null"], "description": "Expanded center node, if found in the result."},
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "relationship_type": {"type": "string"},
                    "direction": {"type": "string", "enum": ["incoming", "outgoing", "self"]},
                    "node_type": {"type": "string"},
                    "count": {"type": "integer"},
                    "nodes": {"type": "array", "items": {"type": "object"}},
                    "edges": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["relationship_type", "direction", "node_type", "count", "nodes", "edges"],
                "additionalProperties": True,
            },
            "description": "One-hop neighbors grouped by relationship type, direction, and neighbor node type.",
        },
        "nodes": {"type": "array", "items": {"type": "object"}},
        "edges": {"type": "array", "items": {"type": "object"}},
        "meta": {"type": "object"},
    },
    "required": ["center", "groups", "nodes", "edges", "meta"],
    "additionalProperties": True,
}

SYSTEM_TOOL_INPUT_CONTRACTS: dict[str, dict[str, dict[str, Any]]] = {
    SYSTEM_WORKFLOW_GET_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_RUN_TOOL_ID: {
        "workflow_id": {"filled_by": "user"},
        "input_payload": {"filled_by": "user_or_agent"},
        "runtime_adapter_id": {"filled_by": "user_or_agent"},
        "goal_id": {"filled_by": "user_or_agent"},
        "conversation_id": {"filled_by": "agent", "user_visible": False},
        "origin_message_id": {"filled_by": "agent", "user_visible": False},
    },
    SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID: {
        "summary": {"filled_by": "agent"},
        "diff_summary": {"filled_by": "agent"},
        "goal": {"filled_by": "agent"},
        "conversation_history": {"filled_by": "agent", "user_visible": False},
        "model_profile_id": {"filled_by": "user_or_agent"},
        "restart_active_executions": {"filled_by": "user_or_agent"},
        "workflow": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID: {
        "workflow_id": {"filled_by": "user"},
        "summary": {"filled_by": "agent"},
        "diff_summary": {"filled_by": "agent"},
        "goal": {"filled_by": "agent"},
        "conversation_history": {"filled_by": "agent", "user_visible": False},
        "model_profile_id": {"filled_by": "user_or_agent"},
        "workflow": {"filled_by": "agent"},
    },
    SYSTEM_GOAL_LIST_TOOL_ID: {
        "status": {"filled_by": "user_or_agent"},
        "parent_goal_id": {"filled_by": "user_or_agent"},
        "active_only": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GOAL_GET_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GOAL_CREATE_TOOL_ID: {
        "objective": {"filled_by": "agent"},
        "priority": {"filled_by": "agent"},
        "success_criteria": {"filled_by": "agent"},
        "constraints": {"filled_by": "agent"},
        "parent_goal_id": {"filled_by": "user_or_agent"},
        "metadata": {"filled_by": "agent"},
    },
    SYSTEM_GOAL_UPDATE_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
        "patch": {"filled_by": "agent"},
    },
    SYSTEM_GOAL_PLAN_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
        "plan": {"filled_by": "agent"},
        "reason": {"filled_by": "agent"},
    },
    SYSTEM_GOAL_REPLAN_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
        "plan": {"filled_by": "agent"},
        "reason": {"filled_by": "agent"},
    },
    SYSTEM_GOAL_PAUSE_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GOAL_RESUME_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GOAL_CANCEL_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
        "reason": {"filled_by": "agent"},
    },
    SYSTEM_GOAL_EVIDENCE_ATTACH_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
        "evidence": {"filled_by": "agent"},
    },
    SYSTEM_GOAL_EVALUATE_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
        "evidence": {"filled_by": "agent"},
        "persist": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GOAL_SUPERVISOR_FINDINGS_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GOAL_SUPERVISOR_DECISION_RECORD_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
        "decision": {"filled_by": "agent"},
    },
    SYSTEM_GOAL_COMPLETE_TOOL_ID: {
        "goal_id": {"filled_by": "user_or_agent"},
        "evidence": {"filled_by": "agent"},
        "evaluation": {"filled_by": "agent"},
    },
    SYSTEM_SCHEDULE_LIST_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "enabled": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_SCHEDULE_GET_TOOL_ID: {
        "schedule_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_SCHEDULE_CREATE_TOOL_ID: {
        "schedule": {"filled_by": "agent"},
    },
    SYSTEM_SCHEDULE_UPDATE_TOOL_ID: {
        "schedule_id": {"filled_by": "user_or_agent"},
        "patch": {"filled_by": "agent"},
    },
    SYSTEM_SCHEDULE_DELETE_TOOL_ID: {
        "schedule_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_SCHEDULE_TRIGGER_NOW_TOOL_ID: {
        "schedule_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_GET_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_RUNTIME_GOVERNANCE_UPDATE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "patch": {"filled_by": "agent"},
    },
    SYSTEM_MAIN_AGENT_MONITOR_UPDATE_ROUTES_TOOL_ID: {
        "patch": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_MONITORING_EVENTS_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_MONITOR_PROPOSAL_DISPATCH_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "proposal_event_id": {"filled_by": "agent"},
        "operator_note": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSALS_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "proposal_id": {"filled_by": "user_or_agent"},
        "status": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_CREATE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "title": {"filled_by": "agent"},
        "summary": {"filled_by": "agent"},
        "status": {"filled_by": "agent"},
        "priority": {"filled_by": "agent"},
        "proposal_kind": {"filled_by": "agent"},
        "created_by": {"filled_by": "agent"},
        "execution_id": {"filled_by": "user_or_agent"},
        "finding_event_id": {"filled_by": "agent"},
        "proposal_event_id": {"filled_by": "agent"},
        "approval_request_id": {"filled_by": "agent"},
        "diagnosis": {"filled_by": "agent"},
        "proposed_change": {"filled_by": "agent"},
        "expected_benefit": {"filled_by": "agent"},
        "risk": {"filled_by": "agent"},
        "validation_plan": {"filled_by": "agent"},
        "rollback_plan": {"filled_by": "agent"},
        "tags": {"filled_by": "agent"},
        "metadata": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_UPDATE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "proposal_id": {"filled_by": "user_or_agent"},
        "patch": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_IMPROVEMENT_PROPOSAL_REQUEST_APPROVAL_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "proposal_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_STEERING_APPROVALS_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "approval_id": {"filled_by": "user_or_agent"},
        "status": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_STEERING_APPROVAL_CREATE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "title": {"filled_by": "agent"},
        "status": {"filled_by": "agent"},
        "recommended_action": {"filled_by": "agent"},
        "reason": {"filled_by": "agent"},
        "created_by": {"filled_by": "agent"},
        "execution_id": {"filled_by": "user_or_agent"},
        "finding_event_id": {"filled_by": "agent"},
        "steering_request_event_id": {"filled_by": "agent"},
        "approval_request_id": {"filled_by": "agent"},
        "target_task_id": {"filled_by": "agent"},
        "target_agent_id": {"filled_by": "agent"},
        "operator_parameters": {"filled_by": "agent"},
        "evidence": {"filled_by": "agent"},
        "policy": {"filled_by": "agent"},
        "metadata": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_STEERING_APPROVAL_UPDATE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "approval_id": {"filled_by": "user_or_agent"},
        "patch": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_STEERING_APPROVAL_REQUEST_APPROVAL_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "approval_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_GOVERNANCE_AUDIT_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_GOVERNANCE_REPAIR_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "record_kind": {"filled_by": "agent"},
        "record_id": {"filled_by": "user_or_agent"},
        "action": {"filled_by": "agent"},
        "approval_request_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_GOVERNANCE_REMEDIATE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "dry_run": {"filled_by": "user_or_agent"},
        "sync_status_mismatches": {"filled_by": "agent"},
        "clear_orphaned_references": {"filled_by": "agent"},
        "adopt_orphaned_approvals": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_GOVERNANCE_REVIEW_QUEUE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_GOVERNANCE_ACTION_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "action": {"filled_by": "agent"},
        "record_kind": {"filled_by": "agent"},
        "record_id": {"filled_by": "user_or_agent"},
        "document_id": {"filled_by": "user_or_agent"},
        "label": {"filled_by": "agent"},
        "summary": {"filled_by": "agent"},
        "linked_by": {"filled_by": "agent"},
        "metadata": {"filled_by": "agent"},
        "sync_status_mismatches": {"filled_by": "agent"},
        "clear_orphaned_references": {"filled_by": "agent"},
        "adopt_orphaned_approvals": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_GOVERNANCE_DOCUMENT_SUGGEST_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "record_kind": {"filled_by": "agent"},
        "record_id": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_GOVERNANCE_BUNDLE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "record_kind": {"filled_by": "agent"},
        "record_id": {"filled_by": "user_or_agent"},
        "attach_top_suggestion": {"filled_by": "agent"},
        "request_approval": {"filled_by": "agent"},
        "document_limit": {"filled_by": "user_or_agent"},
        "evidence_label": {"filled_by": "agent"},
        "evidence_summary": {"filled_by": "agent"},
        "metadata": {"filled_by": "agent"},
        "dry_run": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_DOCUMENT_LINKS_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "link_id": {"filled_by": "user_or_agent"},
        "target_type": {"filled_by": "user_or_agent"},
        "target_id": {"filled_by": "user_or_agent"},
        "document_id": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_DOCUMENT_LINK_ADD_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "document_id": {"filled_by": "user_or_agent"},
        "target_type": {"filled_by": "agent"},
        "target_id": {"filled_by": "agent"},
        "label": {"filled_by": "agent"},
        "summary": {"filled_by": "agent"},
        "linked_by": {"filled_by": "agent"},
        "metadata": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_DOCUMENT_LINK_DELETE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "link_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_DOCUMENT_SUMMARY_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "document_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACES_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "namespace_id": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_CREATE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "name": {"filled_by": "agent"},
        "description": {"filled_by": "agent"},
        "status": {"filled_by": "agent"},
        "target_type": {"filled_by": "agent"},
        "target_id": {"filled_by": "agent"},
        "memory_scope": {"filled_by": "agent"},
        "tags": {"filled_by": "agent"},
        "memory_ids": {"filled_by": "agent"},
        "created_by": {"filled_by": "agent"},
        "metadata": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_UPDATE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "namespace_id": {"filled_by": "user_or_agent"},
        "patch": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_DELETE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "namespace_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORIES_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "namespace_id": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_ADD_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "namespace_id": {"filled_by": "user_or_agent"},
        "memory_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_SHARED_MEMORY_NAMESPACE_MEMORY_REMOVE_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "namespace_id": {"filled_by": "user_or_agent"},
        "memory_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_OBSERVABILITY_WORKFLOW_METRICS_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_OBSERVABILITY_EXECUTION_TIMELINE_TOOL_ID: {
        "execution_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_DOCUMENTS_LIST_TOOL_ID: {
        "conversation_id": {"filled_by": "user_or_agent"},
        "workflow_id": {"filled_by": "user_or_agent"},
        "agent_id": {"filled_by": "user_or_agent"},
        "scope": {"filled_by": "user_or_agent"},
        "upload_mode": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_DOCUMENTS_GET_TOOL_ID: {
        "document_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_DOCUMENTS_DELETE_TOOL_ID: {
        "document_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_TOOL_GET_TOOL_ID: {
        "tool_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_TOOL_PROPOSE_CREATE_TOOL_ID: {
        "summary": {"filled_by": "agent"},
        "diff_summary": {"filled_by": "agent"},
        "tool": {"filled_by": "agent"},
    },
    SYSTEM_TOOL_PROPOSE_UPDATE_TOOL_ID: {
        "tool_id": {"filled_by": "user"},
        "summary": {"filled_by": "agent"},
        "diff_summary": {"filled_by": "agent"},
        "goal": {"filled_by": "agent"},
        "patch": {"filled_by": "agent"},
        "tool": {"filled_by": "agent"},
    },
    SYSTEM_TOOL_WORKSPACE_LIST_TOOL_ID: {
        "package_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_TOOL_WORKSPACE_SCAFFOLD_TOOL_ID: {
        "package_id": {"filled_by": "agent"},
        "name": {"filled_by": "agent"},
        "description": {"filled_by": "agent"},
        "function_name": {"filled_by": "agent"},
        "overwrite": {"filled_by": "user_or_agent"},
    },
    SYSTEM_TOOL_WORKSPACE_PUBLISH_TOOL_ID: {
        "package_id": {"filled_by": "agent"},
        "tool_id": {"filled_by": "agent"},
        "name": {"filled_by": "agent"},
        "display_name": {"filled_by": "agent"},
        "description": {"filled_by": "agent"},
        "callable_name": {"filled_by": "agent"},
        "input_schema": {"filled_by": "agent"},
        "output_schema": {"filled_by": "agent"},
        "tags": {"filled_by": "agent"},
        "security": {"filled_by": "agent"},
    },
    SYSTEM_AGENT_GET_TOOL_ID: {
        "agent_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_AGENT_PROPOSE_UPDATE_TOOL_ID: {
        "agent_id": {"filled_by": "user"},
        "summary": {"filled_by": "agent"},
        "diff_summary": {"filled_by": "agent"},
        "goal": {"filled_by": "agent"},
        "patch": {"filled_by": "agent"},
        "agent": {"filled_by": "agent"},
    },
    SYSTEM_MEMORY_LIST_TOOL_ID: {
        "scope": {"filled_by": "user_or_agent"},
        "query": {"filled_by": "agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_MEMORY_CATALOG_TOOL_ID: {
        "scope": {"filled_by": "user_or_agent"},
        "workflow_id": {"filled_by": "agent", "user_visible": False},
        "agent_id": {"filled_by": "agent", "user_visible": False},
        "conversation_id": {"filled_by": "agent", "user_visible": False},
        "target_type": {"filled_by": "user_or_agent"},
        "target_id": {"filled_by": "agent", "user_visible": False},
        "query": {"filled_by": "agent"},
        "include_sensitive": {"filled_by": "user_or_agent"},
        "status": {"filled_by": "user_or_agent"},
        "limit_per_group": {"filled_by": "user_or_agent"},
    },
    SYSTEM_MEMORY_REMEMBER_TOOL_ID: {
        "scope": {"filled_by": "user_or_agent"},
        "content": {"filled_by": "agent"},
        "summary": {"filled_by": "agent"},
        "tags": {"filled_by": "agent"},
        "sensitive": {"filled_by": "user_or_agent"},
        "confirmed": {"filled_by": "user_or_agent"},
        "workspace_id": {"filled_by": "agent", "user_visible": False},
        "conversation_id": {"filled_by": "agent", "user_visible": False},
        "workflow_id": {"filled_by": "agent", "user_visible": False},
    },
    SYSTEM_MEMORY_UPDATE_TOOL_ID: {
        "memory_id": {"filled_by": "agent"},
        "content": {"filled_by": "agent"},
        "summary": {"filled_by": "agent"},
        "tags": {"filled_by": "agent"},
        "sensitive": {"filled_by": "user_or_agent"},
        "confirmed": {"filled_by": "user_or_agent"},
        "workflow_id": {"filled_by": "agent", "user_visible": False},
        "target_type": {"filled_by": "agent", "user_visible": False},
        "target_id": {"filled_by": "agent", "user_visible": False},
    },
    SYSTEM_MEMORY_DELETE_TOOL_ID: {
        "memory_id": {"filled_by": "agent"},
        "workflow_id": {"filled_by": "agent", "user_visible": False},
        "target_type": {"filled_by": "agent", "user_visible": False},
        "target_id": {"filled_by": "agent", "user_visible": False},
    },
    SYSTEM_MEMORY_EXCLUSIONS_LIST_TOOL_ID: {
        "memory_id": {"filled_by": "agent"},
        "target_type": {"filled_by": "agent", "user_visible": False},
        "target_id": {"filled_by": "agent", "user_visible": False},
    },
    SYSTEM_MEMORY_EXCLUSIONS_ADD_TOOL_ID: {
        "memory_id": {"filled_by": "agent"},
        "target_type": {"filled_by": "agent", "user_visible": False},
        "target_id": {"filled_by": "agent", "user_visible": False},
        "reason": {"filled_by": "agent"},
    },
    SYSTEM_MEMORY_EXCLUSIONS_DELETE_TOOL_ID: {
        "memory_id": {"filled_by": "agent"},
        "exclusion_id": {"filled_by": "agent"},
    },
    SYSTEM_WORKFLOW_MEMORY_LINKS_LIST_TOOL_ID: {
        "workflow_id": {"filled_by": "user"},
    },
    SYSTEM_WORKFLOW_MEMORY_LINKS_ADD_TOOL_ID: {
        "workflow_id": {"filled_by": "user"},
        "target_type": {"filled_by": "user_or_agent"},
        "target_id": {"filled_by": "user_or_agent"},
        "ref_type": {"filled_by": "user_or_agent"},
        "ref_id": {"filled_by": "user_or_agent"},
        "access_mode": {"filled_by": "user_or_agent"},
        "label": {"filled_by": "user_or_agent"},
    },
    SYSTEM_WORKFLOW_MEMORY_LINKS_DELETE_TOOL_ID: {
        "workflow_id": {"filled_by": "user"},
        "link_id": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GRAPH_CONTEXT_TOOL_ID: {
        "query": {"filled_by": "agent"},
        "intent": {"filled_by": "agent"},
        "anchor_type": {"filled_by": "agent"},
        "anchor_id": {"filled_by": "agent"},
        "scope": {"filled_by": "agent"},
        "mode": {"filled_by": "user_or_agent"},
        "include_memories": {"filled_by": "user_or_agent"},
        "include_events": {"filled_by": "user_or_agent"},
        "include_raw_graph": {"filled_by": "user_or_agent"},
        "budget": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GRAPH_SEARCH_TOOL_ID: {
        "query": {"filled_by": "agent"},
        "labels": {"filled_by": "agent"},
        "node_types": {"filled_by": "agent"},
        "workflow_id": {"filled_by": "agent", "user_visible": False},
        "agent_id": {"filled_by": "agent", "user_visible": False},
        "tool_id": {"filled_by": "agent", "user_visible": False},
        "document_id": {"filled_by": "agent", "user_visible": False},
        "entity_id": {"filled_by": "agent", "user_visible": False},
        "error_text": {"filled_by": "agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GRAPH_EXPAND_TOOL_ID: {
        "node_id": {"filled_by": "agent"},
        "preset": {"filled_by": "agent"},
        "mode": {"filled_by": "user_or_agent"},
        "labels": {"filled_by": "agent"},
        "relationship_types": {"filled_by": "agent"},
        "depth": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
        "include_deleted": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GRAPH_NEIGHBORS_TOOL_ID: {
        "node_id": {"filled_by": "agent"},
        "preset": {"filled_by": "agent"},
        "mode": {"filled_by": "user_or_agent"},
        "labels": {"filled_by": "agent"},
        "relationship_types": {"filled_by": "agent"},
        "limit": {"filled_by": "user_or_agent"},
        "include_deleted": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GRAPH_PATH_TOOL_ID: {
        "path_type": {"filled_by": "agent"},
        "source_id": {"filled_by": "agent"},
        "target_id": {"filled_by": "agent"},
        "relationship_types": {"filled_by": "agent"},
        "memory_id": {"filled_by": "agent", "user_visible": False},
        "run_id": {"filled_by": "agent", "user_visible": False},
        "anchor_type": {"filled_by": "agent"},
        "anchor_id": {"filled_by": "agent"},
        "workflow_id": {"filled_by": "agent", "user_visible": False},
        "agent_id": {"filled_by": "agent", "user_visible": False},
        "max_depth": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GRAPH_SUMMARIZE_SUBGRAPH_TOOL_ID: {
        "nodes": {"filled_by": "agent"},
        "edges": {"filled_by": "agent"},
        "meta": {"filled_by": "agent"},
        "query": {"filled_by": "agent"},
        "intent": {"filled_by": "agent"},
        "anchor_type": {"filled_by": "agent"},
        "anchor_id": {"filled_by": "agent"},
        "scope": {"filled_by": "agent"},
        "mode": {"filled_by": "user_or_agent"},
        "include_memories": {"filled_by": "user_or_agent"},
        "include_events": {"filled_by": "user_or_agent"},
        "include_raw_graph": {"filled_by": "user_or_agent"},
        "budget": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GRAPH_WORKING_SET_CREATE_TOOL_ID: {
        "execution_id": {"filled_by": "agent", "user_visible": False},
        "working_set_id": {"filled_by": "agent", "user_visible": False},
        "owner_agent_id": {"filled_by": "agent", "user_visible": False},
        "conversation_id": {"filled_by": "agent", "user_visible": False},
        "workflow_id": {"filled_by": "agent", "user_visible": False},
        "run_id": {"filled_by": "agent", "user_visible": False},
        "anchors": {"filled_by": "agent"},
        "notes": {"filled_by": "agent"},
        "ttl_seconds": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GRAPH_WORKING_SET_ADD_TOOL_ID: {
        "execution_id": {"filled_by": "agent", "user_visible": False},
        "working_set_id": {"filled_by": "agent", "user_visible": False},
        "anchors": {"filled_by": "agent"},
        "visited_nodes": {"filled_by": "agent"},
        "selected_nodes": {"filled_by": "agent"},
        "notes": {"filled_by": "agent"},
        "ttl_seconds": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GRAPH_WORKING_SET_REMOVE_TOOL_ID: {
        "execution_id": {"filled_by": "agent", "user_visible": False},
        "working_set_id": {"filled_by": "agent", "user_visible": False},
        "anchor_ids": {"filled_by": "agent"},
        "visited_node_ids": {"filled_by": "agent"},
        "selected_node_ids": {"filled_by": "agent"},
        "clear_notes": {"filled_by": "agent"},
        "ttl_seconds": {"filled_by": "user_or_agent"},
    },
    SYSTEM_GRAPH_WORKING_SET_SUMMARIZE_TOOL_ID: {
        "execution_id": {"filled_by": "agent", "user_visible": False},
        "working_set_id": {"filled_by": "agent", "user_visible": False},
    },
    SYSTEM_GRAPH_WORKING_SET_CLEAR_TOOL_ID: {
        "execution_id": {"filled_by": "agent", "user_visible": False},
        "working_set_id": {"filled_by": "agent", "user_visible": False},
    },
    SYSTEM_GRAPH_WORKING_SET_PERSIST_CONTEXT_PACK_TOOL_ID: {
        "execution_id": {"filled_by": "agent", "user_visible": False},
        "working_set_id": {"filled_by": "agent", "user_visible": False},
        "scope": {"filled_by": "user_or_agent"},
        "summary": {"filled_by": "agent"},
        "content": {"filled_by": "agent"},
        "created_by_user_id": {"filled_by": "agent", "user_visible": False},
        "workspace_id": {"filled_by": "agent", "user_visible": False},
        "conversation_id": {"filled_by": "agent", "user_visible": False},
        "workflow_id": {"filled_by": "agent", "user_visible": False},
        "importance": {"filled_by": "user_or_agent"},
        "tags": {"filled_by": "agent"},
        "confirmed": {"filled_by": "user_or_agent"},
    },
    SYSTEM_COMMAND_RUN_TOOL_ID: {
        "command": {"filled_by": "agent"},
        "mode": {"filled_by": "user_or_agent"},
        "cwd": {"filled_by": "user_or_agent"},
        "timeout_seconds": {"filled_by": "user_or_agent"},
    },
    SYSTEM_EXECUTION_GET_TOOL_ID: {
        "execution_id": {"filled_by": "agent"},
    },
    SYSTEM_EXECUTION_LIST_TOOL_ID: {
        "workflow_id": {"filled_by": "user_or_agent"},
        "agent_id": {"filled_by": "user_or_agent"},
        "status": {"filled_by": "user_or_agent"},
        "active_only": {"filled_by": "user_or_agent"},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_EXECUTION_EVENTS_TOOL_ID: {
        "execution_id": {"filled_by": "agent"},
        "after_sequence": {"filled_by": "agent"},
        "event_types": {"filled_by": "agent"},
        "agent_id": {"filled_by": "agent", "user_visible": False},
        "task_id": {"filled_by": "agent", "user_visible": False},
        "limit": {"filled_by": "user_or_agent"},
    },
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID: {
        "execution_id": {"filled_by": "agent"},
        "include_content": {"filled_by": "user_or_agent"},
        "max_content_chars": {"filled_by": "user_or_agent"},
    },
    SYSTEM_EXECUTION_PAUSE_TOOL_ID: {
        "execution_id": {"filled_by": "agent"},
    },
    SYSTEM_EXECUTION_RESUME_TOOL_ID: {
        "execution_id": {"filled_by": "agent"},
    },
    SYSTEM_EXECUTION_CANCEL_TOOL_ID: {
        "execution_id": {"filled_by": "agent"},
    },
    SYSTEM_EXECUTION_APPROVALS_TOOL_ID: {
        "execution_id": {"filled_by": "agent"},
    },
    SYSTEM_EXECUTION_APPROVE_TOOL_ID: {
        "execution_id": {"filled_by": "agent"},
        "tool_id": {"filled_by": "agent"},
        "reason": {"filled_by": "agent"},
    },
    SYSTEM_EXECUTION_REJECT_TOOL_ID: {
        "execution_id": {"filled_by": "agent"},
        "tool_id": {"filled_by": "agent"},
        "reason": {"filled_by": "agent"},
    },
}


def _annotate_system_input_schema(
        schema: dict[str, Any],
        contracts: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    if not contracts:
        return schema

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema

    next_properties: dict[str, Any] = {}
    for name, property_schema in properties.items():
        if not isinstance(property_schema, dict):
            next_properties[name] = property_schema
            continue

        next_property_schema = dict(property_schema)
        contract = contracts.get(name) or {}
        filled_by = contract.get("filled_by")
        if isinstance(filled_by, str):
            next_property_schema[SYSTEM_SCHEMA_FILLED_BY_KEY] = filled_by
        if "user_visible" in contract:
            next_property_schema[SYSTEM_SCHEMA_USER_VISIBLE_KEY] = bool(contract["user_visible"])
        next_properties[name] = next_property_schema

    return {
        **schema,
        "properties": next_properties,
    }


def _apply_system_input_contracts(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    annotated: list[ToolDefinition] = []
    for tool in tools:
        contracts = SYSTEM_TOOL_INPUT_CONTRACTS.get(tool.id)
        if not contracts:
            annotated.append(tool)
            continue

        # System tools are authored inline, so we normalize ownership metadata here instead of
        # scattering responsibility rules across frontend drawers or runtime call sites.
        annotated.append(
            tool.model_copy(
                update={
                    "input_schema": _annotate_system_input_schema(tool.input_schema, contracts),
                }
            )
        )
    return annotated


def _materialize_system_tool_specs(
        specs: list[dict[str, Any]],
        *,
        output_schemas: dict[str, dict[str, Any]],
) -> list[ToolDefinition]:
    """Build ToolDefinition objects from declarative system-tool specs.

    Smaller control-plane families can live as plain data in `app.tools.system_specs`,
    which makes tool descriptions and schemas easier to audit without reading large
    blocks of constructor code. Runtime-coupled families can stay inline here until
    they are similarly extracted.
    """

    tools: list[ToolDefinition] = []
    for spec in specs:
        tools.append(
            ToolDefinition(
                id=spec["id"],
                name=spec["name"],
                display_name=spec["display_name"],
                description=spec["description"],
                tool_type=ToolType[spec["tool_type"]],
                input_schema=spec["input_schema"],
                output_schema=output_schemas[spec["output_schema_name"]],
                implementation=ToolImplementationReference.model_validate(spec["implementation"]),
                security=SecuritySettings(**spec["security"]),
                tags=list(spec["tags"]),
            )
        )
    return tools


def workflow_system_tool_ids(*, can_trigger_workflows: bool = True) -> list[str]:
    if not can_trigger_workflows:
        return []
    return _spec_tool_ids(WORKFLOW_SYSTEM_TOOL_SPECS)


def goal_system_tool_ids(*, can_manage_goals: bool = True) -> list[str]:
    if not can_manage_goals:
        return []
    return _spec_tool_ids(GOAL_SYSTEM_TOOL_SPECS)


def tool_management_system_tool_ids(*, can_manage_tools: bool = True) -> list[str]:
    if not can_manage_tools:
        return []
    return _spec_tool_ids(TOOL_MANAGEMENT_SYSTEM_TOOL_SPECS)


def agent_management_system_tool_ids(*, can_manage_agents: bool = True) -> list[str]:
    if not can_manage_agents:
        return []
    return _spec_tool_ids(AGENT_MANAGEMENT_SYSTEM_TOOL_SPECS)


def connector_system_tool_ids(*, can_manage_integrations: bool = True) -> list[str]:
    if not can_manage_integrations:
        return []
    return _spec_tool_ids(CONNECTOR_SYSTEM_TOOL_SPECS)


def memory_system_tool_ids(*, can_manage_memory: bool = True) -> list[str]:
    if not can_manage_memory:
        return []
    return [
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
    ]


def graph_system_tool_ids(*, can_read_graph_context: bool = True) -> list[str]:
    if not can_read_graph_context:
        return []
    return [
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
    ]


def command_system_tool_ids(*, can_run_commands: bool = True) -> list[str]:
    if not can_run_commands:
        return []
    return [SYSTEM_COMMAND_RUN_TOOL_ID]


def execution_system_tool_ids(*, can_inspect_executions: bool = True) -> list[str]:
    if not can_inspect_executions:
        return []
    return _spec_tool_ids(EXECUTION_SYSTEM_TOOL_SPECS)


def workflow_system_tool_definitions(*, can_trigger_workflows: bool = True) -> list[ToolDefinition]:
    if not can_trigger_workflows:
        return []
    workflow_run_output_schema = {
        "type": "object",
        "properties": {
            "execution_id": {"type": "string", "description": "Created execution id."},
            "status": {"type": "string", "description": "Execution status."},
            "output": {"description": "Execution output payload."},
            "error": {"type": ["string", "null"], "description": "Execution error when failed."},
        },
        "required": ["execution_id", "status"],
        "additionalProperties": True,
    }
    return _apply_system_input_contracts(
        _materialize_system_tool_specs(
            WORKFLOW_SYSTEM_TOOL_SPECS,
            output_schemas={
                "ITEMS_OUTPUT_SCHEMA": ITEMS_OUTPUT_SCHEMA,
                "PROPOSAL_OUTPUT_SCHEMA": PROPOSAL_OUTPUT_SCHEMA,
                "RESULT_OUTPUT_SCHEMA": RESULT_OUTPUT_SCHEMA,
                "WORKFLOW_RUN_OUTPUT_SCHEMA": workflow_run_output_schema,
            },
        )
    )


def goal_system_tool_definitions(*, can_manage_goals: bool = True) -> list[ToolDefinition]:
    if not can_manage_goals:
        return []
    return _apply_system_input_contracts(
        _materialize_system_tool_specs(
            GOAL_SYSTEM_TOOL_SPECS,
            output_schemas={
                "ITEMS_OUTPUT_SCHEMA": ITEMS_OUTPUT_SCHEMA,
                "RESULT_OUTPUT_SCHEMA": RESULT_OUTPUT_SCHEMA,
            },
        )
    )


def tool_management_system_tool_definitions(*, can_manage_tools: bool = True) -> list[ToolDefinition]:
    if not can_manage_tools:
        return []
    return _apply_system_input_contracts(
        _materialize_system_tool_specs(
            TOOL_MANAGEMENT_SYSTEM_TOOL_SPECS,
            output_schemas={
                "ITEMS_OUTPUT_SCHEMA": ITEMS_OUTPUT_SCHEMA,
                "PROPOSAL_OUTPUT_SCHEMA": PROPOSAL_OUTPUT_SCHEMA,
                "RESULT_OUTPUT_SCHEMA": RESULT_OUTPUT_SCHEMA,
            },
        )
    )


def agent_management_system_tool_definitions(*, can_manage_agents: bool = True) -> list[ToolDefinition]:
    if not can_manage_agents:
        return []
    return _apply_system_input_contracts(
        _materialize_system_tool_specs(
            AGENT_MANAGEMENT_SYSTEM_TOOL_SPECS,
            output_schemas={
                "ITEMS_OUTPUT_SCHEMA": ITEMS_OUTPUT_SCHEMA,
                "PROPOSAL_OUTPUT_SCHEMA": PROPOSAL_OUTPUT_SCHEMA,
                "RESULT_OUTPUT_SCHEMA": RESULT_OUTPUT_SCHEMA,
            },
        )
    )


def connector_system_tool_definitions(*, can_manage_integrations: bool = True) -> list[ToolDefinition]:
    if not can_manage_integrations:
        return []
    return _apply_system_input_contracts(
        _materialize_system_tool_specs(
            CONNECTOR_SYSTEM_TOOL_SPECS,
            output_schemas={
                "ITEMS_OUTPUT_SCHEMA": ITEMS_OUTPUT_SCHEMA,
                "RESULT_OUTPUT_SCHEMA": RESULT_OUTPUT_SCHEMA,
            },
        )
    )


def memory_system_tool_definitions(*, can_manage_memory: bool = True) -> list[ToolDefinition]:
    if not can_manage_memory:
        return []
    return _apply_system_input_contracts(
        memory_runtime_tool_definitions(
            items_output_schema=ITEMS_OUTPUT_SCHEMA,
            result_output_schema=RESULT_OUTPUT_SCHEMA,
        )
    )


def graph_system_tool_definitions(*, can_read_graph_context: bool = True) -> list[ToolDefinition]:
    if not can_read_graph_context:
        return []
    return _apply_system_input_contracts(
        graph_runtime_tool_definitions(
            result_output_schema=RESULT_OUTPUT_SCHEMA,
            graph_context_output_schema=GRAPH_CONTEXT_OUTPUT_SCHEMA,
            graph_document_output_schema=GRAPH_DOCUMENT_OUTPUT_SCHEMA,
            graph_neighbors_output_schema=GRAPH_NEIGHBORS_OUTPUT_SCHEMA,
        )
    )


def command_system_tool_definitions(*, can_run_commands: bool = True) -> list[ToolDefinition]:
    if not can_run_commands:
        return []
    return _apply_system_input_contracts(
        _materialize_system_tool_specs(
            COMMAND_SYSTEM_TOOL_SPECS,
            output_schemas={"COMMAND_OUTPUT_SCHEMA": COMMAND_OUTPUT_SCHEMA},
        )
    )


def execution_system_tool_definitions(*, can_inspect_executions: bool = True) -> list[ToolDefinition]:
    if not can_inspect_executions:
        return []
    return _apply_system_input_contracts(
        _materialize_system_tool_specs(
            EXECUTION_SYSTEM_TOOL_SPECS,
            output_schemas={
                "ITEMS_OUTPUT_SCHEMA": ITEMS_OUTPUT_SCHEMA,
                "RESULT_OUTPUT_SCHEMA": RESULT_OUTPUT_SCHEMA,
            },
        )
    )


def is_system_workflow_tool(tool: ToolDefinition) -> bool:
    return (
            tool.implementation.target == SYSTEM_WORKFLOW_TOOL_TARGET
            or tool.id
            in {
                SYSTEM_WORKFLOW_LIST_TOOL_ID,
                SYSTEM_WORKFLOW_GET_TOOL_ID,
                SYSTEM_WORKFLOW_RUN_TOOL_ID,
                SYSTEM_WORKFLOW_PROPOSE_CREATE_TOOL_ID,
                SYSTEM_WORKFLOW_PROPOSE_UPDATE_TOOL_ID,
            }
    )


def is_system_goal_tool(tool: ToolDefinition) -> bool:
    return (
            tool.implementation.target == SYSTEM_GOAL_TOOL_TARGET
            or tool.id
            in {
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
            }
    )


def is_system_tool_management_tool(tool: ToolDefinition) -> bool:
    return (
            tool.implementation.target == SYSTEM_TOOL_MANAGEMENT_TARGET
            or tool.id
            in {
                SYSTEM_TOOL_LIST_TOOL_ID,
                SYSTEM_TOOL_GET_TOOL_ID,
                SYSTEM_TOOL_PROPOSE_CREATE_TOOL_ID,
                SYSTEM_TOOL_PROPOSE_UPDATE_TOOL_ID,
                SYSTEM_TOOL_WORKSPACE_LIST_TOOL_ID,
                SYSTEM_TOOL_WORKSPACE_SCAFFOLD_TOOL_ID,
                SYSTEM_TOOL_WORKSPACE_PUBLISH_TOOL_ID,
            }
    )


def is_system_agent_management_tool(tool: ToolDefinition) -> bool:
    return (
            tool.implementation.target == SYSTEM_AGENT_MANAGEMENT_TARGET
            or tool.id
            in {
                SYSTEM_AGENT_LIST_TOOL_ID,
                SYSTEM_AGENT_GET_TOOL_ID,
                SYSTEM_AGENT_PROPOSE_UPDATE_TOOL_ID,
            }
    )


def is_system_connector_tool(tool: ToolDefinition) -> bool:
    return (
            tool.implementation.target == SYSTEM_CONNECTOR_TOOL_TARGET
            or tool.id
            in {
                SYSTEM_CONNECTOR_CAPABILITIES_TOOL_ID,
                SYSTEM_CONNECTOR_CREDENTIALS_TOOL_ID,
                SYSTEM_CONNECTOR_RESOLVE_TOOL_ID,
                SYSTEM_CONNECTOR_HISTORY_TOOL_ID,
                SYSTEM_CONNECTOR_TEST_TOOL_ID,
            }
    )


def is_system_memory_tool(tool: ToolDefinition) -> bool:
    return (
            tool.implementation.target == SYSTEM_MEMORY_TOOL_TARGET
            or tool.id
            in {
                SYSTEM_MEMORY_LIST_TOOL_ID,
                SYSTEM_MEMORY_REMEMBER_TOOL_ID,
                SYSTEM_MEMORY_UPDATE_TOOL_ID,
                SYSTEM_MEMORY_DELETE_TOOL_ID,
            }
    )


def is_system_graph_tool(tool: ToolDefinition) -> bool:
    return tool.implementation.target == SYSTEM_GRAPH_TOOL_TARGET or tool.id == SYSTEM_GRAPH_CONTEXT_TOOL_ID


def is_system_command_tool(tool: ToolDefinition) -> bool:
    return tool.implementation.target == SYSTEM_COMMAND_TOOL_TARGET or tool.id == SYSTEM_COMMAND_RUN_TOOL_ID


def is_system_execution_tool(tool: ToolDefinition) -> bool:
    return (
            tool.implementation.target == SYSTEM_EXECUTION_TOOL_TARGET
            or tool.id
            in {
                SYSTEM_EXECUTION_GET_TOOL_ID,
                SYSTEM_EXECUTION_LIST_TOOL_ID,
                SYSTEM_EXECUTION_EVENTS_TOOL_ID,
                SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
                SYSTEM_EXECUTION_PAUSE_TOOL_ID,
                SYSTEM_EXECUTION_RESUME_TOOL_ID,
                SYSTEM_EXECUTION_CANCEL_TOOL_ID,
                SYSTEM_EXECUTION_APPROVALS_TOOL_ID,
                SYSTEM_EXECUTION_APPROVE_TOOL_ID,
                SYSTEM_EXECUTION_REJECT_TOOL_ID,
            }
    )


@dataclass(slots=True)
class AgentToolResolver:
    context: ApiContext

    async def ensure_default_main_agent_speech_tools(self) -> list[ToolDefinition]:
        specs = get_tool_catalog_specs()
        tools = [
            specs[tool_id].tool_definition
            for tool_id in DEFAULT_MAIN_AGENT_SPEECH_TOOL_IDS
            if tool_id in specs
        ]
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_workflow_system_tools(self, *, can_trigger_workflows: bool = True) -> list[ToolDefinition]:
        tools = workflow_system_tool_definitions(can_trigger_workflows=can_trigger_workflows)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_goal_system_tools(self, *, can_manage_goals: bool = True) -> list[ToolDefinition]:
        tools = goal_system_tool_definitions(can_manage_goals=can_manage_goals)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_tool_management_system_tools(self, *, can_manage_tools: bool = True) -> list[ToolDefinition]:
        tools = tool_management_system_tool_definitions(can_manage_tools=can_manage_tools)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_agent_management_system_tools(self, *, can_manage_agents: bool = True) -> list[ToolDefinition]:
        tools = agent_management_system_tool_definitions(can_manage_agents=can_manage_agents)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_connector_system_tools(self, *, can_manage_integrations: bool = True) -> list[ToolDefinition]:
        tools = connector_system_tool_definitions(can_manage_integrations=can_manage_integrations)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_memory_system_tools(self, *, can_manage_memory: bool = True) -> list[ToolDefinition]:
        tools = memory_system_tool_definitions(can_manage_memory=can_manage_memory)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_graph_system_tools(self, *, can_read_graph_context: bool = True) -> list[ToolDefinition]:
        tools = graph_system_tool_definitions(can_read_graph_context=can_read_graph_context)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_command_system_tools(self, *, can_run_commands: bool = True) -> list[ToolDefinition]:
        tools = command_system_tool_definitions(can_run_commands=can_run_commands)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_optional_module_system_tools(
            self,
            module_key: str,
            *,
            enabled: bool = True,
            **kwargs: Any,
    ) -> list[ToolDefinition]:
        tools = optional_module_system_tool_definitions(module_key, enabled=enabled, **kwargs)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def ensure_execution_system_tools(self, *, can_inspect_executions: bool = True) -> list[ToolDefinition]:
        tools = execution_system_tool_definitions(can_inspect_executions=can_inspect_executions)
        for tool in tools:
            await self.context.tool_repo.save(tool)
        return tools

    async def resolve_agent_tools(self, agent: AgentDefinition | None) -> list[ToolDefinition]:
        if agent is None or not agent.tool_ids:
            return []
        tools: list[ToolDefinition] = []
        seen: set[str] = set()
        for tool_id in agent.tool_ids:
            if tool_id in seen:
                continue
            seen.add(tool_id)
            tool = await self.context.tool_repo.get(tool_id)
            if tool is not None:
                tools.append(tool)
        return tools

    def optional_module_tool_profile_ids(self, module_key: str, profile: str) -> list[str]:
        return optional_module_tool_profile_ids(module_key, profile)

    def main_agent_default_tool_ids(self, policy: dict[str, Any]) -> list[str]:
        # Import lazily to avoid turning the shared system-tool catalog into a hard import cycle
        # for modules that only need low-level ToolDefinition factories.
        from app.tools.system_catalog import builtin_system_tool_ids_from_catalog

        normalized_policy = {
            "can_trigger_workflows": bool(policy.get("can_trigger_workflows", True)),
            "can_manage_goals": bool(policy.get("can_manage_goals", True)),
            "can_manage_tools": bool(policy.get("can_manage_tools", True)),
            "can_manage_agents": bool(policy.get("can_manage_agents", True)),
            "can_manage_integrations": bool(policy.get("can_manage_integrations", True)),
            "can_manage_memory": bool(policy.get("can_manage_memory", True)),
            "can_read_graph_context": bool(policy.get("can_read_graph_context", False)),
            "can_inspect_executions": bool(policy.get("can_inspect_executions", True)),
            "can_run_commands": bool(policy.get("can_run_commands", True)),
        }
        for key, value in policy.items():
            if key not in normalized_policy and isinstance(value, bool):
                normalized_policy[key] = value
        tool_ids = builtin_system_tool_ids_from_catalog(
            include_connectors=True,
            policy=normalized_policy,
        )
        return list(dict.fromkeys([*tool_ids, *DEFAULT_MAIN_AGENT_SPEECH_TOOL_IDS]))
