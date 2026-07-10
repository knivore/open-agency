"""Runtime graph-context auto-retrieval for native executions."""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.domain import (
    AgentDefinition,
    ContextCompactionRecord,
    Execution,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionEventType,
    GoalDefinition,
    MemoryRecord,
    TaskDefinition,
    ToolDefinition,
    WorkflowDefinition,
)
from app.runtime.native.state import NativeExecutionState

FAILURE_EVENT_TYPES = {
    ExecutionEventType.EXECUTION_FAILED,
    ExecutionEventType.AGENT_STEP_FAILED,
    ExecutionEventType.SUBAGENT_STEP_FAILED,
    ExecutionEventType.TOOL_CALL_FAILED,
    ExecutionEventType.CONTAINER_FAILED,
    ExecutionEventType.CONTEXT_COMPACTION_FAILED,
    ExecutionEventType.RUNTIME_BUILD_FAILED,
    ExecutionEventType.OUTBOUND_WEBHOOK_FAILED,
}
TOOL_CALL_EVENT_TYPES = {
    ExecutionEventType.TOOL_CALL_STARTED,
    ExecutionEventType.TOOL_CALL_COMPLETED,
    ExecutionEventType.TOOL_CALL_FAILED,
}
MODEL_REQUEST_EVENT_TYPES = {
    ExecutionEventType.LLM_REQUEST_CREATED,
    ExecutionEventType.LLM_RESPONSE_CREATED,
}
PROPOSAL_TOOL_IDS = {
    "agency.workflow.propose-create",
    "agency.workflow.propose-update",
    "agency.tool.propose-create",
    "agency.tool.propose-update",
    "agency.agent.propose-update",
}


class RuntimeGraphContextAutoRetriever:
    """Build Agency Graph context at runtime trigger points."""

    def __init__(self, context: Any):
        self.context = context

    async def retrieve_for_goal_supervision(
            self,
            goal_id: str,
            *,
            budget: str = "balanced",
    ) -> dict[str, Any]:
        normalized_goal_id = str(goal_id or "").strip()
        if not normalized_goal_id:
            raise ValueError("goal_id is required")
        goal_repo = getattr(self.context, "goal_repo", None)
        getter = getattr(goal_repo, "get", None)
        if getter is None:
            return _missing_goal_supervision_context(normalized_goal_id, "goal_repo_unavailable", budget=budget)
        goal = await getter(normalized_goal_id)
        if goal is None:
            return _missing_goal_supervision_context(normalized_goal_id, "goal_not_found", budget=budget)

        executions = await _goal_supervision_executions(self.context, goal)
        execution_contexts: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        recent_events: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        for execution in executions:
            events = await _list_execution_events(self.context, execution.id)
            execution_artifacts = await _list_execution_artifacts(self.context, execution.id)
            execution_contexts.append(_goal_supervision_execution_context(execution, events, execution_artifacts))
            failures.extend(_goal_supervision_failures(execution, events))
            recent_events.extend(_event_context(event) for event in events[-6:])
            artifacts.extend(_artifact_context(artifact) for artifact in execution_artifacts[-6:])

        memories = await _goal_supervision_memories(self.context, goal)
        projection_events = await _goal_supervision_projection_events(self.context, goal)
        relationships = _goal_supervision_relationships(goal, executions, memories, projection_events)
        decisions = _goal_supervision_decisions(goal)
        next_actions = _goal_supervision_next_actions(goal)
        facts = [
            {
                "kind": "goal",
                "goal_id": goal.id,
                "objective": goal.objective,
                "status": goal.status.value,
                "priority": goal.priority,
                "success_criteria_count": len(goal.success_criteria),
                "evidence_count": len(goal.evidence),
                "execution_count": len(execution_contexts),
            },
            {
                "kind": "goal_evaluation",
                "goal_id": goal.id,
                "evaluation": _compact_value(goal.evaluation or {}),
            },
        ]
        # Goal supervision must remain useful before the graph projection worker catches up, so this pack
        # combines graph outbox lineage with the current durable goal, execution, artifact, and memory records.
        return {
            "status": "ok",
            "summary": (
                f"Goal '{goal.objective}' is {goal.status.value} with "
                f"{len(execution_contexts)} execution(s), {len(failures)} failure signal(s), "
                f"{len(decisions)} supervisor decision(s), and {len(next_actions)} next action(s)."
            ),
            "query_meta": {
                "intent": "supervise_goal",
                "budget": budget if budget in {"brief", "balanced", "full"} else "balanced",
                "anchor_type": "goal",
                "anchor_id": goal.id,
                "projection_event_count": len(projection_events),
                "runtime_events_fallback_used": True,
            },
            "goal": _goal_supervision_goal_context(goal),
            "facts": facts,
            "relationships": relationships,
            "prior_attempts": execution_contexts,
            "failures": failures[-10:],
            "decisions": decisions,
            "constraints": _goal_supervision_constraints(goal),
            "next_actions": next_actions,
            "related_memories": [_memory_context(memory) for memory in memories],
            "recent_events": recent_events[-12:],
            "artifacts": artifacts[-12:],
            "projection_events": [_projection_event_context(event) for event in projection_events],
        }

    async def retrieve_before_subagent_start(
            self,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            execution_input: dict[str, Any],
            state: NativeExecutionState,
    ) -> dict[str, Any] | None:
        if await _coding_agent_start_retrieval_enabled(self.context, agent):
            return await self._retrieve_before_coding_agent_start(
                workflow=workflow,
                task=task,
                agent=agent,
                execution=execution,
                execution_input=execution_input,
                state=state,
            )
        if not _subagent_start_retrieval_enabled(agent):
            return None
        graph_settings = agent.graph_context
        intent = graph_settings.default_intent if graph_settings.default_intent in {"handoff", "plan"} else "handoff"
        skipped = _graph_context_loop_guard_entry(
            state,
            trigger="subagent_start",
            reason="prepare_assigned_agent_context",
            intent=intent,
            budget="brief",
            anchor_type="task",
            anchor_id=task.id,
            workflow_id=workflow.id,
            execution_id=execution.id,
            task_id=task.id,
            agent_id=agent.id,
        )
        if skipped is not None:
            return skipped
        request = {
            "intent": intent,
            "anchor_type": "task",
            "anchor_id": task.id,
            "scope": {
                "workflow_id": workflow.id,
                "workflow_name": workflow.name,
                "execution_id": execution.id,
                "run_id": execution.id,
                "task_id": task.id,
                "task_name": task.name,
                "agent_id": agent.id,
                "agent_name": agent.name,
                "node_id": state.current_node_id,
                "trigger": "subagent_start",
                "input_keys": sorted(execution_input.keys()),
            },
            "include_memories": graph_settings.include_memories,
            "include_events": graph_settings.include_events,
            "include_raw_graph": False,
            "budget": "brief",
            "limit": _brief_limit(graph_settings.max_records),
        }
        from app.services.agency_graph_context import AgencyGraphContextService

        context = await AgencyGraphContextService(self.context).build_context(request)
        return _record_auto_retrieval_injection(self.context, _with_loop_guard_metadata(state, {
            "trigger": "subagent_start",
            "reason": "prepare_assigned_agent_context",
            "intent": intent,
            "budget": "brief",
            "anchor_type": "task",
            "anchor_id": task.id,
            "workflow_id": workflow.id,
            "execution_id": execution.id,
            "task_id": task.id,
            "agent_id": agent.id,
            "context": context,
        }))

    async def _retrieve_before_coding_agent_start(
            self,
            *,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            execution_input: dict[str, Any],
            state: NativeExecutionState,
    ) -> dict[str, Any] | None:
        graph_settings = agent.graph_context
        budget = graph_settings.default_budget if graph_settings.default_budget in {"brief", "balanced",
                                                                                    "full"} else "balanced"
        skipped = _graph_context_loop_guard_entry(
            state,
            trigger="coding_agent_start",
            reason="resume_coding_agent_context",
            intent="resume",
            budget=budget,
            anchor_type="task",
            anchor_id=task.id,
            workflow_id=workflow.id,
            execution_id=execution.id,
            task_id=task.id,
            agent_id=agent.id,
        )
        if skipped is not None:
            return skipped
        scope = _coding_resume_scope(
            workflow=workflow,
            task=task,
            agent=agent,
            execution=execution,
            execution_input=execution_input,
            state=state,
        )
        request = {
            "intent": "resume",
            "anchor_type": "task",
            "anchor_id": task.id,
            "scope": scope,
            "include_memories": graph_settings.include_memories,
            "include_events": True,
            "include_raw_graph": False,
            "budget": budget,
            "limit": _balanced_limit(graph_settings.max_records),
        }
        from app.services.agency_graph_context import AgencyGraphContextService

        context = await AgencyGraphContextService(self.context).build_context(request)
        return _record_auto_retrieval_injection(self.context, _with_loop_guard_metadata(state, {
            "trigger": "coding_agent_start",
            "reason": "resume_coding_agent_context",
            "intent": "resume",
            "budget": budget,
            "anchor_type": "task",
            "anchor_id": task.id,
            "workflow_id": workflow.id,
            "execution_id": execution.id,
            "task_id": task.id,
            "agent_id": agent.id,
            "workspace_id": scope.get("workspace_id"),
            "workspace": scope.get("workspace"),
            "workspace_path": scope.get("workspace_path"),
            "repository": scope.get("repository"),
            "repo_path": scope.get("repo_path"),
            "conversation_id": scope.get("conversation_id"),
            "prior_changes": _context_section(context, "prior_changes"),
            "prior_attempts": _context_section(context, "prior_attempts"),
            "failures": _context_section(context, "failures"),
            "decisions": _context_section(context, "decisions"),
            "run_summaries": _run_summaries_from_context(context),
            "constraints": _context_section(context, "constraints"),
            "next_actions": _context_section(context, "next_actions"),
            "context": context,
        }))

    async def retrieve_after_execution_failed(
            self,
            workflow: WorkflowDefinition,
            execution: Execution,
            state: NativeExecutionState,
            error: str,
            failure_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not _execution_failure_retrieval_enabled():
            return None
        skipped = _graph_context_loop_guard_entry(
            state,
            trigger="execution_failed",
            reason="root_cause_context_after_execution_failure",
            intent="root_cause",
            budget="balanced",
            anchor_type="run",
            anchor_id=execution.id,
            workflow_id=workflow.id,
            execution_id=execution.id,
        )
        if skipped is not None:
            return skipped
        events = await _list_execution_events(self.context, execution.id)
        artifacts = await _list_execution_artifacts(self.context, execution.id)
        failed_events = [_event_context(event) for event in _latest_events(events, FAILURE_EVENT_TYPES, limit=8)]
        tool_calls = [_event_context(event) for event in _latest_events(events, TOOL_CALL_EVENT_TYPES, limit=8)]
        model_requests = [_event_context(event) for event in _latest_events(events, MODEL_REQUEST_EVENT_TYPES, limit=6)]
        artifact_context = [_artifact_context(artifact) for artifact in artifacts[-8:]]
        request = {
            "intent": "root_cause",
            "anchor_type": "run",
            "anchor_id": execution.id,
            "scope": {
                "workflow_id": workflow.id,
                "workflow_name": workflow.name,
                "execution_id": execution.id,
                "run_id": execution.id,
                "trigger": "execution_failed",
                "failure_event_id": failure_event_id,
                "error": error,
                "failed_events": failed_events,
                "tool_calls": tool_calls,
                "artifacts": artifact_context,
                "model_requests": model_requests,
            },
            "include_memories": True,
            "include_events": True,
            "include_raw_graph": False,
            "budget": "balanced",
            "limit": 20,
        }
        from app.services.agency_graph_context import AgencyGraphContextService

        context = await AgencyGraphContextService(self.context).build_context(request)
        prior_attempts = context.get("prior_attempts") if isinstance(context.get("prior_attempts"), list) else []
        return _record_auto_retrieval_injection(self.context, _with_loop_guard_metadata(state, {
            "trigger": "execution_failed",
            "reason": "root_cause_context_after_execution_failure",
            "intent": "root_cause",
            "budget": "balanced",
            "anchor_type": "run",
            "anchor_id": execution.id,
            "workflow_id": workflow.id,
            "execution_id": execution.id,
            "failure_event_id": failure_event_id,
            "error": error,
            "failed_events": failed_events,
            "tool_calls": tool_calls,
            "artifacts": artifact_context,
            "model_requests": model_requests,
            "prior_attempts": prior_attempts,
            "context": context,
        }))

    async def retrieve_after_context_compaction(
            self,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            state: NativeExecutionState,
            record: ContextCompactionRecord,
    ) -> dict[str, Any] | None:
        if not _context_compaction_retrieval_enabled(agent) or not record.compacted:
            return None
        skipped = _graph_context_loop_guard_entry(
            state,
            trigger="context_compaction",
            reason="handoff_context_after_compaction",
            intent="handoff",
            budget="brief",
            anchor_type="run",
            anchor_id=execution.id,
            workflow_id=workflow.id,
            execution_id=execution.id,
            task_id=task.id,
            agent_id=agent.id,
        )
        if skipped is not None:
            return skipped
        scope = _context_compaction_scope(
            workflow=workflow,
            task=task,
            agent=agent,
            execution=execution,
            state=state,
            record=record,
        )
        request = {
            "intent": "handoff",
            "anchor_type": "run",
            "anchor_id": execution.id,
            "scope": scope,
            "include_memories": agent.graph_context.include_memories,
            "include_events": True,
            "include_raw_graph": False,
            "budget": "brief",
            "limit": _brief_limit(agent.graph_context.max_records),
        }
        from app.services.agency_graph_context import AgencyGraphContextService

        context = await AgencyGraphContextService(self.context).build_context(request)
        graph_context_metadata = _graph_context_pack_metadata(
            context,
            trigger="context_compaction",
            context_pack_id=record.memory_id,
        )
        metadata_attached = await _attach_graph_context_metadata_to_memory(
            self.context,
            record.memory_id,
            graph_context_metadata,
        )
        return _record_auto_retrieval_injection(self.context, _with_loop_guard_metadata(state, {
            "trigger": "context_compaction",
            "reason": "handoff_context_after_compaction",
            "intent": "handoff",
            "budget": "brief",
            "anchor_type": "run",
            "anchor_id": execution.id,
            "workflow_id": workflow.id,
            "execution_id": execution.id,
            "task_id": task.id,
            "agent_id": agent.id,
            "context_pack_id": record.memory_id,
            "graph_context_metadata": graph_context_metadata,
            "graph_context_metadata_attached": metadata_attached,
            "context": context,
        }))

    async def retrieve_before_proposal_tool(
            self,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            state: NativeExecutionState,
            tool: ToolDefinition,
            arguments: dict[str, Any],
            tool_call_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not _proposal_tool_retrieval_enabled(agent, tool):
            return None
        anchor_type, anchor_id = _proposal_tool_anchor(workflow, task, agent, tool, arguments)
        intent = _proposal_tool_intent(tool)
        budget = agent.graph_context.default_budget if agent.graph_context.default_budget in {"brief", "balanced",
                                                                                              "full"} else "balanced"
        skipped = _graph_context_loop_guard_entry(
            state,
            trigger="proposal_tool",
            reason="prepare_mutation_proposal_context",
            intent=intent,
            budget=budget,
            anchor_type=anchor_type,
            anchor_id=anchor_id,
            workflow_id=workflow.id,
            execution_id=execution.id,
            task_id=task.id,
            agent_id=agent.id,
            extra_key=tool.id,
        )
        if skipped is not None:
            skipped["proposal_tool_id"] = tool.id
            skipped["proposal_tool_name"] = tool.name
            skipped["tool_call_id"] = tool_call_id
            return skipped
        scope = _proposal_tool_scope(
            workflow=workflow,
            task=task,
            agent=agent,
            execution=execution,
            state=state,
            tool=tool,
            arguments=arguments,
            tool_call_id=tool_call_id,
        )
        request = {
            "intent": intent,
            "anchor_type": anchor_type,
            "anchor_id": anchor_id,
            "scope": scope,
            "include_memories": agent.graph_context.include_memories,
            "include_events": agent.graph_context.include_events,
            "include_raw_graph": False,
            "budget": budget,
            "limit": _balanced_limit(agent.graph_context.max_records),
        }
        from app.services.agency_graph_context import AgencyGraphContextService

        context = await AgencyGraphContextService(self.context).build_context(request)
        return _record_auto_retrieval_injection(self.context, _with_loop_guard_metadata(state, {
            "trigger": "proposal_tool",
            "reason": "prepare_mutation_proposal_context",
            "intent": intent,
            "budget": budget,
            "anchor_type": anchor_type,
            "anchor_id": anchor_id,
            "workflow_id": workflow.id,
            "execution_id": execution.id,
            "task_id": task.id,
            "agent_id": agent.id,
            "tool_call_id": tool_call_id,
            "proposal_tool_id": tool.id,
            "proposal_tool_name": tool.name,
            "proposal_target_type": scope.get("proposal_target_type"),
            "proposal_target_id": scope.get("proposal_target_id"),
            "context": context,
        }))


def _subagent_start_retrieval_enabled(agent: AgentDefinition) -> bool:
    settings = get_settings()
    graph_settings = agent.graph_context
    return bool(
        settings.agency_graph_context_tools_enabled
        and settings.graph_context_auto_retrieval_enabled
        and settings.graph_context_subagent_steering_enabled
        and graph_settings.enabled
        and graph_settings.auto_retrieval_enabled is not False
        and graph_settings.subagent_steering_enabled is not False
    )


def _execution_failure_retrieval_enabled() -> bool:
    settings = get_settings()
    return bool(settings.agency_graph_context_tools_enabled and settings.graph_context_auto_retrieval_enabled)


def _record_auto_retrieval_injection(context: Any, entry: dict[str, Any]) -> dict[str, Any]:
    operations = getattr(context, "runtime_operations", None)
    if operations is None:
        return entry
    trigger = str(entry.get("trigger") or "unknown")
    operations.increment("graph_context.auto_retrieval.injections")
    operations.increment(f"graph_context.auto_retrieval.injections.{_metric_key(trigger)}")
    operations.record_action(
        "graph_context.auto_retrieval_injected",
        trigger=trigger,
        reason=entry.get("reason"),
        intent=entry.get("intent"),
        budget=entry.get("budget"),
        anchor_type=entry.get("anchor_type"),
        anchor_id=entry.get("anchor_id"),
        workflow_id=entry.get("workflow_id"),
        execution_id=entry.get("execution_id"),
        task_id=entry.get("task_id"),
        agent_id=entry.get("agent_id"),
        status=(entry.get("context") or {}).get("status") if isinstance(entry.get("context"), dict) else None,
    )
    return entry


def _metric_key(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_") or "unknown"


def _context_compaction_retrieval_enabled(agent: AgentDefinition) -> bool:
    settings = get_settings()
    graph_settings = agent.graph_context
    return bool(
        settings.agency_graph_context_tools_enabled
        and settings.graph_context_auto_retrieval_enabled
        and graph_settings.enabled
        and graph_settings.auto_retrieval_enabled is not False
    )


def _proposal_tool_retrieval_enabled(agent: AgentDefinition, tool: ToolDefinition) -> bool:
    return bool(
        tool.id in PROPOSAL_TOOL_IDS
        and _context_compaction_retrieval_enabled(agent)
    )


def _graph_context_loop_guard_entry(
        state: NativeExecutionState,
        *,
        trigger: str,
        reason: str,
        intent: str,
        budget: str,
        anchor_type: str,
        anchor_id: str,
        workflow_id: str,
        execution_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        extra_key: str | None = None,
) -> dict[str, Any] | None:
    if not get_settings().graph_context_loop_guard_enabled:
        return None
    current_progress = _graph_context_progress_signature(state)
    guard_key = _graph_context_guard_key(
        trigger=trigger,
        intent=intent,
        anchor_type=anchor_type,
        anchor_id=anchor_id,
        task_id=task_id,
        agent_id=agent_id,
        extra_key=extra_key,
    )
    for entry in reversed(state.graph_context_entries):
        if entry.get("skipped"):
            continue
        metadata = entry.get("loop_guard") if isinstance(entry.get("loop_guard"), dict) else {}
        if metadata.get("guard_key") != guard_key:
            continue
        if metadata.get("progress_signature") != current_progress:
            return None
        return {
            "trigger": trigger,
            "reason": "auto_retrieval_loop_guard_no_progress",
            "skip_reason": "no_runtime_progress_since_last_graph_context",
            "skipped": True,
            "intent": intent,
            "budget": budget,
            "anchor_type": anchor_type,
            "anchor_id": anchor_id,
            "workflow_id": workflow_id,
            "execution_id": execution_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "loop_guard": {
                "guard_key": guard_key,
                "progress_signature": current_progress,
                "matched_entry_trigger": entry.get("trigger"),
                "matched_entry_reason": entry.get("reason"),
            },
            "context": {
                "status": "skipped",
                "summary": (
                    "Graph context auto-retrieval skipped because the same graph context "
                    "was already retrieved and no task progress has been recorded since."
                ),
                "query_meta": {
                    "intent": intent,
                    "budget": budget,
                    "anchor_type": anchor_type,
                    "anchor_id": anchor_id,
                    "scope": {
                        "workflow_id": workflow_id,
                        "execution_id": execution_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "trigger": trigger,
                    },
                    "node_count": 0,
                    "edge_count": 0,
                },
            },
        }
    return None


def _with_loop_guard_metadata(state: NativeExecutionState, entry: dict[str, Any]) -> dict[str, Any]:
    entry["loop_guard"] = {
        "guard_key": _graph_context_guard_key(
            trigger=str(entry.get("trigger") or ""),
            intent=str(entry.get("intent") or ""),
            anchor_type=str(entry.get("anchor_type") or ""),
            anchor_id=str(entry.get("anchor_id") or ""),
            task_id=entry.get("task_id") if isinstance(entry.get("task_id"), str) else None,
            agent_id=entry.get("agent_id") if isinstance(entry.get("agent_id"), str) else None,
            extra_key=entry.get("proposal_tool_id") if isinstance(entry.get("proposal_tool_id"), str) else None,
        ),
        "progress_signature": _graph_context_progress_signature(state),
        "recorded_at_sequence": state.sequence,
    }
    return entry


def _graph_context_guard_key(
        *,
        trigger: str,
        intent: str,
        anchor_type: str,
        anchor_id: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        extra_key: str | None = None,
) -> str:
    parts = [
        trigger,
        intent,
        anchor_type,
        anchor_id,
        task_id or "",
        agent_id or "",
        extra_key or "",
    ]
    return "|".join(parts)


def _graph_context_progress_signature(state: NativeExecutionState) -> dict[str, int]:
    return {
        "node_output_count": len(state.node_outputs),
        "memory_entry_count": len(state.memory_entries),
        "context_compaction_count": int(state.context_compaction.get("count") or 0),
        "compacted_context_pack_count": len(state.compacted_context_packs),
    }


async def _coding_agent_start_retrieval_enabled(context: Any, agent: AgentDefinition) -> bool:
    settings = get_settings()
    graph_settings = agent.graph_context
    if not (
            settings.agency_graph_context_tools_enabled
            and settings.graph_context_auto_retrieval_enabled
            and settings.graph_context_coding_agent_resume_enabled
            and graph_settings.enabled
            and graph_settings.auto_retrieval_enabled is not False
            and graph_settings.coding_agent_resume_enabled is not False
    ):
        return False
    if graph_settings.coding_agent_resume_enabled is True:
        return True
    if _agent_looks_like_coding_agent(agent):
        return True
    profile = await _model_profile_for_agent(context, agent)
    if profile is None:
        return False
    provider = str(getattr(profile, "provider", "") or "").lower()
    model = str(getattr(profile, "model", "") or "").lower()
    return "codex" in provider or "codex" in model


def _brief_limit(configured_limit: int | None) -> int:
    if configured_limit is None:
        return 12
    return max(min(int(configured_limit), 20), 1)


def _balanced_limit(configured_limit: int | None) -> int:
    if configured_limit is None:
        return 20
    return max(min(int(configured_limit), 50), 1)


async def _model_profile_for_agent(context: Any, agent: AgentDefinition) -> Any | None:
    if not agent.model_profile_id:
        return None
    repo = getattr(context, "model_profile_repo", None)
    if repo is None:
        return None
    getter = getattr(repo, "get", None) or getattr(repo, "get_profile", None)
    if getter is None:
        return None
    return await getter(agent.model_profile_id)


def _agent_looks_like_coding_agent(agent: AgentDefinition) -> bool:
    metadata = agent.metadata if isinstance(agent.metadata, dict) else {}
    declared_type = _first_string(
        metadata.get("agent_type"),
        metadata.get("type"),
        metadata.get("kind"),
        metadata.get("runtime_role"),
        metadata.get("specialization"),
    )
    if declared_type and _contains_coding_marker(declared_type):
        return True
    haystack = " ".join(
        value
        for value in (
            agent.name,
            agent.display_name,
            agent.role,
            agent.description,
            agent.instructions,
            agent.model_profile_id,
        )
        if isinstance(value, str)
    )
    has_command_tool = "agency.command.run" in set(agent.tool_ids)
    return _contains_coding_marker(haystack) and (has_command_tool or "codex" in haystack.lower())


def _contains_coding_marker(value: str) -> bool:
    normalized = value.lower().replace("-", "_").replace(" ", "_")
    return any(
        marker in normalized
        for marker in (
            "coding",
            "coder",
            "codex",
            "software_engineer",
            "software_developer",
            "code_agent",
        )
    )


def _coding_resume_scope(
        *,
        workflow: WorkflowDefinition,
        task: TaskDefinition,
        agent: AgentDefinition,
        execution: Execution,
        execution_input: dict[str, Any],
        state: NativeExecutionState,
) -> dict[str, Any]:
    mappings = [
        execution_input,
        task.metadata,
        agent.metadata,
        workflow.metadata,
        execution.metadata,
        execution.trigger_payload,
    ]
    workspace_id = _first_mapping_string(mappings, "workspace_id", "workspaceId")
    workspace = _first_mapping_string(mappings, "workspace", "workspace_name", "workspaceName", "target_workspace")
    workspace_path = _first_mapping_string(mappings, "workspace_path", "workspacePath", "cwd", "working_directory")
    repository = _first_mapping_string(mappings, "repository", "repo", "repo_name", "repository_name")
    repo_path = _first_mapping_string(mappings, "repo_path", "repository_path", "repo", "repository")
    conversation_id = _first_mapping_string(mappings, "conversation_id", "conversationId", "thread_id", "threadId")
    current_user_id = _first_mapping_string(
        mappings,
        "current_user_id",
        "actor_user_id",
        "user_id",
        "created_by_user_id",
        "created_by",
    )
    return {
        "workflow_id": workflow.id,
        "workflow_name": workflow.name,
        "execution_id": execution.id,
        "run_id": execution.id,
        "task_id": task.id,
        "task_name": task.name,
        "task_description": task.description,
        "task_instructions": task.instructions,
        "expected_output": task.expected_output,
        "agent_id": agent.id,
        "agent_name": agent.name,
        "node_id": state.current_node_id,
        "trigger": "coding_agent_start",
        "workspace_id": workspace_id,
        "workspace": workspace,
        "workspace_path": workspace_path,
        "repository": repository,
        "repo_path": repo_path,
        "conversation_id": conversation_id,
        "current_user_id": current_user_id,
        "input_keys": sorted(execution_input.keys()),
        "needs": [
            "prior_changes",
            "prior_attempts",
            "failures",
            "decisions",
            "run_summaries",
            "constraints",
            "next_actions",
        ],
    }


def _context_compaction_scope(
        *,
        workflow: WorkflowDefinition,
        task: TaskDefinition,
        agent: AgentDefinition,
        execution: Execution,
        state: NativeExecutionState,
        record: ContextCompactionRecord,
) -> dict[str, Any]:
    mappings = [
        task.metadata,
        agent.metadata,
        workflow.metadata,
        execution.metadata,
        execution.trigger_payload,
        execution.input_payload,
    ]
    return {
        "workflow_id": workflow.id,
        "workflow_name": workflow.name,
        "execution_id": execution.id,
        "run_id": execution.id,
        "task_id": task.id,
        "task_name": task.name,
        "agent_id": agent.id,
        "agent_name": agent.name,
        "node_id": state.current_node_id,
        "trigger": "context_compaction",
        "conversation_id": _first_mapping_string(mappings, "conversation_id", "conversationId", "thread_id",
                                                 "threadId"),
        "context_pack_id": record.memory_id,
        "compaction_reason": record.reason,
        "source_model_request_id": record.source_model_request_id,
        "estimated_tokens_saved": record.estimated_tokens_saved,
        "compacted": record.compacted,
        "source_event_start_sequence": record.metadata.get("source_event_start_sequence"),
        "source_event_end_sequence": record.metadata.get("source_event_end_sequence"),
        "needs": ["handoff_summary", "decisions", "constraints", "next_actions", "recent_events"],
    }


def _proposal_tool_scope(
        *,
        workflow: WorkflowDefinition,
        task: TaskDefinition,
        agent: AgentDefinition,
        execution: Execution,
        state: NativeExecutionState,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        tool_call_id: str | None,
) -> dict[str, Any]:
    target_type, target_id = _proposal_tool_target(tool, arguments)
    mappings = [
        arguments,
        task.metadata,
        agent.metadata,
        workflow.metadata,
        execution.metadata,
        execution.trigger_payload,
        execution.input_payload,
    ]
    return {
        "workflow_id": workflow.id,
        "workflow_name": workflow.name,
        "execution_id": execution.id,
        "run_id": execution.id,
        "task_id": task.id,
        "task_name": task.name,
        "agent_id": agent.id,
        "agent_name": agent.name,
        "node_id": state.current_node_id,
        "trigger": "proposal_tool",
        "proposal_tool_id": tool.id,
        "proposal_tool_name": tool.name,
        "tool_call_id": tool_call_id,
        "conversation_id": _first_mapping_string(mappings, "conversation_id", "conversationId", "thread_id",
                                                 "threadId"),
        "proposal_target_type": target_type,
        "proposal_target_id": target_id,
        "summary": _first_string(arguments.get("summary"), arguments.get("diff_summary")),
        "goal": _first_string(arguments.get("goal")),
        "argument_keys": sorted(arguments.keys()),
        "needs": ["current_definition", "linked_decisions", "constraints", "prior_changes", "open_questions"],
    }


def _proposal_tool_anchor(
        workflow: WorkflowDefinition,
        task: TaskDefinition,
        agent: AgentDefinition,
        tool: ToolDefinition,
        arguments: dict[str, Any],
) -> tuple[str, str]:
    target_type, target_id = _proposal_tool_target(tool, arguments)
    if tool.id.endswith("propose-create"):
        return "task", task.id
    if target_type in {"workflow", "tool", "agent"} and target_id:
        return target_type, target_id
    if target_type == "workflow":
        return "workflow", workflow.id
    if target_type == "agent":
        return "agent", target_id or agent.id
    return "task", task.id


def _proposal_tool_target(tool: ToolDefinition, arguments: dict[str, Any]) -> tuple[str, str | None]:
    if tool.id == "agency.workflow.propose-update":
        return "workflow", _first_string(arguments.get("workflow_id"), _nested_string(arguments, "workflow", "id"))
    if tool.id == "agency.workflow.propose-create":
        return "workflow", _nested_string(arguments, "workflow", "id")
    if tool.id == "agency.tool.propose-update":
        return "tool", _first_string(arguments.get("tool_id"), _nested_string(arguments, "tool", "id"))
    if tool.id == "agency.tool.propose-create":
        return "tool", _nested_string(arguments, "tool", "id")
    if tool.id == "agency.agent.propose-update":
        return "agent", _first_string(arguments.get("agent_id"), _nested_string(arguments, "agent", "id"))
    return "task", None


def _proposal_tool_intent(tool: ToolDefinition) -> str:
    if tool.id.endswith("propose-create"):
        return "plan"
    return "audit"


def _nested_string(mapping: dict[str, Any], key: str, nested_key: str) -> str | None:
    value = mapping.get(key)
    if not isinstance(value, dict):
        return None
    return _first_string(value.get(nested_key))


def _graph_context_pack_metadata(
        context: dict[str, Any],
        *,
        trigger: str,
        context_pack_id: str | None,
) -> dict[str, Any]:
    query_meta = context.get("query_meta") if isinstance(context.get("query_meta"), dict) else {}
    provenance = context.get("provenance") if isinstance(context.get("provenance"), dict) else {}
    nodes = provenance.get("nodes") if isinstance(provenance.get("nodes"), list) else []
    edges = provenance.get("edges") if isinstance(provenance.get("edges"), list) else []
    return {
        "schema_version": "runtime_graph_context.context_pack_metadata.v1",
        "trigger": trigger,
        "context_pack_id": context_pack_id,
        "status": context.get("status") or "unknown",
        "summary": context.get("summary"),
        "query_meta": {
            "intent": query_meta.get("intent"),
            "budget": query_meta.get("budget"),
            "anchor_type": query_meta.get("anchor_type"),
            "anchor_id": query_meta.get("anchor_id"),
            "node_count": query_meta.get("node_count"),
            "edge_count": query_meta.get("edge_count"),
            "runtime_events_fallback_used": query_meta.get("runtime_events_fallback_used"),
            "fallback_used": query_meta.get("fallback_used"),
        },
        "section_counts": {
            "facts": len(context.get("facts") or []),
            "related_memories": len(context.get("related_memories") or []),
            "recent_events": len(context.get("recent_events") or []),
            "prior_attempts": len(context.get("prior_attempts") or []),
            "prior_changes": len(context.get("prior_changes") or []),
            "failures": len(context.get("failures") or []),
            "decisions": len(context.get("decisions") or []),
            "constraints": len(context.get("constraints") or []),
            "next_actions": len(context.get("next_actions") or []),
        },
        "provenance": {
            "node_ids": [_provenance_id(item) for item in nodes[:25] if _provenance_id(item)],
            "edge_ids": [_provenance_id(item) for item in edges[:25] if _provenance_id(item)],
        },
    }


def _provenance_id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    value = item.get("id")
    return str(value) if value else None


async def _attach_graph_context_metadata_to_memory(
        context: Any,
        memory_id: str | None,
        graph_context_metadata: dict[str, Any],
) -> bool:
    if not memory_id:
        return False
    repo = getattr(context, "memory_repo", None)
    getter = getattr(repo, "get", None)
    saver = getattr(repo, "save", None)
    if getter is None or saver is None:
        return False
    try:
        memory = await getter(memory_id)
        if memory is None:
            return False
        metadata = dict(memory.metadata or {})
        runtime_graph_context = dict(metadata.get("runtime_graph_context") or {})
        runtime_graph_context.update(graph_context_metadata)
        metadata["runtime_graph_context"] = runtime_graph_context
        updated = _memory_with_metadata(memory, metadata)
        await saver(updated)
        return True
    except Exception:
        return False


def _memory_with_metadata(memory: MemoryRecord, metadata: dict[str, Any]) -> MemoryRecord:
    dumped = memory.model_dump(mode="json")
    dumped["metadata"] = metadata
    return MemoryRecord.model_validate(dumped)


def _first_mapping_string(mappings: list[dict[str, Any] | None], *keys: str) -> str | None:
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        nested_values = [
            mapping,
            mapping.get("metadata"),
            mapping.get("runtime_context"),
            mapping.get("trigger"),
            mapping.get("workspace"),
            mapping.get("repository"),
        ]
        for candidate in nested_values:
            if not isinstance(candidate, dict):
                continue
            value = _first_string(*(candidate.get(key) for key in keys))
            if value:
                return value
    return None


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _context_section(context: dict[str, Any], key: str) -> list[dict[str, Any]]:
    section = context.get(key)
    if not isinstance(section, list):
        return []
    return [item for item in section if isinstance(item, dict)]


def _run_summaries_from_context(context: dict[str, Any]) -> list[dict[str, Any]]:
    related_memories = _context_section(context, "related_memories")
    return [
        item
        for item in related_memories
        if str(item.get("memory_type") or "").lower() == "run_summary"
           or "run_summary" in {str(tag).lower() for tag in item.get("tags", []) if isinstance(tag, str)}
    ]


def _missing_goal_supervision_context(goal_id: str, reason: str, *, budget: str) -> dict[str, Any]:
    return {
        "status": "skipped",
        "reason": reason,
        "summary": f"Goal supervision graph context skipped: {reason}.",
        "query_meta": {
            "intent": "supervise_goal",
            "budget": budget if budget in {"brief", "balanced", "full"} else "balanced",
            "anchor_type": "goal",
            "anchor_id": goal_id,
            "runtime_events_fallback_used": False,
        },
        "goal": {"id": goal_id},
        "facts": [],
        "relationships": [],
        "prior_attempts": [],
        "failures": [],
        "decisions": [],
        "constraints": [],
        "next_actions": [],
        "related_memories": [],
        "recent_events": [],
        "artifacts": [],
        "projection_events": [],
    }


async def _goal_supervision_executions(context: Any, goal: GoalDefinition) -> list[Execution]:
    execution_store = getattr(context, "execution_store", None)
    get_execution = getattr(execution_store, "get_execution", None)
    list_executions = getattr(execution_store, "list_executions", None)
    executions: dict[str, Execution] = {}
    if get_execution is not None:
        for execution_id in goal.execution_ids:
            execution = await get_execution(execution_id)
            if execution is not None:
                executions[execution.id] = execution
    if list_executions is not None:
        for execution in await list_executions():
            if execution.goal_id == goal.id or execution.id in goal.execution_ids:
                executions[execution.id] = execution
    return sorted(executions.values(), key=lambda item: (item.created_at, item.id))


def _goal_supervision_goal_context(goal: GoalDefinition) -> dict[str, Any]:
    return {
        "id": goal.id,
        "objective": goal.objective,
        "status": goal.status.value,
        "priority": goal.priority,
        "owner_actor": goal.owner_actor,
        "parent_goal_id": goal.parent_goal_id,
        "success_criteria": _compact_value(goal.success_criteria),
        "constraints": _compact_value(goal.constraints),
        "execution_ids": list(goal.execution_ids),
        "evidence_count": len(goal.evidence),
        "deadline_at": goal.deadline_at.isoformat() if goal.deadline_at is not None else None,
        "completed_at": goal.completed_at.isoformat() if goal.completed_at is not None else None,
    }


def _goal_supervision_execution_context(
        execution: Execution,
        events: list[ExecutionEvent],
        artifacts: list[ExecutionArtifact],
) -> dict[str, Any]:
    failed_events = _latest_events(events, FAILURE_EVENT_TYPES, limit=5)
    next_action_events = [
        event
        for event in events
        if event.event_type in {
            ExecutionEventType.SUBAGENT_NEEDS_INPUT,
            ExecutionEventType.SUBAGENT_NEEDS_APPROVAL,
        }
    ]
    return {
        "id": execution.id,
        "workflow_id": execution.workflow_id,
        "goal_id": execution.goal_id,
        "status": execution.status.value,
        "created_at": execution.created_at.isoformat(),
        "started_at": execution.started_at.isoformat() if execution.started_at is not None else None,
        "completed_at": execution.completed_at.isoformat() if execution.completed_at is not None else None,
        "last_heartbeat_at": execution.last_heartbeat_at.isoformat()
        if execution.last_heartbeat_at is not None else None,
        "error": execution.error,
        "event_count": len(events),
        "artifact_count": len(artifacts),
        "latest_failure_events": [_event_context(event) for event in failed_events],
        "latest_waiting_events": [_event_context(event) for event in next_action_events[-5:]],
    }


def _goal_supervision_failures(execution: Execution, events: list[ExecutionEvent]) -> list[dict[str, Any]]:
    failures = []
    if execution.error:
        failures.append(
            {
                "source": "execution",
                "execution_id": execution.id,
                "workflow_id": execution.workflow_id,
                "status": execution.status.value,
                "error": execution.error[:500],
                "completed_at": execution.completed_at.isoformat() if execution.completed_at is not None else None,
            }
        )
    failures.extend(
        {
            "source": "execution_event",
            "execution_id": execution.id,
            **_event_context(event),
        }
        for event in _latest_events(events, FAILURE_EVENT_TYPES, limit=8)
    )
    return failures


async def _goal_supervision_memories(context: Any, goal: GoalDefinition) -> list[MemoryRecord]:
    repo = getattr(context, "memory_repo", None)
    getter = getattr(repo, "get", None)
    query = getattr(repo, "query", None)
    memories: dict[str, MemoryRecord] = {}
    if getter is not None:
        for memory_id in _goal_supervision_memory_ids(goal):
            memory = await getter(memory_id)
            if memory is not None:
                memories[memory.id] = memory
    if query is not None:
        for memory in await query(tags=[f"goal:{goal.id}"], limit=10):
            memories[memory.id] = memory
    return sorted(memories.values(), key=lambda item: (item.updated_at, item.id), reverse=True)[:10]


def _goal_supervision_memory_ids(goal: GoalDefinition) -> list[str]:
    ids: list[str] = []
    metadata = goal.metadata if isinstance(goal.metadata, dict) else {}
    for item in metadata.get("memory_ids", []):
        if isinstance(item, str) and item.strip():
            ids.append(item.strip())
    for evidence in goal.evidence:
        if not isinstance(evidence, dict):
            continue
        value = evidence.get("memory_id") or evidence.get("memory_ref")
        if isinstance(value, str) and value.strip():
            ids.append(value.strip())
        if str(evidence.get("type") or evidence.get("kind") or "") == "memory":
            value = evidence.get("id")
            if isinstance(value, str) and value.strip():
                ids.append(value.strip())
    return sorted(set(ids))


def _memory_context(memory: MemoryRecord) -> dict[str, Any]:
    return {
        "id": memory.id,
        "scope": memory.scope.value,
        "memory_type": memory.memory_type.value if memory.memory_type is not None else None,
        "status": memory.status.value,
        "summary": memory.summary,
        "content": memory.content[:700],
        "tags": list(memory.tags),
        "workflow_id": memory.workflow_id,
        "source": memory.source,
        "source_execution_id": memory.source_execution_id,
        "updated_at": memory.updated_at.isoformat(),
        "metadata": _compact_mapping(memory.metadata),
    }


async def _goal_supervision_projection_events(context: Any, goal: GoalDefinition) -> list[Any]:
    repo = getattr(context, "graph_projection_event_repo", None)
    list_events = getattr(repo, "list_events", None)
    if list_events is None:
        return []
    events = await list_events(limit=200)
    goal_execution_ids = set(goal.execution_ids)
    selected = [
        event
        for event in events
        if event.aggregate_id == goal.id
           or event.payload.get("goal_id") == goal.id
           or event.payload.get("execution_id") in goal_execution_ids
    ]
    return selected[-25:]


def _projection_event_context(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "status": event.status,
        "occurred_at": event.occurred_at.isoformat(),
        "source_event_id": event.source_event_id,
        "payload": _compact_mapping(event.payload),
    }


def _goal_supervision_relationships(
        goal: GoalDefinition,
        executions: list[Execution],
        memories: list[MemoryRecord],
        projection_events: list[Any],
) -> list[dict[str, Any]]:
    relationships: list[dict[str, Any]] = []
    if goal.parent_goal_id:
        relationships.append({"from": goal.id, "to": goal.parent_goal_id, "type": "child_of_goal"})
    relationships.extend(
        {"from": goal.id, "to": execution.id, "type": "has_execution", "workflow_id": execution.workflow_id}
        for execution in executions
    )
    relationships.extend({"from": goal.id, "to": memory.id, "type": "has_memory"} for memory in memories)
    for evidence in goal.evidence:
        if isinstance(evidence, dict) and evidence.get("id"):
            relationships.append({"from": goal.id, "to": str(evidence["id"]), "type": "has_evidence"})
    relationships.extend(
        {"from": goal.id, "to": event.event_id, "type": "projected_by_event", "event_type": event.event_type}
        for event in projection_events
    )
    return relationships


def _goal_supervision_decisions(goal: GoalDefinition) -> list[dict[str, Any]]:
    monitoring = goal.metadata.get("main_agent_monitoring") if isinstance(goal.metadata, dict) else {}
    if not isinstance(monitoring, dict):
        return []
    decisions = [item for item in monitoring.get("supervisor_decisions", []) if isinstance(item, dict)]
    actions = [item for item in monitoring.get("supervisor_actions", []) if isinstance(item, dict)]
    return [
        {"source": "supervisor_decision", **_compact_mapping(item)}
        for item in decisions[-10:]
    ] + [
        {"source": "supervisor_action", **_compact_mapping(item)}
        for item in actions[-10:]
    ]


def _goal_supervision_next_actions(goal: GoalDefinition) -> list[dict[str, Any]]:
    metadata = goal.metadata if isinstance(goal.metadata, dict) else {}
    planning = metadata.get("goal_planning") if isinstance(metadata.get("goal_planning"), dict) else {}
    active_plan = planning.get("active_plan") if isinstance(planning.get("active_plan"), dict) else {}
    steps = active_plan.get("steps") if isinstance(active_plan.get("steps"), list) else []
    next_steps = [
        step
        for step in steps
        if isinstance(step, dict) and str(step.get("status") or "pending") not in {"completed", "cancelled", "failed"}
    ]
    monitoring = metadata.get("main_agent_monitoring") if isinstance(metadata.get("main_agent_monitoring"),
                                                                     dict) else {}
    last_action = monitoring.get("last_supervisor_action") if isinstance(monitoring, dict) else None
    actions = [{"source": "active_plan", **_compact_mapping(step)} for step in next_steps[:5]]
    if isinstance(last_action, dict):
        actions.append({"source": "last_supervisor_action", **_compact_mapping(last_action)})
    return actions


def _goal_supervision_constraints(goal: GoalDefinition) -> list[dict[str, Any]]:
    constraints = []
    for key, value in goal.constraints.items():
        constraints.append({"source": "goal_constraints", "key": str(key), "value": _compact_value(value)})
    if goal.deadline_at is not None:
        constraints.append({"source": "goal", "key": "deadline_at", "value": goal.deadline_at.isoformat()})
    return constraints


async def _list_execution_events(context: Any, execution_id: str) -> list[ExecutionEvent]:
    execution_store = getattr(context, "execution_store", None)
    list_events = getattr(execution_store, "list_events", None)
    if list_events is None:
        return []
    return list(await list_events(execution_id))


async def _list_execution_artifacts(context: Any, execution_id: str) -> list[ExecutionArtifact]:
    execution_store = getattr(context, "execution_store", None)
    list_artifacts = getattr(execution_store, "list_artifacts", None)
    if list_artifacts is None:
        return []
    return list(await list_artifacts(execution_id))


def _latest_events(
        events: list[ExecutionEvent],
        event_types: set[ExecutionEventType],
        *,
        limit: int,
) -> list[ExecutionEvent]:
    matching = [event for event in events if event.event_type in event_types]
    return sorted(matching, key=lambda event: (event.sequence, event.timestamp, event.id), reverse=True)[:limit]


def _event_context(event: ExecutionEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "event_type": event.event_type.value,
        "sequence": event.sequence,
        "timestamp": event.timestamp.isoformat(),
        "agent_id": event.agent_id,
        "task_id": event.task_id,
        "tool_call_id": event.tool_call_id,
        "model_request_id": event.model_request_id,
        "status": event.status,
        "payload": _compact_mapping(event.payload),
        "metrics": _compact_mapping(event.metrics),
    }


def _artifact_context(artifact: ExecutionArtifact) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "event_id": artifact.event_id,
        "artifact_type": artifact.artifact_type,
        "name": artifact.name,
        "uri": artifact.uri,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "created_at": artifact.created_at.isoformat(),
        "metadata": _compact_mapping(artifact.metadata),
    }


def _compact_mapping(value: dict[str, Any] | None, *, limit: int = 12) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= limit:
            compact["omitted_keys"] = max(len(value) - limit, 0)
            break
        compact[str(key)] = _compact_value(item)
    return compact


def _compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _compact_mapping(value, limit=8)
    if isinstance(value, list):
        return [_compact_value(item) for item in value[:8]]
    if isinstance(value, tuple):
        return [_compact_value(item) for item in list(value)[:8]]
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)[:500]
