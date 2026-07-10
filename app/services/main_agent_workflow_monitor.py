from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core.config import Settings, get_settings
from app.core.time import ensure_utc, utc_now
from app.domain import (
    AgentDefinition,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalTargetType,
    ApprovalType,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    Execution,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    GoalDefinition,
    GoalStatus,
    GraphProjectionEvent,
    WorkflowDefinition,
)
from app.observability.redaction import Redactor
from app.runtime.execution_lifecycle import ResolvedExecutionRuntimePolicy, resolve_execution_runtime_policy
from app.services.agent_tools import (
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    SYSTEM_EXECUTION_EVENTS_TOOL_ID,
    SYSTEM_EXECUTION_GET_TOOL_ID,
    SYSTEM_WORKFLOW_GET_TOOL_ID,
    SYSTEM_WORKFLOW_LIST_TOOL_ID,
)
from app.services.conversations.channel_registry import chat_channel_types
from app.services.conversations.policy import MainAgentPolicyService
from app.services.execution_classification import STALE_EXECUTION_STATUSES, classify_execution_staleness
from app.services.execution_run_summary import ExecutionRunSummaryService
from app.services.executions import ExecutionService
from app.services.goals import ACTIVE_GOAL_STATUSES, GoalEvaluator, GoalService

if TYPE_CHECKING:
    from app.api.context import ApiContext

logger = logging.getLogger(__name__)

TERMINAL_ATTENTION_STATUS_SET = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
}

IMPROVEMENT_FINDING_CATEGORIES = {
    "stale_execution",
    "repeated_failure",
    "missing_approval_gate",
    "missing_validation",
    "low_quality_output",
    "tool_failure",
    "schedule_drift",
    "missing_artifact",
    "timeout",
}

SUPERVISION_ALLOWED_ACTIONS = {
    "pause_execution",
    "resume_execution",
    "cancel_execution",
    "repair_stale_execution",
    "redirect_subagent",
    "replace_task_instructions",
    "request_replan",
    "request_human_review",
    "lower_max_iterations",
    "reduce_tool_scope",
}

SUPERVISION_AUTO_APPLY_ACTIONS = {
    "pause_execution",
    "resume_execution",
    "cancel_execution",
    "repair_stale_execution",
}

GOAL_SUPERVISION_MODES = {"off", "advisory", "guarded", "high_autonomy"}

GOAL_EXECUTION_SIGNAL_CATEGORIES = {
    "context_compaction_failure",
    "context_critical",
    "context_overflow",
    "stale_execution",
    "repeated_tool_call_failure",
    "subagent_blocked",
    "subagent_context_degraded",
    "subagent_low_confidence",
    "subagent_needs_approval",
    "subagent_needs_input",
    "subagent_off_track",
    "subagent_repeated_progress",
    "subagent_step_failed",
    "token_budget_exceeded",
    "token_budget_warning",
}

GOVERNANCE_EVENT_TYPES = {
    ExecutionEventType.TOKEN_BUDGET_WARNING,
    ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
    ExecutionEventType.CONTEXT_HEALTH_RECORDED,
    ExecutionEventType.CONTEXT_COMPACTION_FAILED,
    ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
    ExecutionEventType.SUBAGENT_STEP_FAILED,
    ExecutionEventType.SUBAGENT_NEEDS_INPUT,
    ExecutionEventType.SUBAGENT_NEEDS_APPROVAL,
    ExecutionEventType.TOOL_CALL_FAILED,
}

STEERING_ACTION_BY_FINDING_CATEGORY = {
    "stale_execution": "repair_stale_execution",
    "token_budget_exceeded": "request_replan",
    "context_critical": "request_replan",
    "context_overflow": "request_replan",
    "context_compaction_failure": "request_human_review",
    "repeated_tool_call_failure": "request_human_review",
    "subagent_blocked": "request_human_review",
    "subagent_context_degraded": "request_replan",
    "subagent_low_confidence": "request_replan",
    "subagent_off_track": "redirect_subagent",
    "subagent_repeated_progress": "request_replan",
    "subagent_step_failed": "request_replan",
    "subagent_needs_input": "request_human_review",
    "subagent_needs_approval": "request_human_review",
}

EVALUATION_AGENT_READ_ONLY_TOOL_IDS = {
    SYSTEM_EXECUTION_GET_TOOL_ID,
    SYSTEM_EXECUTION_EVENTS_TOOL_ID,
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    SYSTEM_WORKFLOW_GET_TOOL_ID,
    SYSTEM_WORKFLOW_LIST_TOOL_ID,
}


@dataclass(frozen=True, slots=True)
class MainAgentMonitorFinding:
    category: str
    execution_id: str
    workflow_id: str
    status: str
    severity: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MainAgentWorkflowImprovementProposal:
    workflow_id: str
    diagnosis: dict[str, Any]
    quality_signals: dict[str, Any]
    finding: dict[str, Any]
    proposed_change: dict[str, Any]
    expected_benefit: str
    risk: str
    validation_plan: str
    rollback_plan: str
    restart_active_executions: bool
    requires_human_permission: bool = True
    evaluation_review: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SupervisorSteeringDecision:
    execution_id: str
    workflow_id: str
    finding_event_id: str
    category: str
    severity: str
    recommended_action: str
    reason: str
    status: str = "requested"
    confidence: str = "medium"
    evidence: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MainAgentWorkflowMonitorService:
    context: ApiContext
    settings: Settings | None = None
    _seen_finding_keys: set[str] = field(default_factory=set, init=False, repr=False)

    def workflow_is_monitorable(self, workflow: WorkflowDefinition) -> bool:
        return MainAgentPolicyService(self.context, settings=self._settings()).workflow_is_monitorable_by_main_agent(
            workflow
        )

    def workflow_monitoring_level(self, workflow: WorkflowDefinition) -> str:
        return MainAgentPolicyService(self.context, settings=self._settings()).workflow_monitoring_level(workflow)

    async def run_once(self) -> dict[str, Any]:
        return await self._run_once(goal_ids=None)

    async def run_for_goal(self, goal_id: str) -> dict[str, Any]:
        normalized_goal_id = str(goal_id or "").strip()
        if not normalized_goal_id:
            raise ValueError("goal_id is required")
        result = await self._run_once(goal_ids={normalized_goal_id})
        result["goal_graph_context"] = await self._goal_graph_context(normalized_goal_id)
        return result

    async def _run_once(self, *, goal_ids: set[str] | None) -> dict[str, Any]:
        settings = self._settings()
        workflows = {workflow.id: workflow for workflow in await self.context.workflow_repo.list()}
        scheduled_workflow_ids = await self._scheduled_workflow_ids()
        self_monitor_workflow_id = await self._self_monitor_workflow_id()
        all_executions = await self.context.execution_store.list_executions()
        retention_result = await self._purge_expired_unlinked_findings(settings, all_executions)
        active_executions = await self.context.execution_store.list_active_executions()
        executions_by_id = {execution.id: execution for execution in all_executions}
        active_execution_ids = {execution.id for execution in active_executions}
        findings: list[MainAgentMonitorFinding] = []
        skipped = 0
        scanned_by_level: dict[str, int] = {}

        for execution in active_executions:
            workflow = workflows.get(execution.workflow_id)
            level = self._effective_monitoring_level(
                workflow,
                scheduled_workflow_ids=scheduled_workflow_ids,
                self_monitor_workflow_id=self_monitor_workflow_id,
            )
            if workflow is None or level == "off":
                skipped += 1
                continue
            scanned_by_level[level] = scanned_by_level.get(level, 0) + 1
            finding = self._stale_execution_finding(
                execution,
                workflow=workflow,
                settings=settings,
            )
            if finding is not None:
                findings.append(finding)
            findings.extend(await self._governance_findings(execution, workflow))

        terminal_executions = self._recent_terminal_attention_executions(settings, all_executions)
        for execution in terminal_executions:
            workflow = workflows.get(execution.workflow_id)
            level = self._effective_monitoring_level(
                workflow,
                scheduled_workflow_ids=scheduled_workflow_ids,
                self_monitor_workflow_id=self_monitor_workflow_id,
            )
            if workflow is None or level == "off":
                skipped += 1
                continue
            if level == "minimal" and execution.status == ExecutionStatus.COMPLETED:
                continue
            scanned_by_level[level] = scanned_by_level.get(level, 0) + 1
            findings.append(self._terminal_attention_finding(execution))

        active_goals = await self._active_goals(goal_ids=goal_ids)
        goal_findings = self._goal_supervision_findings(
            active_goals,
            executions_by_id=executions_by_id,
            active_execution_ids=active_execution_ids,
            execution_findings=findings,
        )

        self._seen_finding_keys.update(await self._persisted_finding_keys(all_executions))
        self._seen_finding_keys.update(self._persisted_goal_finding_keys(active_goals))
        emitted_findings = [finding for finding in findings if self._mark_seen(finding)]
        emitted_goal_findings = [finding for finding in goal_findings if self._mark_seen(finding)]
        recorded_findings: list[MainAgentMonitorFinding] = []
        recorded_goal_findings: list[MainAgentMonitorFinding] = []
        proposals: list[MainAgentWorkflowImprovementProposal] = []
        approval_requests: list[dict[str, Any]] = []
        run_summary_results: list[dict[str, Any]] = []
        post_change_comparisons: list[dict[str, Any]] = []
        evaluation_reviews: list[dict[str, Any]] = []
        steering_requests: list[dict[str, Any]] = []
        auto_replans: list[dict[str, Any]] = []
        auto_repairs: list[dict[str, Any]] = []
        auto_investigations: list[dict[str, Any]] = []
        for finding in emitted_findings:
            finding_event = await self._record_finding(finding)
            if finding_event is None:
                continue
            recorded_findings.append(finding)
            workflow = workflows.get(finding.workflow_id)
            execution = executions_by_id.get(finding.execution_id)
            if workflow is not None:
                steering_request = await self._maybe_record_supervisor_steering_request(
                    finding=finding,
                    workflow=workflow,
                    finding_event_id=finding_event.id,
                )
                if steering_request is not None:
                    steering_requests.append(steering_request)
                    steering_approval = await self._maybe_create_steering_approval_request(
                        steering_request=steering_request,
                        workflow=workflow,
                    )
                    if steering_approval is not None:
                        approval_requests.append(steering_approval.model_dump(mode="json"))
            if workflow is not None and execution is not None:
                run_summary_result = await self._maybe_persist_monitor_run_summary(
                    execution=execution,
                    workflow=workflow,
                    finding=finding,
                    finding_event=finding_event,
                )
                if run_summary_result is not None:
                    run_summary_results.append(run_summary_result)
            quality_signals = await self._workflow_quality_signals(
                finding.workflow_id,
                all_executions,
                stale_after_seconds=settings.main_agent_workflow_monitor_stale_after_seconds,
            )
            evaluation_review: dict[str, Any] | None = None
            if workflow is not None and execution is not None:
                review_result = await self._maybe_record_evaluation_review(
                    finding=finding,
                    workflow=workflow,
                    execution=execution,
                    finding_event_id=finding_event.id,
                    quality_signals=quality_signals,
                )
                if review_result is not None:
                    evaluation_reviews.append(review_result)
                    if review_result.get("status") == "recorded":
                        evaluation_review = review_result
            if workflow is not None and execution is not None:
                post_change_comparisons.extend(
                    await self._record_post_change_comparisons(
                        execution=execution,
                        workflow=workflow,
                        quality_signals=quality_signals,
                    )
                )
            proposal = self._proposal_for_finding(
                finding,
                workflow,
                finding_event.id,
                quality_signals,
                evaluation_review=evaluation_review,
            )
            if proposal is not None:
                proposal_event = await self._record_proposal(proposal)
                approval = await self._maybe_create_approval_request(
                    proposal=proposal,
                    proposal_event=proposal_event,
                    workflow=workflow,
                )
                if approval is not None:
                    approval_requests.append(approval.model_dump(mode="json"))
                proposals.append(proposal)

        for finding in emitted_goal_findings:
            finding_record = await self._record_goal_finding(finding)
            if finding_record is not None:
                recorded_goal_findings.append(finding)
                await self._record_goal_supervisor_action(
                    goal_id=str(finding.evidence.get("goal_id") or ""),
                    action="record_supervisor_finding",
                    finding=finding,
                    finding_record=finding_record,
                    status="completed",
                    result={"category": finding.category, "severity": finding.severity},
                )
                auto_replan = await self._maybe_apply_low_risk_goal_replan(
                    finding=finding,
                    finding_record=finding_record,
                )
                if auto_replan is not None:
                    auto_replans.append(auto_replan)
                auto_repair = await self._maybe_apply_goal_stale_repair(
                    finding=finding,
                    finding_record=finding_record,
                )
                if auto_repair is not None:
                    auto_repairs.append(auto_repair)
                auto_investigation = await self._maybe_spawn_read_only_goal_investigation(
                    finding=finding,
                    finding_record=finding_record,
                    executions_by_id=executions_by_id,
                )
                if auto_investigation is not None:
                    auto_investigations.append(auto_investigation)
                approval = await self._maybe_create_goal_supervisor_approval_request(
                    finding=finding,
                    finding_record=finding_record,
                )
                if approval is not None:
                    approval_requests.append(approval.model_dump(mode="json"))

        total_finding_count = len(recorded_findings) + len(recorded_goal_findings)
        self._record_scan(
            active_count=len(active_executions),
            terminal_count=len(terminal_executions),
            skipped=skipped,
            finding_count=total_finding_count,
            scanned_by_level=scanned_by_level,
        )
        return {
            "status": "ok",
            "scope": {"goal_ids": sorted(goal_ids) if goal_ids else []},
            "active_scanned": len(active_executions),
            "terminal_scanned": len(terminal_executions),
            "active_goals_scanned": len(active_goals),
            "skipped": skipped,
            "scanned_by_level": scanned_by_level,
            "findings": [asdict(finding) for finding in recorded_findings],
            "goal_findings": [asdict(finding) for finding in recorded_goal_findings],
            "goal_finding_count": len(recorded_goal_findings),
            "finding_count": total_finding_count,
            "proposals": [asdict(proposal) for proposal in proposals],
            "proposal_count": len(proposals),
            "approval_requests": approval_requests,
            "approval_request_count": len(approval_requests),
            "run_summaries": run_summary_results,
            "run_summary_count": len(run_summary_results),
            "post_change_comparisons": post_change_comparisons,
            "post_change_comparison_count": len(post_change_comparisons),
            "evaluation_reviews": evaluation_reviews,
            "evaluation_review_count": len(evaluation_reviews),
            "steering_requests": steering_requests,
            "steering_request_count": len(steering_requests),
            "steering_applied": [
                request["applied"]
                for request in steering_requests
                if isinstance(request.get("applied"), dict)
            ],
            "steering_applied_count": sum(
                1 for request in steering_requests if isinstance(request.get("applied"), dict)),
            "auto_replans": auto_replans,
            "auto_replan_count": len(auto_replans),
            "auto_repairs": auto_repairs,
            "auto_repair_count": len(auto_repairs),
            "auto_investigations": auto_investigations,
            "auto_investigation_count": len(auto_investigations),
            "retention": retention_result,
        }

    def _settings(self) -> Settings:
        return self.settings or get_settings()

    async def _maybe_spawn_read_only_goal_investigation(
            self,
            *,
            finding: MainAgentMonitorFinding,
            finding_record: ExecutionEvent | dict[str, Any],
            executions_by_id: dict[str, Execution],
    ) -> dict[str, Any] | None:
        policy = finding.evidence.get("supervision_policy") if isinstance(finding.evidence, dict) else {}
        policy = policy if isinstance(policy, dict) else {}
        policy_decision = self._goal_supervisor_policy_decision(policy, "spawn_read_only_investigation")
        if not policy_decision["allowed"] or policy_decision["requires_approval"]:
            return None
        goal_id = str(finding.evidence.get("goal_id") or "").strip()
        if not goal_id:
            return None
        goal = await self.context.goal_repo.get(goal_id)
        if goal is None:
            return None
        workflow_id = self._goal_read_only_investigation_workflow_id(goal)
        if workflow_id is None:
            return None
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None or not self._workflow_is_read_only_investigation(workflow):
            return None
        if self._goal_has_active_investigation(goal, workflow_id, executions_by_id):
            return None

        finding_key = self._goal_finding_record_key(finding, finding_record)
        if self._goal_has_supervisor_action(goal, action="spawn_read_only_investigation", finding_key=finding_key):
            return None

        execution = await ExecutionService(self.context).create_execution(
            workflow_id,
            {
                "goal_id": goal.id,
                "investigation_mode": "read_only",
                "finding_category": finding.category,
                "finding_reason": self._redact_monitor_reason(finding.reason),
                "source_execution_id": finding.evidence.get("source_execution_id") or finding.execution_id,
                "source_event_ids": finding.evidence.get("source_event_ids", []),
                "success_criteria": goal.success_criteria,
            },
            {
                "type": "main_agent_goal_investigation",
                "created_by": "main_agent_monitor",
                "goal_id": goal.id,
                "source": "main_agent_monitor",
                "read_only": True,
                "finding_key": finding_key,
            },
            goal_id=goal.id,
        )
        await self._record_goal_supervisor_decision(
            goal_id=goal.id,
            action="spawn_read_only_investigation",
            finding=finding,
            finding_key=finding_key,
            approval_request_id=None,
            policy=policy,
            risk=policy_decision["risk"],
            rationale=self._redact_monitor_reason(finding.reason),
            policy_decision=policy_decision,
        )
        await self._record_goal_supervisor_action(
            goal_id=goal.id,
            action="spawn_read_only_investigation",
            finding=finding,
            finding_record=finding_record,
            status="completed",
            result={
                "workflow_id": workflow_id,
                "execution_id": execution["id"],
                "read_only": True,
            },
            policy_decision=policy_decision,
        )
        self.context.runtime_operations.increment("main_agent_monitor.goal_auto_investigations")
        self.context.runtime_operations.record_action(
            "main_agent_monitor.goal_auto_investigation",
            goal_id=goal.id,
            workflow_id=workflow_id,
            execution_id=execution["id"],
            finding_category=finding.category,
        )
        return {
            "status": "created",
            "goal_id": goal.id,
            "workflow_id": workflow_id,
            "execution_id": execution["id"],
            "finding_category": finding.category,
            "policy_decision": policy_decision,
        }

    @staticmethod
    def _goal_read_only_investigation_workflow_id(goal: GoalDefinition) -> str | None:
        metadata = goal.metadata if isinstance(goal.metadata, dict) else {}
        monitoring = metadata.get("main_agent_monitoring") if isinstance(metadata.get("main_agent_monitoring"),
                                                                         dict) else {}
        constraints = goal.constraints if isinstance(goal.constraints, dict) else {}
        for source in (constraints, monitoring):
            for key in (
                    "read_only_investigation_workflow_id",
                    "investigation_workflow_id",
                    "follow_up_investigation_workflow_id",
            ):
                value = source.get(key) if isinstance(source, dict) else None
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _workflow_is_read_only_investigation(workflow: WorkflowDefinition) -> bool:
        metadata = workflow.metadata if isinstance(workflow.metadata, dict) else {}
        monitoring = metadata.get("main_agent_monitoring") if isinstance(metadata.get("main_agent_monitoring"),
                                                                         dict) else {}
        # Autonomous investigation spawning must be opt-in read-only, not just a configured workflow id.
        return bool(
            metadata.get("read_only")
            or metadata.get("read_only_investigation")
            or monitoring.get("read_only")
            or monitoring.get("read_only_investigation")
        )

    def _goal_has_active_investigation(
            self,
            goal: GoalDefinition,
            workflow_id: str,
            executions_by_id: dict[str, Execution],
    ) -> bool:
        for execution_id in goal.execution_ids:
            execution = executions_by_id.get(execution_id)
            if execution is None:
                continue
            if execution.workflow_id != workflow_id:
                continue
            if execution.status in {
                ExecutionStatus.CREATED,
                ExecutionStatus.QUEUED,
                ExecutionStatus.RUNNING,
                ExecutionStatus.WAITING_FOR_APPROVAL,
                ExecutionStatus.PAUSED,
            }:
                return True
        return False

    @staticmethod
    def _goal_has_supervisor_action(goal: GoalDefinition, *, action: str, finding_key: str) -> bool:
        monitoring = goal.metadata.get("main_agent_monitoring") if isinstance(goal.metadata, dict) else {}
        if not isinstance(monitoring, dict):
            return False
        actions = [item for item in monitoring.get("supervisor_actions", []) if isinstance(item, dict)]
        return any(item.get("action") == action and item.get("finding_key") == finding_key for item in actions)

    async def _maybe_apply_goal_stale_repair(
            self,
            *,
            finding: MainAgentMonitorFinding,
            finding_record: ExecutionEvent | dict[str, Any],
    ) -> dict[str, Any] | None:
        if str(finding.evidence.get("recommended_action") or "") != "repair_stale_execution":
            return None
        policy = finding.evidence.get("supervision_policy") if isinstance(finding.evidence, dict) else {}
        policy = policy if isinstance(policy, dict) else {}
        policy_decision = self._goal_supervisor_policy_decision(policy, "repair_stale_execution")
        if not policy_decision["allowed"] or policy_decision["requires_approval"]:
            return None
        goal_id = str(finding.evidence.get("goal_id") or "").strip()
        execution_id = str(finding.evidence.get("source_execution_id") or finding.execution_id or "").strip()
        workflow_id = str(finding.evidence.get("source_workflow_id") or finding.workflow_id or "").strip()
        if not goal_id or not execution_id or not workflow_id:
            return None
        goal = await self.context.goal_repo.get(goal_id)
        if goal is None:
            return None

        result = {
            "items": await self.context.control_plane.repair_stale_executions(
                workflow_id=workflow_id,
                execution_id=execution_id,
            )
        }
        finding_key = self._goal_finding_record_key(finding, finding_record)
        await self._record_goal_supervisor_decision(
            goal_id=goal.id,
            action="repair_stale_execution",
            finding=finding,
            finding_key=finding_key,
            approval_request_id=None,
            policy=policy,
            risk=policy_decision["risk"],
            rationale=self._redact_monitor_reason(finding.reason),
            policy_decision=policy_decision,
        )
        await self._record_goal_supervisor_action(
            goal_id=goal.id,
            action="repair_stale_execution",
            finding=finding,
            finding_record=finding_record,
            status="completed",
            result=result,
            policy_decision=policy_decision,
        )
        self.context.runtime_operations.increment("main_agent_monitor.goal_auto_repairs")
        self.context.runtime_operations.record_action(
            "main_agent_monitor.goal_auto_repair",
            goal_id=goal.id,
            execution_id=execution_id,
            workflow_id=workflow_id,
            repaired_count=len(result["items"]),
        )
        return {
            "status": "completed",
            "goal_id": goal.id,
            "execution_id": execution_id,
            "workflow_id": workflow_id,
            "finding_category": finding.category,
            "policy_decision": policy_decision,
            "result": result,
        }

    async def _maybe_apply_low_risk_goal_replan(
            self,
            *,
            finding: MainAgentMonitorFinding,
            finding_record: ExecutionEvent | dict[str, Any],
    ) -> dict[str, Any] | None:
        if str(finding.evidence.get("recommended_action") or "") != "request_replan":
            return None
        policy = finding.evidence.get("supervision_policy") if isinstance(finding.evidence, dict) else {}
        policy = policy if isinstance(policy, dict) else {}
        automatic_actions = {
            str(item)
            for item in policy.get("automatic_actions", [])
            if str(item).strip()
        }
        if "low_risk_replan" not in automatic_actions:
            return None
        policy_decision = self._goal_supervisor_policy_decision(policy, "low_risk_replan")
        if not policy_decision["allowed"] or policy_decision["requires_approval"]:
            return None
        goal_id = str(finding.evidence.get("goal_id") or "").strip()
        if not goal_id:
            return None
        goal = await self.context.goal_repo.get(goal_id)
        if goal is None:
            return None

        plan = self._low_risk_goal_replan_payload(goal, finding)
        reason = self._low_risk_goal_replan_reason(finding)
        replanned = await GoalService(self.context).replan_goal(
            goal.id,
            plan=plan,
            reason=reason,
            actor="main_agent_monitor",
        )
        finding_key = self._goal_finding_record_key(finding, finding_record)
        active_plan = self._goal_active_plan(replanned) or {}
        await self._record_goal_supervisor_decision(
            goal_id=goal.id,
            action="low_risk_replan",
            finding=finding,
            finding_key=finding_key,
            approval_request_id=None,
            policy=policy,
            risk=policy_decision["risk"],
            rationale=self._redact_monitor_reason(reason),
            policy_decision=policy_decision,
        )
        await self._record_goal_supervisor_action(
            goal_id=goal.id,
            action="low_risk_replan",
            finding=finding,
            finding_record=finding_record,
            status="completed",
            result={
                "goal_id": goal.id,
                "plan_version": active_plan.get("version"),
                "reason": reason,
                "summary": active_plan.get("summary"),
            },
            policy_decision=policy_decision,
        )
        self.context.runtime_operations.increment("main_agent_monitor.goal_auto_replans")
        self.context.runtime_operations.record_action(
            "main_agent_monitor.goal_auto_replan",
            goal_id=goal.id,
            finding_category=finding.category,
            plan_version=active_plan.get("version"),
        )
        return {
            "status": "completed",
            "goal_id": goal.id,
            "finding_category": finding.category,
            "policy_decision": policy_decision,
            "plan_version": active_plan.get("version"),
            "reason": reason,
            "plan": active_plan,
        }

    def _low_risk_goal_replan_payload(
            self,
            goal: GoalDefinition,
            finding: MainAgentMonitorFinding,
    ) -> dict[str, Any]:
        evidence = finding.evidence if isinstance(finding.evidence, dict) else {}
        source_execution_ids = [
            str(item)
            for item in evidence.get("source_execution_ids", [])
            if isinstance(item, str) and item.strip()
        ]
        workflow_id = str(finding.workflow_id or "").strip()
        steps: list[dict[str, Any]] = [
            {
                "id": f"inspect-{execution_id}",
                "action": "inspect_execution",
                "status": "pending",
                "execution_id": execution_id,
                "expected_evidence": ["failure_signature", "partial_outputs", "recovery_options"],
            }
            for execution_id in source_execution_ids[:5]
        ]
        steps.append(
            {
                "id": "retrieve-goal-context",
                "action": "retrieve_memory",
                "status": "pending",
                "query": goal.objective,
                "expected_evidence": ["goal_summary", "prior_decisions", "unresolved_blockers"],
            }
        )
        if workflow_id:
            steps.append(
                {
                    "id": "retry-workflow-read-only",
                    "action": "start_workflow",
                    "status": "pending",
                    "workflow_id": workflow_id,
                    "input_payload": {
                        "goal_id": goal.id,
                        "supervisor_replan": True,
                        "source_execution_ids": source_execution_ids,
                        "replan_reason": self._low_risk_goal_replan_reason(finding),
                    },
                    "assigned_agents": [],
                    "expected_evidence": goal.success_criteria,
                }
            )
        steps.append(
            {
                "id": "evaluate-replan-evidence",
                "action": "evaluate_evidence",
                "status": "pending",
                "expected_evidence": goal.success_criteria,
            }
        )
        return {
            "summary": f"Low-risk supervisor replan for {goal.objective}",
            "steps": steps,
            "expected_evidence": goal.success_criteria,
        }

    @staticmethod
    def _low_risk_goal_replan_reason(finding: MainAgentMonitorFinding) -> str:
        return (
            f"Automatic low-risk replan after {finding.category}: "
            f"{finding.reason}"
        )[:1000]

    async def _goal_graph_context(self, goal_id: str) -> dict[str, Any]:
        try:
            from app.runtime.native.graph_context import RuntimeGraphContextAutoRetriever

            return await RuntimeGraphContextAutoRetriever(self.context).retrieve_for_goal_supervision(goal_id)
        except Exception as exc:
            logger.warning("Failed to retrieve goal supervision graph context", exc_info=True)
            return {
                "status": "error",
                "reason": str(exc),
                "summary": "Goal supervision graph context retrieval failed.",
                "query_meta": {
                    "intent": "supervise_goal",
                    "budget": "balanced",
                    "anchor_type": "goal",
                    "anchor_id": goal_id,
                },
            }

    def _redact_monitor_value(self, value: Any) -> Any:
        redacted, _ = Redactor(enabled=True).redact_value(value)
        return redacted

    def _redact_monitor_reason(self, reason: str) -> str:
        # Keep monitor taxonomy words like "token_budget_exceeded" intact while scrubbing runtime secrets.
        redacted = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", reason)
        redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{10,}", "[REDACTED]", redacted)
        return redacted

    def _redacted_monitor_finding_payload(self, finding: MainAgentMonitorFinding) -> dict[str, Any]:
        payload = asdict(finding)
        payload["reason"] = self._redact_monitor_reason(str(payload.get("reason") or ""))
        payload["evidence"] = self._redact_monitor_value(payload.get("evidence", {}))
        return payload

    async def _purge_expired_unlinked_findings(
            self,
            settings: Settings,
            executions: list[Execution],
    ) -> dict[str, Any]:
        retention_days = int(settings.main_agent_workflow_monitor_finding_retention_days)
        cutoff = utc_now() - timedelta(days=retention_days)
        events: list[ExecutionEvent] = []
        for execution in executions:
            events.extend(await self.context.execution_store.list_events(execution.id))
        referenced_event_ids = self._referenced_monitor_event_ids(events)
        for approval in await self.context.conversation_approval_repo.list():
            referenced_event_ids.update(self._event_ids_in_value(approval.metadata))
            referenced_event_ids.update(self._event_ids_in_value(approval.proposed_payload))

        expired_findings = [
            event
            for event in events
            if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED
               and ensure_utc(event.timestamp) < cutoff
               and event.id not in referenced_event_ids
        ]
        purged_count = 0
        for event in expired_findings:
            if await self.context.execution_store.delete_event(event.id):
                purged_count += 1
        goal_retention = await self._purge_expired_goal_monitoring_records(cutoff)
        if purged_count:
            self.context.runtime_operations.increment("main_agent_monitor.findings.purged", purged_count)
            self.context.runtime_operations.record_action(
                "main_agent_monitor.finding_retention",
                retention_days=retention_days,
                cutoff_at=cutoff.isoformat(),
                purged_count=purged_count,
            )
        return {
            "retention_days": retention_days,
            "cutoff_at": cutoff.isoformat(),
            "purged_finding_count": purged_count,
            **goal_retention,
        }

    async def _purge_expired_goal_monitoring_records(self, cutoff: Any) -> dict[str, Any]:
        pending_approval_ids = {
            approval.id
            for approval in await self.context.conversation_approval_repo.list()
            if approval.status == ApprovalStatus.PENDING
        }
        purged_counts = {
            "purged_goal_finding_count": 0,
            "purged_goal_supervisor_action_count": 0,
            "purged_goal_supervisor_decision_count": 0,
            "purged_goal_approval_request_count": 0,
        }
        for goal in await self.context.goal_repo.list():
            monitoring = goal.metadata.get("main_agent_monitoring") if isinstance(goal.metadata, dict) else None
            if not isinstance(monitoring, dict):
                continue
            updated_monitoring = dict(monitoring)
            changed = False

            for key, count_key, last_key in (
                    ("findings", "purged_goal_finding_count", None),
                    ("supervisor_actions", "purged_goal_supervisor_action_count", "last_supervisor_action"),
                    ("supervisor_decisions", "purged_goal_supervisor_decision_count", "last_supervisor_decision"),
                    ("approval_requests", "purged_goal_approval_request_count", "last_approval_request"),
            ):
                items = [item for item in monitoring.get(key, []) if isinstance(item, dict)]
                if not items:
                    continue
                retained = [
                    item
                    for item in items
                    if not self._goal_monitoring_record_expired(
                        item,
                        cutoff=cutoff,
                        last_record=monitoring.get(last_key) if last_key else None,
                        pending_approval_ids=pending_approval_ids,
                    )
                ]
                purged = len(items) - len(retained)
                if purged:
                    purged_counts[count_key] += purged
                    updated_monitoring[key] = retained
                    changed = True

            if changed:
                metadata = dict(goal.metadata)
                metadata["main_agent_monitoring"] = updated_monitoring
                await self.context.goal_repo.save(
                    goal.model_copy(update={"metadata": metadata, "updated_at": utc_now()}))

        total = sum(purged_counts.values())
        if total:
            self.context.runtime_operations.increment("main_agent_monitor.goal_monitoring_records.purged", total)
            self.context.runtime_operations.record_action(
                "main_agent_monitor.goal_monitoring_retention",
                cutoff_at=cutoff.isoformat(),
                purged_count=total,
                **purged_counts,
            )
        return purged_counts

    def _goal_monitoring_record_expired(
            self,
            record: dict[str, Any],
            *,
            cutoff: Any,
            last_record: Any,
            pending_approval_ids: set[str],
    ) -> bool:
        if isinstance(last_record, dict) and self._goal_monitoring_record_identity(
                record) == self._goal_monitoring_record_identity(last_record):
            return False
        approval_request_id = record.get("approval_request_id")
        if isinstance(approval_request_id, str) and approval_request_id in pending_approval_ids:
            return False
        recorded_at = record.get("recorded_at")
        if not isinstance(recorded_at, str) or not recorded_at.strip():
            return False
        try:
            return ensure_utc(datetime.fromisoformat(recorded_at)) < cutoff
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _goal_monitoring_record_identity(record: dict[str, Any]) -> tuple[Any, Any, Any]:
        return (
            record.get("id"),
            record.get("dedupe_key") or record.get("finding_key"),
            record.get("approval_request_id"),
        )

    def _referenced_monitor_event_ids(self, events: list[ExecutionEvent]) -> set[str]:
        referenced: set[str] = set()
        for event in events:
            if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED:
                continue
            if event.event_type in {
                ExecutionEventType.MONITOR_EVALUATION_RECORDED,
                ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
                ExecutionEventType.MONITOR_IMPROVEMENT_COMPARED,
                ExecutionEventType.SUPERVISOR_STEERING_REQUESTED,
                ExecutionEventType.SUPERVISOR_STEERING_APPLIED,
            }:
                referenced.update(self._event_ids_in_value(event.payload))
                referenced.update(self._event_ids_in_value(event.metadata))
        return referenced

    def _event_ids_in_value(self, value: Any) -> set[str]:
        event_ids: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and (key.endswith("event_id") or key == "event_id"):
                    event_ids.add(item)
                else:
                    event_ids.update(self._event_ids_in_value(item))
        elif isinstance(value, list):
            for item in value:
                event_ids.update(self._event_ids_in_value(item))
        return event_ids

    async def _scheduled_workflow_ids(self) -> set[str]:
        schedules = await self.context.schedule_repo.list()
        return {
            schedule.workflow_id
            for schedule in schedules
            if schedule.enabled and getattr(schedule.trigger_type, "value", schedule.trigger_type) != "manual"
        }

    async def _self_monitor_workflow_id(self) -> str | None:
        profile = await self._active_main_agent_profile()
        return getattr(profile, "default_workflow_id", None)

    async def _active_goals(self, *, goal_ids: set[str] | None = None) -> list[GoalDefinition]:
        goals = await self.context.goal_repo.list()
        if goal_ids:
            goals = [goal for goal in goals if goal.id in goal_ids]
        return [goal for goal in goals if goal.status in ACTIVE_GOAL_STATUSES]

    def _goal_supervision_findings(
            self,
            goals: list[GoalDefinition],
            *,
            executions_by_id: dict[str, Execution],
            active_execution_ids: set[str],
            execution_findings: list[MainAgentMonitorFinding],
    ) -> list[MainAgentMonitorFinding]:
        findings: list[MainAgentMonitorFinding] = []
        for goal in goals:
            policy = self._goal_supervision_policy(goal)
            if not policy["enabled"] or not policy["can_inspect"]:
                continue
            if goal.status in {GoalStatus.PAUSED, GoalStatus.WAITING_FOR_INPUT, GoalStatus.WAITING_FOR_APPROVAL}:
                continue
            linked_executions = [
                executions_by_id[execution_id]
                for execution_id in goal.execution_ids
                if execution_id in executions_by_id
            ]
            goal_signal_findings = self._goal_execution_signal_findings(
                goal,
                linked_executions,
                execution_findings=execution_findings,
                policy=policy,
            )
            if goal_signal_findings:
                findings.extend(goal_signal_findings)
                continue
            limit_finding = self._goal_supervision_limit_finding(
                goal,
                linked_executions,
                policy=policy,
            )
            if limit_finding is not None:
                findings.append(limit_finding)
                continue
            repeated_failure_finding = self._repeated_goal_failure_finding(
                goal,
                linked_executions,
                policy=policy,
            )
            if repeated_failure_finding is not None:
                findings.append(repeated_failure_finding)
                continue
            active_linked_executions = [
                execution for execution in linked_executions if execution.id in active_execution_ids
            ]
            plan_mismatch_finding = self._goal_plan_mismatch_finding(
                goal,
                active_linked_executions,
                policy=policy,
            )
            if plan_mismatch_finding is not None:
                findings.append(plan_mismatch_finding)
                continue
            if active_linked_executions:
                continue
            missing_evidence_finding = self._missing_goal_evidence_finding(
                goal,
                linked_executions,
                policy=policy,
            )
            if missing_evidence_finding is not None:
                findings.append(missing_evidence_finding)
                continue
            if self._goal_has_planned_next_action(goal):
                continue
            findings.append(self._stalled_goal_finding(goal, linked_executions, policy=policy))
        return findings

    def _goal_supervision_policy(self, goal: GoalDefinition) -> dict[str, Any]:
        constraints = goal.constraints if isinstance(goal.constraints, dict) else {}
        monitoring = goal.metadata.get("main_agent_monitoring") if isinstance(goal.metadata, dict) else None
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        requested_mode = "guarded"
        source = "default"
        for key in ("supervision_mode", "autonomy_mode", "autonomy"):
            if constraints.get(key):
                requested_mode = constraints[key]
                source = "goal.constraints"
                break
        else:
            for key in ("supervision_mode", "autonomy_mode", "autonomy"):
                if monitoring.get(key):
                    requested_mode = monitoring[key]
                    source = "goal.metadata"
                    break
        mode = str(requested_mode).strip().lower()
        if mode not in GOAL_SUPERVISION_MODES:
            mode = "guarded"
            source = "default"
        if monitoring.get("enabled") is False:
            mode = "off"
            source = "goal.metadata"

        can_inspect = mode in {"advisory", "guarded", "high_autonomy"}
        can_record_findings = can_inspect
        automatic_actions = ["read_only_inspection", "summarize_goal_state"] if can_inspect else []
        if can_record_findings:
            automatic_actions.append("record_supervisor_finding")
            automatic_actions.append("request_human_approval")
        if mode in {"guarded", "high_autonomy"}:
            automatic_actions.append("request_more_work_when_evidence_is_insufficient")
            automatic_actions.append("repair_stale_execution")
        if mode == "high_autonomy":
            automatic_actions.extend(["spawn_read_only_investigation", "low_risk_replan"])

        approval_required_actions = [
            "workflow_definition_mutation",
            "tool_definition_mutation",
            "shell_side_effect",
            "external_write",
            "purchase",
            "delete",
            "physical_world_action",
            "high_priority_or_user_created_goal_cancellation",
        ]
        for source_mapping in (constraints, monitoring):
            approval_policy = source_mapping.get("approval_policy") if isinstance(source_mapping, dict) else None
            configured = (
                approval_policy.get("approval_required_actions")
                if isinstance(approval_policy, dict)
                else source_mapping.get("approval_required_actions")
                if isinstance(source_mapping, dict)
                else None
            )
            if isinstance(configured, list):
                approval_required_actions.extend(str(item) for item in configured if str(item).strip())

        return {
            "enabled": mode != "off",
            "mode": mode,
            "source": source,
            "can_inspect": can_inspect,
            "can_summarize": can_inspect,
            "can_record_findings": can_record_findings,
            "automatic_actions": automatic_actions,
            "approval_required_actions": sorted(set(approval_required_actions)),
            "requires_approval_for_mutations": True,
        }

    def _goal_has_planned_next_action(self, goal: GoalDefinition) -> bool:
        plan = self._goal_active_plan(goal)
        if not isinstance(plan, dict):
            return False
        next_action = plan.get("next_action")
        if isinstance(next_action, dict) and next_action:
            return True
        if isinstance(next_action, str) and next_action.strip():
            return True
        steps = plan.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                status = str(step.get("status") or "").lower()
                if status in {"pending", "ready", "active", "in_progress"}:
                    return True
        return False

    def _stalled_goal_finding(
            self,
            goal: GoalDefinition,
            linked_executions: list[Execution],
            *,
            policy: dict[str, Any],
    ) -> MainAgentMonitorFinding:
        anchor_execution = self._goal_finding_anchor_execution(linked_executions)
        execution_id = anchor_execution.id if anchor_execution is not None else self._goal_monitor_anchor_id(goal.id)
        workflow_id = anchor_execution.workflow_id if anchor_execution is not None else ""
        latest_execution = anchor_execution
        return MainAgentMonitorFinding(
            category="stalled_goal",
            execution_id=execution_id,
            workflow_id=workflow_id,
            status=goal.status.value,
            severity="medium",
            reason="Goal has no active execution and no planned next supervisor action.",
            evidence={
                "goal_id": goal.id,
                "objective": goal.objective,
                "priority": goal.priority,
                "owner_actor": goal.owner_actor,
                "goal_status": goal.status.value,
                "execution_ids": list(goal.execution_ids),
                "active_execution_count": 0,
                "latest_execution_id": latest_execution.id if latest_execution is not None else None,
                "latest_execution_status": latest_execution.status.value if latest_execution is not None else None,
                "anchor_type": "goal",
                "anchor_id": goal.id,
                "intent": "supervise_goal",
                "supervision_policy": policy,
            },
        )

    def _goal_execution_signal_findings(
            self,
            goal: GoalDefinition,
            linked_executions: list[Execution],
            *,
            execution_findings: list[MainAgentMonitorFinding],
            policy: dict[str, Any],
    ) -> list[MainAgentMonitorFinding]:
        linked_execution_ids = {execution.id for execution in linked_executions}
        if not linked_execution_ids:
            return []
        signals: list[MainAgentMonitorFinding] = []
        for source in execution_findings:
            if source.execution_id not in linked_execution_ids:
                continue
            if source.category not in GOAL_EXECUTION_SIGNAL_CATEGORIES:
                continue
            budget_finding = self._goal_budget_control_finding(goal, source, policy=policy)
            if budget_finding is not None:
                signals.append(budget_finding)
                continue
            signals.append(self._goal_execution_signal_finding(goal, source, policy=policy))
        return signals

    def _goal_budget_control_finding(
            self,
            goal: GoalDefinition,
            source: MainAgentMonitorFinding,
            *,
            policy: dict[str, Any],
    ) -> MainAgentMonitorFinding | None:
        if self._goal_execution_signal_group(source.category) != "token_budget":
            return None
        budget_policy = self._goal_budget_policy(goal)
        max_tokens = budget_policy.get("max_tokens")
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            return None
        source_budget = source.evidence.get("budget") if isinstance(source.evidence.get("budget"), dict) else {}
        used_tokens = self._non_negative_int(source_budget.get("used_tokens"))
        if used_tokens is None:
            return None
        warn_ratio = budget_policy.get("warn_ratio") if isinstance(budget_policy.get("warn_ratio"),
                                                                   int | float) else 0.8
        warn_threshold = max_tokens * float(warn_ratio)
        exceeded = used_tokens >= max_tokens
        warning = used_tokens >= warn_threshold
        if not exceeded and not warning:
            return None
        source_event_ids = sorted(self._source_event_ids_from_evidence(source.evidence))
        source_key = self._finding_key(
            category=source.category,
            workflow_id=source.workflow_id,
            execution_id=source.execution_id,
            status=source.status,
            evidence=source.evidence,
        )
        return MainAgentMonitorFinding(
            category="goal_budget_exceeded" if exceeded else "goal_budget_warning",
            execution_id=source.execution_id,
            workflow_id=source.workflow_id,
            status=goal.status.value,
            severity="critical" if exceeded else "medium",
            reason=(
                f"Goal token budget {'exceeded' if exceeded else 'warning'}: "
                f"{used_tokens}/{max_tokens} tokens."
            ),
            evidence={
                "goal_id": goal.id,
                "objective": goal.objective,
                "priority": goal.priority,
                "owner_actor": goal.owner_actor,
                "goal_status": goal.status.value,
                "budget_policy": budget_policy,
                "budget": source_budget,
                "used_tokens": used_tokens,
                "max_tokens": max_tokens,
                "warn_ratio": warn_ratio,
                "warn_threshold": warn_threshold,
                "budget_exceeded": exceeded,
                "budget_warning": warning,
                "signal_category": source.category,
                "source_execution_id": source.execution_id,
                "source_workflow_id": source.workflow_id,
                "source_finding_key": source_key,
                "source_event_ids": source_event_ids,
                "source_event_id": source_event_ids[0] if len(source_event_ids) == 1 else None,
                "recommended_action": "review_goal_budget_or_context",
                "anchor_type": "execution",
                "anchor_id": source.execution_id,
                "intent": "enforce_goal_budget",
                "supervision_policy": policy,
            },
        )

    def _goal_budget_policy(self, goal: GoalDefinition) -> dict[str, Any]:
        constraints = goal.constraints if isinstance(goal.constraints, dict) else {}
        monitoring = goal.metadata.get("main_agent_monitoring") if isinstance(goal.metadata, dict) else None
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        merged: dict[str, Any] = {}
        for source_mapping in (constraints, monitoring):
            budget = source_mapping.get("budget") if isinstance(source_mapping, dict) else None
            if isinstance(budget, dict):
                merged.update(budget)
            token_budget = source_mapping.get("token_budget") if isinstance(source_mapping, dict) else None
            if isinstance(token_budget, dict):
                merged.update(token_budget)
            for key in ("max_tokens", "warn_ratio"):
                if isinstance(source_mapping, dict) and key in source_mapping:
                    merged[key] = source_mapping[key]
        max_tokens = (
                self._non_negative_int(merged.get("max_tokens"))
                or self._non_negative_int(merged.get("token_budget"))
                or self._non_negative_int(merged.get("budget_tokens"))
        )
        warn_ratio = self._ratio(merged.get("warn_ratio"))
        return {
            "max_tokens": max_tokens,
            "warn_ratio": warn_ratio if warn_ratio is not None else 0.8,
        }

    def _goal_execution_signal_finding(
            self,
            goal: GoalDefinition,
            source: MainAgentMonitorFinding,
            *,
            policy: dict[str, Any],
    ) -> MainAgentMonitorFinding:
        source_event_ids = sorted(self._source_event_ids_from_evidence(source.evidence))
        source_key = self._finding_key(
            category=source.category,
            workflow_id=source.workflow_id,
            execution_id=source.execution_id,
            status=source.status,
            evidence=source.evidence,
        )
        signal_group = self._goal_execution_signal_group(source.category)
        return MainAgentMonitorFinding(
            category="goal_execution_signal",
            execution_id=source.execution_id,
            workflow_id=source.workflow_id,
            status=goal.status.value,
            severity=source.severity,
            reason=f"Goal-linked execution signal requires supervisor attention: {source.reason}",
            evidence={
                "goal_id": goal.id,
                "objective": goal.objective,
                "priority": goal.priority,
                "owner_actor": goal.owner_actor,
                "goal_status": goal.status.value,
                "signal_category": source.category,
                "signal_group": signal_group,
                "signal_reason": source.reason,
                "signal_severity": source.severity,
                "source_execution_id": source.execution_id,
                "source_workflow_id": source.workflow_id,
                "source_finding_key": source_key,
                "source_event_ids": source_event_ids,
                "source_event_id": source_event_ids[0] if len(source_event_ids) == 1 else None,
                "source_evidence": source.evidence,
                "recommended_action": self._goal_execution_signal_action(signal_group),
                "anchor_type": "execution",
                "anchor_id": source.execution_id,
                "intent": "supervise_goal_execution_signal",
                "supervision_policy": policy,
            },
        )

    @staticmethod
    def _goal_execution_signal_group(category: str) -> str:
        if category == "stale_execution":
            return "stale_execution"
        if category.startswith("subagent_"):
            return "subagent"
        if category.startswith("token_budget_"):
            return "token_budget"
        if category.startswith("context_"):
            return "context_health"
        if category == "repeated_tool_call_failure":
            return "tool_failure"
        return "execution"

    @staticmethod
    def _goal_execution_signal_action(signal_group: str) -> str:
        return {
            "stale_execution": "repair_stale_execution",
            "subagent": "inspect_or_redirect_subagent",
            "token_budget": "review_goal_budget_or_context",
            "context_health": "review_goal_budget_or_context",
            "tool_failure": "inspect_tool_failure_and_replan",
        }.get(signal_group, "inspect_goal_execution")

    def _goal_supervision_limit_finding(
            self,
            goal: GoalDefinition,
            linked_executions: list[Execution],
            *,
            policy: dict[str, Any],
    ) -> MainAgentMonitorFinding | None:
        limits = self._goal_supervision_limits(goal)
        max_retry = limits.get("max_retry_count")
        max_replan = limits.get("max_replan_count")
        failed_executions = [
            execution for execution in linked_executions if execution.status == ExecutionStatus.FAILED
        ]
        replan_count = self._goal_replan_count(goal)
        retry_limit_reached = isinstance(max_retry, int) and max_retry >= 0 and len(failed_executions) >= max_retry
        replan_limit_reached = isinstance(max_replan, int) and max_replan >= 0 and replan_count >= max_replan
        if not retry_limit_reached and not replan_limit_reached:
            return None
        anchor_execution = self._goal_finding_anchor_execution(failed_executions or linked_executions)
        execution_id = anchor_execution.id if anchor_execution is not None else self._goal_monitor_anchor_id(goal.id)
        workflow_id = anchor_execution.workflow_id if anchor_execution is not None else ""
        reasons = []
        if retry_limit_reached:
            reasons.append(f"failed executions {len(failed_executions)} reached retry limit {max_retry}")
        if replan_limit_reached:
            reasons.append(f"replans {replan_count} reached replan limit {max_replan}")
        return MainAgentMonitorFinding(
            category="goal_supervision_limit_reached",
            execution_id=execution_id,
            workflow_id=workflow_id,
            status=goal.status.value,
            severity="critical",
            reason=f"Goal supervision loop guard reached: {'; '.join(reasons)}.",
            evidence={
                "goal_id": goal.id,
                "objective": goal.objective,
                "priority": goal.priority,
                "owner_actor": goal.owner_actor,
                "goal_status": goal.status.value,
                "failed_execution_count": len(failed_executions),
                "failed_execution_ids": [execution.id for execution in failed_executions],
                "source_execution_ids": [execution.id for execution in failed_executions] or list(goal.execution_ids),
                "replan_count": replan_count,
                "max_retry_count": max_retry,
                "max_replan_count": max_replan,
                "retry_limit_reached": retry_limit_reached,
                "replan_limit_reached": replan_limit_reached,
                "limits": limits,
                "active_plan_version": self._goal_active_plan_version(goal),
                "recommended_action": "escalate_goal_loop_guard",
                "anchor_type": "execution" if anchor_execution is not None else "goal",
                "anchor_id": execution_id if anchor_execution is not None else goal.id,
                "intent": "escalate_goal_supervision",
                "supervision_policy": policy,
            },
        )

    def _goal_supervision_limits(self, goal: GoalDefinition) -> dict[str, Any]:
        constraints = goal.constraints if isinstance(goal.constraints, dict) else {}
        monitoring = goal.metadata.get("main_agent_monitoring") if isinstance(goal.metadata, dict) else None
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        limits: dict[str, Any] = {}
        for source_mapping in (constraints, monitoring):
            limit_policy = source_mapping.get("supervision_limits") if isinstance(source_mapping, dict) else None
            if isinstance(limit_policy, dict):
                limits.update(limit_policy)
            for key in ("max_retry_count", "max_replan_count"):
                if isinstance(source_mapping, dict) and key in source_mapping:
                    limits[key] = source_mapping[key]
        return {
            "max_retry_count": self._non_negative_int(limits.get("max_retry_count")),
            "max_replan_count": self._non_negative_int(limits.get("max_replan_count")),
        }

    def _goal_replan_count(self, goal: GoalDefinition) -> int:
        metadata = goal.metadata if isinstance(goal.metadata, dict) else {}
        planning = metadata.get("goal_planning")
        if not isinstance(planning, dict):
            return 0
        history = planning.get("plan_history")
        history_count = len([item for item in history if isinstance(item, dict)]) if isinstance(history, list) else 0
        active_plan = planning.get("active_plan")
        active_version = active_plan.get("version") if isinstance(active_plan, dict) else None
        if isinstance(active_version, int) and active_version > 0:
            return max(0, active_version - 1)
        return history_count

    def _goal_active_plan_version(self, goal: GoalDefinition) -> int | None:
        active_plan = self._goal_active_plan(goal)
        version = active_plan.get("version") if isinstance(active_plan, dict) else None
        return version if isinstance(version, int) else None

    @staticmethod
    def _non_negative_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return None

    @staticmethod
    def _ratio(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            candidate = float(value)
        elif isinstance(value, str):
            try:
                candidate = float(value.strip())
            except ValueError:
                return None
        else:
            return None
        return candidate if 0 < candidate <= 1 else None

    def _goal_plan_mismatch_finding(
            self,
            goal: GoalDefinition,
            active_linked_executions: list[Execution],
            *,
            policy: dict[str, Any],
    ) -> MainAgentMonitorFinding | None:
        if not active_linked_executions:
            return None
        active_plan = self._goal_active_plan(goal)
        expected_workflow_ids = self._active_plan_workflow_ids(active_plan)
        if not expected_workflow_ids:
            return None
        mismatched_executions = [
            execution for execution in active_linked_executions if execution.workflow_id not in expected_workflow_ids
        ]
        if not mismatched_executions:
            return None
        anchor_execution = sorted(
            mismatched_executions,
            key=lambda item: (item.updated_at, item.created_at, item.id),
            reverse=True,
        )[0]
        return MainAgentMonitorFinding(
            category="goal_plan_mismatch",
            execution_id=anchor_execution.id,
            workflow_id=anchor_execution.workflow_id,
            status=goal.status.value,
            severity="medium",
            reason="Goal has active execution work that no longer matches the current goal plan.",
            evidence={
                "goal_id": goal.id,
                "objective": goal.objective,
                "priority": goal.priority,
                "owner_actor": goal.owner_actor,
                "goal_status": goal.status.value,
                "active_plan_version": active_plan.get("version") if isinstance(active_plan, dict) else None,
                "expected_workflow_ids": sorted(expected_workflow_ids),
                "mismatched_execution_ids": [execution.id for execution in mismatched_executions],
                "source_execution_ids": [execution.id for execution in mismatched_executions],
                "mismatched_workflow_ids": sorted({execution.workflow_id for execution in mismatched_executions}),
                "active_execution_ids": [execution.id for execution in active_linked_executions],
                "recommended_action": "inspect_or_redirect_active_execution",
                "anchor_type": "execution",
                "anchor_id": anchor_execution.id,
                "intent": "align_execution_with_goal_plan",
                "supervision_policy": policy,
            },
        )

    @staticmethod
    def _active_plan_workflow_ids(active_plan: dict[str, Any] | None) -> set[str]:
        if not isinstance(active_plan, dict):
            return set()
        steps = active_plan.get("steps")
        if not isinstance(steps, list):
            return set()
        workflow_ids: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                continue
            action = str(step.get("action") or "").strip().lower()
            if action != "start_workflow":
                continue
            status = str(step.get("status") or "").strip().lower()
            if status in {"completed", "cancelled", "failed", "skipped", "superseded"}:
                continue
            workflow_id = step.get("workflow_id")
            if isinstance(workflow_id, str) and workflow_id.strip():
                workflow_ids.add(workflow_id.strip())
        return workflow_ids

    def _missing_goal_evidence_finding(
            self,
            goal: GoalDefinition,
            linked_executions: list[Execution],
            *,
            policy: dict[str, Any],
    ) -> MainAgentMonitorFinding | None:
        completed_executions = [
            execution for execution in linked_executions if execution.status == ExecutionStatus.COMPLETED
        ]
        if not completed_executions:
            return None
        evaluation = GoalEvaluator.evaluate(goal)
        if evaluation.get("sufficient") is True:
            return None

        sorted_completions = sorted(
            completed_executions,
            key=lambda item: (item.updated_at, item.created_at, item.id),
            reverse=True,
        )
        latest_completion = sorted_completions[0]
        active_plan = self._goal_active_plan(goal)
        return MainAgentMonitorFinding(
            category="goal_missing_evidence",
            execution_id=latest_completion.id,
            workflow_id=latest_completion.workflow_id,
            status=goal.status.value,
            severity="medium",
            reason="Goal has completed execution attempts but lacks sufficient completion evidence.",
            evidence={
                "goal_id": goal.id,
                "objective": goal.objective,
                "priority": goal.priority,
                "owner_actor": goal.owner_actor,
                "goal_status": goal.status.value,
                "completed_execution_ids": [execution.id for execution in sorted_completions],
                "source_execution_ids": [execution.id for execution in sorted_completions],
                "latest_completed_execution_id": latest_completion.id,
                "success_criteria": list(goal.success_criteria),
                "evidence_count": len(goal.evidence),
                "evaluation_status": evaluation.get("status"),
                "evaluation_confidence": evaluation.get("confidence"),
                "missing_evidence": evaluation.get("missing_evidence", []),
                "active_plan_version": active_plan.get("version") if isinstance(active_plan, dict) else None,
                "recommended_action": "attach_or_request_completion_evidence",
                "anchor_type": "execution",
                "anchor_id": latest_completion.id,
                "intent": "evaluate_evidence",
                "supervision_policy": policy,
            },
        )

    def _repeated_goal_failure_finding(
            self,
            goal: GoalDefinition,
            linked_executions: list[Execution],
            *,
            policy: dict[str, Any],
    ) -> MainAgentMonitorFinding | None:
        failed_executions = [
            execution for execution in linked_executions if execution.status == ExecutionStatus.FAILED
        ]
        if len(failed_executions) < 2:
            return None
        sorted_failures = sorted(
            failed_executions,
            key=lambda item: (item.updated_at, item.created_at, item.id),
            reverse=True,
        )
        latest_failure = sorted_failures[0]
        active_plan = self._goal_active_plan(goal)
        failure_signatures = [
            {
                "execution_id": execution.id,
                "workflow_id": execution.workflow_id,
                "error": execution.error,
                "updated_at": execution.updated_at.isoformat(),
            }
            for execution in sorted_failures
        ]
        return MainAgentMonitorFinding(
            category="goal_repeated_failure",
            execution_id=latest_failure.id,
            workflow_id=latest_failure.workflow_id,
            status=goal.status.value,
            severity="high",
            reason="Goal has multiple failed execution attempts and needs supervisor replanning or escalation.",
            evidence={
                "goal_id": goal.id,
                "objective": goal.objective,
                "priority": goal.priority,
                "owner_actor": goal.owner_actor,
                "goal_status": goal.status.value,
                "failure_count": len(sorted_failures),
                "failed_execution_ids": [execution.id for execution in sorted_failures],
                "source_execution_ids": [execution.id for execution in sorted_failures],
                "latest_error": latest_failure.error,
                "failure_signatures": failure_signatures,
                "active_plan_version": active_plan.get("version") if isinstance(active_plan, dict) else None,
                "recommended_action": "request_replan",
                "anchor_type": "execution",
                "anchor_id": latest_failure.id,
                "intent": "replan_goal",
                "supervision_policy": policy,
            },
        )

    def _goal_active_plan(self, goal: GoalDefinition) -> dict[str, Any] | None:
        metadata = goal.metadata if isinstance(goal.metadata, dict) else {}
        planning = metadata.get("goal_planning")
        if isinstance(planning, dict) and isinstance(planning.get("active_plan"), dict):
            return planning["active_plan"]
        active_plan = metadata.get("active_plan")
        if isinstance(active_plan, dict):
            return active_plan
        return None

    def _goal_finding_anchor_execution(self, executions: list[Execution]) -> Execution | None:
        if not executions:
            return None
        return sorted(executions, key=lambda item: (item.updated_at, item.created_at, item.id), reverse=True)[0]

    @staticmethod
    def _goal_monitor_anchor_id(goal_id: str) -> str:
        return f"goal-monitor:{goal_id}"

    def _effective_monitoring_level(
            self,
            workflow: WorkflowDefinition | None,
            *,
            scheduled_workflow_ids: set[str],
            self_monitor_workflow_id: str | None,
    ) -> str:
        if workflow is None:
            return "off"
        level = self.workflow_monitoring_level(workflow)
        if level == "off":
            return "off"
        monitoring = workflow.metadata.get("main_agent_monitoring")
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        if workflow.id == self_monitor_workflow_id and monitoring.get("allow_self_monitoring") is not True:
            return "off"
        if workflow.id in scheduled_workflow_ids and "level" not in monitoring:
            return "strict"
        return level

    def _recent_terminal_attention_executions(
            self,
            settings: Settings,
            executions: list[Execution],
    ) -> list[Execution]:
        lookback_seconds = settings.main_agent_workflow_monitor_terminal_lookback_seconds
        if lookback_seconds <= 0:
            return []
        cutoff = utc_now().timestamp() - lookback_seconds
        recent: list[Execution] = []
        for execution in executions:
            if execution.status not in TERMINAL_ATTENTION_STATUS_SET:
                continue
            reference = execution.completed_at or execution.updated_at or execution.created_at
            if ensure_utc(reference).timestamp() >= cutoff:
                recent.append(execution)
        return recent

    def _stale_execution_finding(
            self,
            execution: Execution,
            *,
            workflow: WorkflowDefinition | None,
            settings: Settings,
    ) -> MainAgentMonitorFinding | None:
        if execution.status not in STALE_EXECUTION_STATUSES:
            return None
        runtime_policy = self._runtime_policy_for_execution(execution, workflow, settings)
        classification = classify_execution_staleness(
            execution,
            stale_after_seconds=settings.main_agent_workflow_monitor_stale_after_seconds,
            idle_timeout_seconds=runtime_policy.idle_timeout_seconds,
            run_timeout_seconds=runtime_policy.run_timeout_seconds,
        )
        if not classification["is_stale"]:
            return None
        stale_kind = classification.get("stale_kind")
        severity = "high" if execution.status in {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLING} else "medium"
        if stale_kind == "alive_but_idle":
            severity = "medium"
        return MainAgentMonitorFinding(
            category="stale_execution",
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            status=execution.status.value,
            severity=severity,
            reason=classification["reason"] or "Execution is stale.",
            evidence={
                "last_heartbeat_at": execution.last_heartbeat_at.isoformat() if execution.last_heartbeat_at else None,
                "updated_at": execution.updated_at.isoformat(),
                "started_at": execution.started_at.isoformat() if execution.started_at else None,
                "stale_after_seconds": settings.main_agent_workflow_monitor_stale_after_seconds,
                "idle_timeout_seconds": runtime_policy.idle_timeout_seconds,
                "run_timeout_seconds": runtime_policy.run_timeout_seconds,
                "timeout_policy_source": runtime_policy.source_map.get("run_timeout_seconds"),
                "runtime_policy": runtime_policy.model_dump(),
                "stale_kind": stale_kind,
                "age_seconds": classification["age_seconds"],
                "activity_age_seconds": classification["activity_age_seconds"],
                "runtime_seconds": classification["runtime_seconds"],
                "last_activity_at": classification["last_activity_at"],
                "last_recorded_activity_at": classification["last_recorded_activity_at"],
                "reference_at": classification["reference_at"],
            },
        )

    def _runtime_policy_for_execution(
            self,
            execution: Execution,
            workflow: WorkflowDefinition | None,
            settings: Settings,
    ) -> ResolvedExecutionRuntimePolicy:
        if workflow is None:
            return resolve_execution_runtime_policy(settings=settings, execution=execution)
        activity = execution.metadata.get("runtime_activity") if isinstance(execution.metadata, dict) else {}
        task = None
        agent = None
        agent_id = None
        if isinstance(activity, dict):
            task_id = activity.get("last_activity_task_id")
            agent_id = activity.get("last_activity_agent_id")
            task = next((item for item in workflow.task_definitions if item.id == task_id), None)
        if task is not None:
            agent_id = agent_id or task.agent_id
        if agent_id is not None:
            agent = next((item for item in workflow.agent_definitions if item.id == agent_id), None)
        return resolve_execution_runtime_policy(
            settings=settings,
            workflow=workflow,
            execution=execution,
            task=task,
            agent=agent,
        )

    def _terminal_attention_finding(self, execution: Execution) -> MainAgentMonitorFinding:
        if execution.status == ExecutionStatus.FAILED:
            category = "failed_execution"
            severity = "medium"
        elif execution.status == ExecutionStatus.CANCELLED:
            category = "cancelled_execution"
            severity = "low"
        else:
            category = "completed_execution"
            severity = "info"
        return MainAgentMonitorFinding(
            category=category,
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            status=execution.status.value,
            severity=severity,
            reason=f"Execution ended with status '{execution.status.value}'.",
            evidence={
                "error": execution.error,
                "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
                "updated_at": execution.updated_at.isoformat(),
            },
        )

    async def _governance_findings(
            self,
            execution: Execution,
            workflow: WorkflowDefinition,
    ) -> list[MainAgentMonitorFinding]:
        controls = self._supervision_controls(workflow)
        events = await self.context.execution_store.list_events(execution.id)
        referenced_source_event_ids = self._referenced_supervision_source_event_ids(events)
        candidates = [
            event
            for event in events
            if event.event_type in GOVERNANCE_EVENT_TYPES and event.id not in referenced_source_event_ids
        ]
        findings: list[MainAgentMonitorFinding] = []
        if controls["supervise_tool_failures"]:
            tool_failure_events = [
                event for event in candidates if event.event_type == ExecutionEventType.TOOL_CALL_FAILED
            ]
            if len(tool_failure_events) >= 3:
                findings.append(self._repeated_tool_failure_finding(execution, tool_failure_events))
                referenced_source_event_ids.update(event.id for event in tool_failure_events)
        if controls["supervise_subagents"]:
            for finding in await self._repeated_subagent_progress_findings(
                    execution,
                    workflow,
                    candidates,
                    events,
                    controls,
            ):
                findings.append(finding)
                referenced_source_event_ids.update(
                    str(item)
                    for item in finding.evidence.get("source_event_ids", [])
                    if item
                )

        for event in candidates:
            if event.id in referenced_source_event_ids:
                continue
            if event.event_type in {ExecutionEventType.TOKEN_BUDGET_WARNING, ExecutionEventType.TOKEN_BUDGET_EXCEEDED}:
                if not controls["supervise_token_usage"]:
                    continue
                finding = self._token_budget_finding(execution, event)
            elif event.event_type in {
                ExecutionEventType.CONTEXT_HEALTH_RECORDED,
                ExecutionEventType.CONTEXT_COMPACTION_FAILED,
            }:
                if not controls["supervise_context_health"]:
                    continue
                finding = self._context_governance_finding(execution, event)
            elif event.event_type in {
                ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
                ExecutionEventType.SUBAGENT_STEP_FAILED,
                ExecutionEventType.SUBAGENT_NEEDS_INPUT,
                ExecutionEventType.SUBAGENT_NEEDS_APPROVAL,
            }:
                if not controls["supervise_subagents"]:
                    continue
                if self._subagent_supervision_excluded(event, controls):
                    continue
                finding = self._subagent_governance_finding(execution, event)
            else:
                finding = None
            if finding is not None:
                findings.append(finding)
        return findings

    async def _repeated_subagent_progress_findings(
            self,
            execution: Execution,
            workflow: WorkflowDefinition,
            candidates: list[ExecutionEvent],
            events: list[ExecutionEvent],
            controls: dict[str, Any],
    ) -> list[MainAgentMonitorFinding]:
        progress_groups: dict[tuple[str, str], list[ExecutionEvent]] = {}
        for event in candidates:
            if event.event_type != ExecutionEventType.SUBAGENT_PROGRESS_UPDATED:
                continue
            if self._subagent_supervision_excluded(event, controls):
                continue
            key = self._subagent_progress_key(event)
            if key is None:
                continue
            progress_groups.setdefault(key, []).append(event)

        findings: list[MainAgentMonitorFinding] = []
        for key, progress_events in sorted(progress_groups.items()):
            ordered = sorted(progress_events, key=lambda item: (item.sequence, item.timestamp, item.id))
            if len(ordered) < 3:
                continue
            if self._has_subagent_completion_after_progress(key, ordered[0], events):
                continue
            task = self._workflow_task_by_id(workflow, key[1])
            finding_events = ordered[-5:]
            current_tasks = [
                str(event.payload.get("current_task") or "")
                for event in finding_events
                if event.payload.get("current_task")
            ]
            next_actions = [
                str(event.payload.get("next_action") or "")
                for event in finding_events
                if event.payload.get("next_action")
            ]
            dependencies = self._task_dependencies(workflow, task)
            prior_failures = self._prior_failure_events_for_subagent(
                key,
                before_event=finding_events[-1],
                events=events,
            )
            graph_context = await self._graph_context_for_repeated_subagent_progress(
                execution=execution,
                workflow=workflow,
                task=task,
                agent_id=key[0],
                finding_events=finding_events,
                dependencies=dependencies,
                prior_failures=prior_failures,
            )
            linked_decisions = (
                graph_context.get("decisions", []) if isinstance(graph_context, dict) else []
            )
            graph_failures = graph_context.get("failures", []) if isinstance(graph_context, dict) else []
            prior_failure_context = [*prior_failures, *graph_failures]
            evidence = {
                "source_event_ids": [event.id for event in finding_events],
                "source_event_type": ExecutionEventType.SUBAGENT_PROGRESS_UPDATED.value,
                "agent_id": key[0],
                "task_id": key[1],
                "anchor_type": "task",
                "anchor_id": key[1],
                "intent": "steer",
                "progress_update_count": len(ordered),
                "latest_status": finding_events[-1].payload.get("status") if finding_events else None,
                "latest_progress_percent": (
                    finding_events[-1].payload.get("progress_percent") if finding_events else None
                ),
                "current_tasks": current_tasks[-5:],
                "next_actions": next_actions[-5:],
                "expected_output": task.expected_output if task is not None else None,
                "dependencies": dependencies,
                "prior_failures": prior_failure_context,
                "linked_decisions": linked_decisions,
            }
            if isinstance(graph_context, dict):
                evidence["graph_context"] = {
                    "status": graph_context.get("status"),
                    "summary": graph_context.get("summary"),
                    "query_meta": graph_context.get("query_meta"),
                }
            findings.append(
                MainAgentMonitorFinding(
                    category="subagent_repeated_progress",
                    execution_id=execution.id,
                    workflow_id=execution.workflow_id,
                    status=execution.status.value,
                    severity="medium",
                    reason=(
                        f"Sub-agent reported {len(ordered)} progress updates without a completion signal."
                    ),
                    evidence=evidence,
                )
            )
        return findings

    def _subagent_progress_key(self, event: ExecutionEvent) -> tuple[str, str] | None:
        payload = event.payload or {}
        agent_id = event.agent_id or payload.get("agent_id") or payload.get("subagent_id") or payload.get(
            "sub_agent_id")
        task_id = event.task_id or payload.get("task_id") or payload.get("step_id")
        if not agent_id or not task_id:
            return None
        return str(agent_id), str(task_id)

    def _has_subagent_completion_after_progress(
            self,
            key: tuple[str, str],
            first_progress_event: ExecutionEvent,
            events: list[ExecutionEvent],
    ) -> bool:
        for event in events:
            if event.sequence < first_progress_event.sequence:
                continue
            if event.event_type != ExecutionEventType.SUBAGENT_STEP_COMPLETED:
                continue
            if self._subagent_progress_key(event) == key:
                return True
        return False

    def _workflow_task_by_id(
            self,
            workflow: WorkflowDefinition,
            task_id: str,
    ):
        for task in workflow.task_definitions:
            if task.id == task_id:
                return task
        return None

    def _task_dependencies(
            self,
            workflow: WorkflowDefinition,
            task: Any,
    ) -> list[dict[str, Any]]:
        if task is None:
            return []
        dependency_task_ids = set(str(item) for item in getattr(task, "depends_on_task_ids", []) if item)
        node_by_id = {node.id: node for node in workflow.nodes}
        task_node_ids = {node.id for node in workflow.nodes if node.task_id == task.id}
        for edge in workflow.edges:
            if edge.target_node_id not in task_node_ids:
                continue
            source = node_by_id.get(edge.source_node_id)
            if source is not None and source.task_id:
                dependency_task_ids.add(str(source.task_id))
        dependencies: list[dict[str, Any]] = []
        for dependency_id in sorted(dependency_task_ids):
            dependency = self._workflow_task_by_id(workflow, dependency_id)
            dependencies.append(
                {
                    "task_id": dependency_id,
                    "name": dependency.name if dependency is not None else None,
                    "description": dependency.description if dependency is not None else None,
                    "expected_output": dependency.expected_output if dependency is not None else None,
                }
            )
        return dependencies

    def _prior_failure_events_for_subagent(
            self,
            key: tuple[str, str],
            *,
            before_event: ExecutionEvent,
            events: list[ExecutionEvent],
    ) -> list[dict[str, Any]]:
        failure_types = {
            ExecutionEventType.AGENT_STEP_FAILED,
            ExecutionEventType.SUBAGENT_STEP_FAILED,
            ExecutionEventType.TOOL_CALL_FAILED,
        }
        failures: list[dict[str, Any]] = []
        for event in sorted(events, key=lambda item: (item.sequence, item.timestamp, item.id), reverse=True):
            if event.sequence > before_event.sequence:
                continue
            if event.event_type not in failure_types:
                continue
            if self._subagent_progress_key(event) != key:
                continue
            failures.append(
                {
                    "event_id": event.id,
                    "event_type": event.event_type.value,
                    "task_id": event.task_id or event.payload.get("task_id"),
                    "agent_id": event.agent_id or event.payload.get("agent_id"),
                    "tool_call_id": event.tool_call_id,
                    "error": event.payload.get("error") or event.payload.get("blocker"),
                    "status": event.status or event.payload.get("status"),
                }
            )
            if len(failures) >= 5:
                break
        return failures

    async def _graph_context_for_repeated_subagent_progress(
            self,
            *,
            execution: Execution,
            workflow: WorkflowDefinition,
            task: Any,
            agent_id: str,
            finding_events: list[ExecutionEvent],
            dependencies: list[dict[str, Any]],
            prior_failures: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        settings = get_settings()
        if not (
                settings.agency_graph_context_tools_enabled
                and settings.graph_context_auto_retrieval_enabled
                and settings.graph_context_subagent_steering_enabled
        ):
            return None
        task_id = task.id if task is not None else (finding_events[-1].task_id if finding_events else None)
        if not task_id:
            return None
        agent = self._workflow_agent_by_id(workflow, agent_id)
        if agent is not None:
            graph_settings = agent.graph_context
            if not (
                    graph_settings.enabled
                    and graph_settings.auto_retrieval_enabled is not False
                    and graph_settings.subagent_steering_enabled is not False
            ):
                return None
            include_memories = graph_settings.include_memories
            include_events = graph_settings.include_events
            limit = max(min(int(graph_settings.max_records or 20), 50), 1)
        else:
            include_memories = True
            include_events = True
            limit = 20
        request = {
            "intent": "steer",
            "anchor_type": "task" if task is not None else "step_run",
            "anchor_id": task_id if task is not None else f"{execution.id}:{task_id}",
            "scope": {
                "workflow_id": workflow.id,
                "workflow_name": workflow.name,
                "execution_id": execution.id,
                "run_id": execution.id,
                "task_id": task_id,
                "task_name": task.name if task is not None else None,
                "agent_id": agent_id,
                "trigger": "subagent_repeated_progress",
                "source_event_ids": [event.id for event in finding_events],
                "expected_output": task.expected_output if task is not None else None,
                "dependencies": dependencies,
                "prior_failure_event_ids": [failure["event_id"] for failure in prior_failures],
            },
            "include_memories": include_memories,
            "include_events": include_events,
            "include_raw_graph": False,
            "budget": "brief",
            "limit": limit,
        }
        from app.services.agency_graph_context import AgencyGraphContextService

        return await AgencyGraphContextService(self.context).build_context(request)

    def _workflow_agent_by_id(self, workflow: WorkflowDefinition, agent_id: str):
        for agent in workflow.agent_definitions:
            if agent.id == agent_id:
                return agent
        return None

    def _referenced_supervision_source_event_ids(self, events: list[ExecutionEvent]) -> set[str]:
        referenced: set[str] = set()
        for event in events:
            if event.event_type not in {
                ExecutionEventType.MONITOR_FINDING_CREATED,
                ExecutionEventType.SUPERVISOR_STEERING_REQUESTED,
                ExecutionEventType.SUPERVISOR_STEERING_APPLIED,
            }:
                continue
            referenced.update(self._source_event_ids_from_evidence(event.payload.get("evidence")))
        return referenced

    def _source_event_ids_from_evidence(self, evidence: Any) -> set[str]:
        if not isinstance(evidence, dict):
            return set()
        event_ids: set[str] = set()
        source_event_id = evidence.get("source_event_id")
        if isinstance(source_event_id, str):
            event_ids.add(source_event_id)
        source_event_ids = evidence.get("source_event_ids")
        if isinstance(source_event_ids, list):
            event_ids.update(str(item) for item in source_event_ids if item)
        return event_ids

    def _token_budget_finding(self, execution: Execution, event: ExecutionEvent) -> MainAgentMonitorFinding | None:
        budget = event.payload.get("budget") if isinstance(event.payload.get("budget"), dict) else {}
        scope = budget.get("scope") or event.payload.get("scope") or "run"
        used_tokens = budget.get("used_tokens")
        budget_tokens = budget.get("budget_tokens")
        exceeded = event.event_type == ExecutionEventType.TOKEN_BUDGET_EXCEEDED
        return MainAgentMonitorFinding(
            category="token_budget_exceeded" if exceeded else "token_budget_warning",
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            status=execution.status.value,
            severity="critical" if exceeded else "medium",
            reason=(
                f"Token budget {'exceeded' if exceeded else 'warning'} for {scope}: "
                f"{used_tokens}/{budget_tokens} tokens."
            ),
            evidence={
                "source_event_id": event.id,
                "source_event_type": event.event_type.value,
                "budget": budget,
                "metrics": event.metrics,
            },
        )

    def _context_governance_finding(
            self,
            execution: Execution,
            event: ExecutionEvent,
    ) -> MainAgentMonitorFinding | None:
        if event.event_type == ExecutionEventType.CONTEXT_COMPACTION_FAILED:
            return MainAgentMonitorFinding(
                category="context_compaction_failure",
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                status=execution.status.value,
                severity="high",
                reason=f"Context compaction failed: {event.payload.get('error') or 'unknown error'}.",
                evidence={
                    "source_event_id": event.id,
                    "source_event_type": event.event_type.value,
                    "error": event.payload.get("error"),
                    "metrics": event.metrics,
                },
            )
        status = str(event.payload.get("status") or event.metrics.get("context_status") or "").lower()
        if status not in {"critical", "overflow"}:
            return None
        return MainAgentMonitorFinding(
            category="context_overflow" if status == "overflow" else "context_critical",
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            status=execution.status.value,
            severity="critical" if status == "overflow" else "high",
            reason=(
                f"Context health is {status}: "
                f"{event.payload.get('estimated_total_context_tokens') or event.metrics.get('estimated_total_context_tokens') or 0}/"
                f"{event.payload.get('context_window') or event.metrics.get('context_window') or 'unknown'} tokens."
            ),
            evidence={
                "source_event_id": event.id,
                "source_event_type": event.event_type.value,
                "context_status": status,
                "payload": event.payload,
                "metrics": event.metrics,
            },
        )

    def _subagent_governance_finding(
            self,
            execution: Execution,
            event: ExecutionEvent,
    ) -> MainAgentMonitorFinding | None:
        payload = event.payload or {}
        evidence = {
            "source_event_id": event.id,
            "source_event_type": event.event_type.value,
            "agent_id": event.agent_id,
            "task_id": event.task_id,
            "payload": payload,
        }
        if event.event_type == ExecutionEventType.SUBAGENT_STEP_FAILED:
            return MainAgentMonitorFinding(
                category="subagent_step_failed",
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                status=execution.status.value,
                severity="high",
                reason=f"Sub-agent step failed: {payload.get('error') or payload.get('blocker') or 'unknown error'}.",
                evidence=evidence,
            )
        if event.event_type == ExecutionEventType.SUBAGENT_NEEDS_INPUT:
            return MainAgentMonitorFinding(
                category="subagent_needs_input",
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                status=execution.status.value,
                severity="medium",
                reason=f"Sub-agent needs input: {payload.get('clarification_needed') or payload.get('question') or 'input required'}.",
                evidence=evidence,
            )
        if event.event_type == ExecutionEventType.SUBAGENT_NEEDS_APPROVAL:
            return MainAgentMonitorFinding(
                category="subagent_needs_approval",
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                status=execution.status.value,
                severity="medium",
                reason=f"Sub-agent needs approval: {payload.get('approval_type') or payload.get('reason') or 'approval required'}.",
                evidence=evidence,
            )
        if payload.get("blocker"):
            return MainAgentMonitorFinding(
                category="subagent_blocked",
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                status=execution.status.value,
                severity="high",
                reason=f"Sub-agent is blocked: {payload.get('blocker')}.",
                evidence=evidence,
            )
        if payload.get("clarification_needed"):
            return MainAgentMonitorFinding(
                category="subagent_needs_input",
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                status=execution.status.value,
                severity="medium",
                reason=f"Sub-agent needs clarification: {payload.get('clarification_needed')}.",
                evidence=evidence,
            )
        subagent_status = str(payload.get("status") or payload.get("subagent_status") or "").lower()
        if subagent_status in {"off_track", "off-track", "stuck", "degraded"}:
            return MainAgentMonitorFinding(
                category="subagent_off_track",
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                status=execution.status.value,
                severity="high",
                reason=f"Sub-agent reported {subagent_status} status.",
                evidence=evidence,
            )
        context_health = payload.get("context_health") if isinstance(payload.get("context_health"), dict) else {}
        context_status = str(context_health.get("status") or "").lower()
        if context_status in {"critical", "overflow"}:
            return MainAgentMonitorFinding(
                category="subagent_context_degraded",
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                status=execution.status.value,
                severity="critical" if context_status == "overflow" else "high",
                reason=f"Sub-agent context health is {context_status}.",
                evidence=evidence,
            )
        confidence = payload.get("confidence")
        if isinstance(confidence, int | float) and confidence <= 0.35:
            return MainAgentMonitorFinding(
                category="subagent_low_confidence",
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                status=execution.status.value,
                severity="high" if confidence <= 0.2 else "medium",
                reason=f"Sub-agent confidence is low: {confidence}.",
                evidence=evidence,
            )
        return None

    def _repeated_tool_failure_finding(
            self,
            execution: Execution,
            events: list[ExecutionEvent],
    ) -> MainAgentMonitorFinding:
        recent = events[-5:]
        tool_names = [
            str(event.payload.get("tool_name") or event.payload.get("tool_id") or event.payload.get("tool") or "tool")
            for event in recent
        ]
        return MainAgentMonitorFinding(
            category="repeated_tool_call_failure",
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            status=execution.status.value,
            severity="high",
            reason=f"Execution has {len(events)} unaddressed tool call failures.",
            evidence={
                "source_event_ids": [event.id for event in events],
                "source_event_type": ExecutionEventType.TOOL_CALL_FAILED.value,
                "tool_names": tool_names,
                "last_error": recent[-1].payload.get("error") if recent else None,
            },
        )

    def _supervision_controls(self, workflow: WorkflowDefinition) -> dict[str, Any]:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        allowed_actions = monitoring.get("allowed_steering_actions")
        explicit_allowed_actions = isinstance(allowed_actions, list)
        if isinstance(allowed_actions, list):
            allowed = sorted(str(item) for item in allowed_actions if str(item) in SUPERVISION_ALLOWED_ACTIONS)
        else:
            allowed = sorted(SUPERVISION_ALLOWED_ACTIONS)
        auto_apply_actions = monitoring.get("auto_apply_steering_actions")
        if isinstance(auto_apply_actions, list):
            auto_apply = sorted(
                str(item)
                for item in auto_apply_actions
                if str(item) in SUPERVISION_AUTO_APPLY_ACTIONS and str(item) in allowed
            )
        else:
            auto_apply = []
        return {
            "supervise_token_usage": monitoring.get("supervise_token_usage") is not False,
            "supervise_context_health": monitoring.get("supervise_context_health") is not False,
            "supervise_subagents": monitoring.get("supervise_subagents") is not False,
            "supervise_tool_failures": monitoring.get("supervise_tool_failures") is not False,
            "delegate_hitl_to_main_agent": monitoring.get("delegate_hitl_to_main_agent") is True,
            "excluded_subagent_ids": self._string_list(monitoring.get("excluded_subagent_ids")),
            "excluded_task_ids": self._string_list(monitoring.get("excluded_task_ids")),
            "allowed_steering_actions": allowed,
            "explicit_allowed_steering_actions": explicit_allowed_actions,
            "auto_apply_steering_actions": auto_apply,
        }

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return sorted({str(item).strip() for item in value if str(item).strip()})

    def _subagent_supervision_excluded(self, event: ExecutionEvent, controls: dict[str, Any]) -> bool:
        payload = event.payload or {}
        agent_candidates = {
            str(item)
            for item in (
                event.agent_id,
                payload.get("agent_id"),
                payload.get("subagent_id"),
                payload.get("sub_agent_id"),
            )
            if item
        }
        task_candidates = {
            str(item)
            for item in (
                event.task_id,
                payload.get("task_id"),
                payload.get("step_id"),
            )
            if item
        }
        excluded_agents = set(controls.get("excluded_subagent_ids") or [])
        excluded_tasks = set(controls.get("excluded_task_ids") or [])
        return bool(agent_candidates.intersection(excluded_agents) or task_candidates.intersection(excluded_tasks))

    def _mark_seen(self, finding: MainAgentMonitorFinding) -> bool:
        key = self._finding_key(
            category=finding.category,
            workflow_id=finding.workflow_id,
            execution_id=finding.execution_id,
            status=finding.status,
            evidence=finding.evidence,
        )
        if key in self._seen_finding_keys:
            return False
        self._seen_finding_keys.add(key)
        return True

    async def _persisted_finding_keys(self, executions: list[Execution]) -> set[str]:
        keys: set[str] = set()
        for execution in executions:
            for event in await self.context.execution_store.list_events(execution.id):
                if event.event_type != ExecutionEventType.MONITOR_FINDING_CREATED:
                    continue
                payload = event.payload if isinstance(event.payload, dict) else {}
                evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
                keys.add(
                    self._finding_key(
                        category=str(payload.get("category") or ""),
                        workflow_id=str(payload.get("workflow_id") or event.workflow_id or ""),
                        execution_id=str(payload.get("execution_id") or event.execution_id),
                        status=str(payload.get("status") or ""),
                        evidence=evidence,
                    )
                )
        return keys

    def _persisted_goal_finding_keys(self, goals: list[GoalDefinition]) -> set[str]:
        keys: set[str] = set()
        for goal in goals:
            monitoring = goal.metadata.get("main_agent_monitoring") if isinstance(goal.metadata, dict) else None
            if not isinstance(monitoring, dict):
                continue
            findings = monitoring.get("findings")
            if not isinstance(findings, list):
                continue
            for item in findings:
                if not isinstance(item, dict):
                    continue
                dedupe_key = item.get("dedupe_key")
                if isinstance(dedupe_key, str) and dedupe_key:
                    keys.add(dedupe_key)
                    continue
                payload = item.get("finding") if isinstance(item.get("finding"), dict) else item
                evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
                keys.add(
                    self._finding_key(
                        category=str(payload.get("category") or ""),
                        workflow_id=str(payload.get("workflow_id") or ""),
                        execution_id=str(payload.get("execution_id") or self._goal_monitor_anchor_id(goal.id)),
                        status=str(payload.get("status") or ""),
                        evidence=evidence,
                    )
                )
        return keys

    def _finding_key(
            self,
            *,
            category: str,
            workflow_id: str,
            execution_id: str,
            status: str,
            evidence: dict[str, Any],
    ) -> str:
        source_event_ids = evidence.get("source_event_ids")
        source_execution_ids = evidence.get("source_execution_ids")
        source = evidence.get("source_event_id") or (
            ",".join(sorted(str(item) for item in source_event_ids if item))
            if isinstance(source_event_ids, list)
            else ""
        ) or (
                     ",".join(sorted(str(item) for item in source_execution_ids if item))
                     if isinstance(source_execution_ids, list)
                     else ""
                 ) or evidence.get("source_finding_key") or evidence.get("goal_id") or evidence.get("anchor_id") or ""
        return f"{category}:{workflow_id}:{execution_id}:{status}:{source}"

    async def _record_finding(self, finding: MainAgentMonitorFinding) -> ExecutionEvent | None:
        payload = self._redacted_monitor_finding_payload(finding)
        dedupe_key = self._finding_key(
            category=finding.category,
            workflow_id=finding.workflow_id,
            execution_id=finding.execution_id,
            status=finding.status,
            evidence=finding.evidence,
        )
        if await self._finding_already_persisted(finding.execution_id, dedupe_key):
            return None
        event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id=finding.execution_id,
                workflow_id=finding.workflow_id,
                event_type=ExecutionEventType.MONITOR_FINDING_CREATED,
                actor_type="system",
                actor_id="main_agent_monitor",
                payload_json=payload,
                metadata={
                    "source": "main_agent_monitor",
                    "category": finding.category,
                    "severity": finding.severity,
                    "dedupe_key": dedupe_key,
                },
            )
        )
        self.context.runtime_operations.increment("main_agent_monitor.findings")
        self.context.runtime_operations.increment(f"main_agent_monitor.findings.{finding.category}")
        self.context.runtime_operations.record_action("main_agent_monitor.finding", **payload)
        logger.info(
            "Main-agent workflow monitor finding: %s",
            payload,
        )
        return event

    async def _record_goal_finding(self, finding: MainAgentMonitorFinding) -> ExecutionEvent | dict[str, Any] | None:
        goal_id = str(finding.evidence.get("goal_id") or "")
        if not goal_id:
            return None
        payload = self._redacted_monitor_finding_payload(finding)
        dedupe_key = self._finding_key(
            category=finding.category,
            workflow_id=finding.workflow_id,
            execution_id=finding.execution_id,
            status=finding.status,
            evidence=finding.evidence,
        )

        if not finding.execution_id.startswith("goal-monitor:"):
            event = await self._record_finding(finding)
            if event is None:
                return None
            await self._append_goal_finding_record(
                goal_id,
                payload=payload,
                dedupe_key=dedupe_key,
                execution_event_id=event.id,
            )
            self.context.runtime_operations.increment("main_agent_monitor.goal_findings")
            return event

        goal = await self.context.goal_repo.get(goal_id)
        if goal is None:
            return None
        monitoring = dict(goal.metadata.get("main_agent_monitoring") or {})
        existing_findings = [
            item for item in monitoring.get("findings", []) if isinstance(item, dict)
        ]
        if any(item.get("dedupe_key") == dedupe_key for item in existing_findings):
            return None

        record = {
            "source": "main_agent_monitor",
            "dedupe_key": dedupe_key,
            "recorded_at": utc_now().isoformat(),
            "finding": payload,
        }
        monitoring["findings"] = [*existing_findings, record]
        metadata = dict(goal.metadata)
        metadata["main_agent_monitoring"] = monitoring
        await self.context.goal_repo.save(goal.model_copy(update={"metadata": metadata, "updated_at": utc_now()}))
        self.context.runtime_operations.increment("main_agent_monitor.findings")
        self.context.runtime_operations.increment(f"main_agent_monitor.findings.{finding.category}")
        self.context.runtime_operations.increment("main_agent_monitor.goal_findings")
        self.context.runtime_operations.record_action("main_agent_monitor.goal_finding", **payload)
        logger.info("Main-agent goal monitor finding: %s", payload)
        return record

    async def _append_goal_finding_record(
            self,
            goal_id: str,
            *,
            payload: dict[str, Any],
            dedupe_key: str,
            execution_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        goal = await self.context.goal_repo.get(goal_id)
        if goal is None:
            return None
        monitoring = dict(goal.metadata.get("main_agent_monitoring") or {})
        existing_findings = [item for item in monitoring.get("findings", []) if isinstance(item, dict)]
        if any(item.get("dedupe_key") == dedupe_key for item in existing_findings):
            return None
        record = {
            "source": "main_agent_monitor",
            "dedupe_key": dedupe_key,
            "execution_event_id": execution_event_id,
            "recorded_at": utc_now().isoformat(),
            "finding": payload,
        }
        monitoring["findings"] = [*existing_findings, record][-100:]
        metadata = dict(goal.metadata)
        metadata["main_agent_monitoring"] = monitoring
        await self.context.goal_repo.save(goal.model_copy(update={"metadata": metadata, "updated_at": utc_now()}))
        return record

    async def _maybe_create_goal_supervisor_approval_request(
            self,
            *,
            finding: MainAgentMonitorFinding,
            finding_record: ExecutionEvent | dict[str, Any],
    ) -> ApprovalRequest | None:
        goal_id = str(finding.evidence.get("goal_id") or "")
        recommended_action = str(finding.evidence.get("recommended_action") or "").strip()
        if not goal_id or not recommended_action:
            return None
        policy = finding.evidence.get("supervision_policy") if isinstance(finding.evidence, dict) else None
        policy = policy if isinstance(policy, dict) else {}
        policy_decision = self._goal_supervisor_policy_decision(policy, recommended_action)
        if not policy_decision["requires_approval"]:
            return None

        goal = await self.context.goal_repo.get(goal_id)
        if goal is None:
            return None
        conversation_id = self._goal_approval_conversation_id(goal)
        if conversation_id is None:
            return None
        conversation = await self.context.conversation_repo.get(conversation_id)
        if conversation is None:
            return None

        finding_key = self._goal_finding_record_key(finding, finding_record)
        approval_noise_key = f"{goal.id}:{finding.category}:{recommended_action}:{finding_key}"
        existing = await self._pending_goal_supervisor_approval(approval_noise_key)
        if existing is not None:
            self.context.runtime_operations.record_action(
                "main_agent_monitor.goal_approval_request_deduped",
                goal_id=goal.id,
                approval_request_id=existing.id,
                proposal_noise_key=approval_noise_key,
            )
            return None

        profile = await self._active_main_agent_profile()
        redacted_finding = self._redacted_monitor_finding_payload(finding)
        origin_message = await self._create_monitor_conversation_message(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.SYSTEM_NOTE,
                plain_text=f"Main-agent supervisor requested approval for goal '{goal.objective}'.",
                content={
                    "source": "main_agent_monitor",
                    "goal_id": goal.id,
                    "finding": redacted_finding,
                    "recommended_action": recommended_action,
                    "policy": policy,
                },
                metadata={"source": "main_agent_monitor", "goal_id": goal.id},
            )
        )
        approval = ApprovalRequest(
            approval_type=ApprovalType.OTHER,
            target_type=ApprovalTargetType.OTHER,
            target_id=goal.id,
            requested_by_agent_id=getattr(profile, "agent_id", None) or "main_agent_monitor",
            requested_by_profile_id=getattr(profile, "id", None),
            conversation_id=conversation_id,
            origin_message_id=origin_message.id,
            summary=f"Supervisor approval required for goal action: {recommended_action}",
            diff_summary=self._redact_monitor_reason(finding.reason),
            proposed_payload={
                "goal_id": goal.id,
                "goal_objective": goal.objective,
                "recommended_action": recommended_action,
                "finding": redacted_finding,
                "policy": policy,
                "policy_decision": policy_decision,
                "risk": policy_decision["risk"],
                "requires_approval": True,
            },
            metadata={
                "action": "goal_supervisor_action",
                "source": "main_agent_monitor",
                "proposal_kind": "goal_supervisor_action",
                "requires_human_permission": True,
                "goal_id": goal.id,
                "recommended_action": recommended_action,
                "finding_category": finding.category,
                "finding_key": finding_key,
                "proposal_noise_key": approval_noise_key,
                "policy": policy,
                "policy_decision": policy_decision,
                "risk": policy_decision["risk"],
            },
        )
        created = await self.context.conversation_approval_repo.create(approval)
        approval_message = await self._create_monitor_conversation_message(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.SYSTEM_NOTE,
                plain_text=created.summary,
                approval_request_id=created.id,
                content={
                    "approval_request_id": created.id,
                    "approval_type": created.approval_type.value,
                    "goal_id": goal.id,
                    "recommended_action": recommended_action,
                    "finding": redacted_finding,
                },
                metadata={
                    "source": "main_agent_monitor",
                    "goal_id": goal.id,
                    "approval_request_id": created.id,
                },
            )
        )
        await self._maybe_deliver_monitor_message(
            conversation=conversation,
            message=approval_message,
            approval_request=created,
        )
        await self._link_goal_supervisor_approval(goal.id, created, finding_key=finding_key)
        await self._record_goal_supervisor_decision(
            goal_id=goal.id,
            action=recommended_action,
            finding=finding,
            finding_key=finding_key,
            approval_request_id=created.id,
            policy=policy,
            risk=policy_decision["risk"],
            rationale=self._redact_monitor_reason(finding.reason),
            policy_decision=policy_decision,
        )
        await self._record_goal_supervisor_action(
            goal_id=goal.id,
            action="request_human_approval",
            finding=finding,
            finding_record=finding_record,
            status="pending_approval",
            result={
                "approval_request_id": created.id,
                "recommended_action": recommended_action,
            },
            approval_request_id=created.id,
        )
        self.context.runtime_operations.increment("main_agent_monitor.approval_requests")
        self.context.runtime_operations.increment("main_agent_monitor.goal_approval_requests")
        self.context.runtime_operations.record_action(
            "main_agent_monitor.goal_approval_request",
            approval_request_id=created.id,
            goal_id=goal.id,
            recommended_action=recommended_action,
        )
        return created

    async def _record_goal_supervisor_action(
            self,
            *,
            goal_id: str,
            action: str,
            finding: MainAgentMonitorFinding,
            finding_record: ExecutionEvent | dict[str, Any],
            status: str,
            result: dict[str, Any] | None = None,
            approval_request_id: str | None = None,
            policy_decision: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not goal_id:
            return None
        goal = await self.context.goal_repo.get(goal_id)
        if goal is None:
            return None
        monitoring = dict(goal.metadata.get("main_agent_monitoring") or {})
        actions = [item for item in monitoring.get("supervisor_actions", []) if isinstance(item, dict)]
        finding_key = self._goal_finding_record_key(finding, finding_record)
        policy = finding.evidence.get("supervision_policy") if isinstance(finding.evidence, dict) else {}
        policy = policy if isinstance(policy, dict) else {}
        decision = policy_decision or self._goal_supervisor_policy_decision(policy, action)
        record = {
            "id": f"action-{len(actions) + 1}",
            "goal_id": goal.id,
            "source": "main_agent_monitor",
            "recorded_at": utc_now().isoformat(),
            "actor": "main_agent_monitor",
            "action": action,
            "status": status,
            "automatic": True,
            "risk": decision["risk"],
            "finding_key": finding_key,
            "finding_category": finding.category,
            "approval_request_id": approval_request_id,
            "policy": policy,
            "policy_decision": decision,
            "requires_approval": decision["requires_approval"],
            "allowed_by_policy": decision["allowed"],
            "result": result or {},
        }
        actions.append(record)
        monitoring["supervisor_actions"] = actions[-100:]
        monitoring["last_supervisor_action"] = record
        metadata = dict(goal.metadata)
        metadata["main_agent_monitoring"] = monitoring
        await self.context.goal_repo.save(goal.model_copy(update={"metadata": metadata, "updated_at": utc_now()}))
        self.context.runtime_operations.increment("main_agent_monitor.goal_supervisor_actions")
        telemetry_record = dict(record)
        telemetry_record["supervisor_action"] = telemetry_record.pop("action")
        self.context.runtime_operations.record_action("main_agent_monitor.goal_supervisor_action", **telemetry_record)
        return record

    async def _record_goal_supervisor_decision(
            self,
            *,
            goal_id: str,
            action: str,
            finding: MainAgentMonitorFinding,
            finding_key: str,
            approval_request_id: str | None,
            policy: dict[str, Any],
            risk: str,
            rationale: str,
            policy_decision: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        goal = await self.context.goal_repo.get(goal_id)
        if goal is None:
            return None
        monitoring = dict(goal.metadata.get("main_agent_monitoring") or {})
        decisions = [item for item in monitoring.get("supervisor_decisions", []) if isinstance(item, dict)]
        decision = policy_decision or self._goal_supervisor_policy_decision(policy, action)
        record = {
            "id": f"decision-{len(decisions) + 1}",
            "goal_id": goal.id,
            "source": "main_agent_monitor",
            "recorded_at": utc_now().isoformat(),
            "actor": "main_agent_monitor",
            "decision": {
                "action": action,
                "finding_category": finding.category,
                "approval_request_id": approval_request_id,
                "requires_approval": decision["requires_approval"],
            },
            "risk": risk,
            "policy": policy,
            "policy_decision": decision,
            "requires_approval": decision["requires_approval"],
            "allowed_by_policy": decision["allowed"],
            "approval_request_id": approval_request_id,
            "finding_id": finding_key,
            "action": action,
            "rationale": rationale,
        }
        decisions.append(record)
        monitoring["supervisor_decisions"] = decisions[-100:]
        monitoring["last_supervisor_decision"] = record
        metadata = dict(goal.metadata)
        metadata["main_agent_monitoring"] = monitoring
        updated_goal = await self.context.goal_repo.save(
            goal.model_copy(update={"metadata": metadata, "updated_at": utc_now()})
        )
        await self._append_goal_supervisor_decision_audit_event(updated_goal, record)
        self.context.runtime_operations.increment("main_agent_monitor.goal_supervisor_decisions")
        telemetry_record = dict(record)
        telemetry_record["supervisor_action"] = telemetry_record.pop("action")
        self.context.runtime_operations.record_action("main_agent_monitor.goal_supervisor_decision", **telemetry_record)
        return record

    async def _append_goal_supervisor_decision_audit_event(
            self,
            goal: GoalDefinition,
            record: dict[str, Any],
    ) -> None:
        if not getattr(self._settings(), "graph_projection_enabled", True):
            return
        repo = getattr(self.context, "graph_projection_event_repo", None)
        if repo is None:
            return
        try:
            await repo.append(
                GraphProjectionEvent(
                    event_type="goal.supervisor_decision.audit_recorded",
                    aggregate_type="goal",
                    aggregate_id=goal.id,
                    user_id=goal.owner_actor,
                    source="main_agent_monitor",
                    payload={
                        "goal": {
                            "id": goal.id,
                            "objective": goal.objective,
                            "status": goal.status.value,
                            "priority": goal.priority,
                            "owner_actor": goal.owner_actor,
                        },
                        "supervisor_decision": record,
                        "audit": {
                            "autonomous": True,
                            "source": "main_agent_monitor",
                            "actor": record.get("actor"),
                            "action": record.get("action"),
                            "risk": record.get("risk"),
                            "allowed_by_policy": record.get("allowed_by_policy"),
                            "requires_approval": record.get("requires_approval"),
                            "approval_request_id": record.get("approval_request_id"),
                            "finding_id": record.get("finding_id"),
                            "recorded_at": record.get("recorded_at"),
                        },
                        "relationships": {
                            "supervisor_decision_ids": [record["id"]] if record.get("id") else [],
                            "approval_request_ids": (
                                [record["approval_request_id"]] if record.get("approval_request_id") else []
                            ),
                            "supervisor_finding_ids": [record["finding_id"]] if record.get("finding_id") else [],
                            "execution_ids": list(goal.execution_ids),
                        },
                    },
                )
            )
        except Exception:
            logger.exception("Failed to append goal supervisor decision audit event")

    def _goal_supervisor_policy_decision(self, policy: dict[str, Any], action: str) -> dict[str, Any]:
        automatic_actions = {
            str(item)
            for item in policy.get("automatic_actions", [])
            if str(item).strip()
        }
        approval_required_actions = {
            str(item)
            for item in policy.get("approval_required_actions", [])
            if str(item).strip()
        }
        requires_approval = action in approval_required_actions
        allowed = action in automatic_actions or requires_approval
        reason = (
            "Action is explicitly allowed for automatic supervisor execution."
            if action in automatic_actions
            else "Action requires human approval before execution."
            if requires_approval
            else "Action is not listed as automatic or approval-routed by the goal supervision policy."
        )
        return {
            "action": action,
            "allowed": allowed,
            "automatic": action in automatic_actions,
            "requires_approval": requires_approval,
            "risk": self._goal_supervisor_action_risk(action),
            "reason": reason,
        }

    def _goal_approval_conversation_id(self, goal: GoalDefinition) -> str | None:
        constraints = goal.constraints if isinstance(goal.constraints, dict) else {}
        monitoring = goal.metadata.get("main_agent_monitoring") if isinstance(goal.metadata, dict) else None
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        for source_mapping in (monitoring, constraints):
            value = source_mapping.get("approval_conversation_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
            approval_policy = source_mapping.get("approval_policy")
            if isinstance(approval_policy, dict):
                for key in ("approval_conversation_id", "conversation_id"):
                    value = approval_policy.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return None

    def _goal_finding_record_key(
            self,
            finding: MainAgentMonitorFinding,
            finding_record: ExecutionEvent | dict[str, Any],
    ) -> str:
        if isinstance(finding_record, ExecutionEvent):
            return finding_record.id
        if isinstance(finding_record, dict):
            value = finding_record.get("dedupe_key")
            if isinstance(value, str) and value:
                return value
        return self._finding_key(
            category=finding.category,
            workflow_id=finding.workflow_id,
            execution_id=finding.execution_id,
            status=finding.status,
            evidence=finding.evidence,
        )

    async def _pending_goal_supervisor_approval(self, approval_noise_key: str) -> ApprovalRequest | None:
        for approval in await self.context.conversation_approval_repo.list():
            if approval.status != ApprovalStatus.PENDING:
                continue
            metadata = approval.metadata if isinstance(approval.metadata, dict) else {}
            if metadata.get("source") != "main_agent_monitor":
                continue
            if metadata.get("proposal_kind") != "goal_supervisor_action":
                continue
            if metadata.get("proposal_noise_key") == approval_noise_key:
                return approval
        return None

    async def _link_goal_supervisor_approval(
            self,
            goal_id: str,
            approval: ApprovalRequest,
            *,
            finding_key: str,
    ) -> None:
        goal = await self.context.goal_repo.get(goal_id)
        if goal is None:
            return
        metadata = dict(goal.metadata)
        monitoring = dict(metadata.get("main_agent_monitoring") or {})
        approvals = [item for item in monitoring.get("approval_requests", []) if isinstance(item, dict)]
        record = {
            "approval_request_id": approval.id,
            "status": approval.status.value,
            "recommended_action": approval.metadata.get("recommended_action"),
            "finding_key": finding_key,
            "recorded_at": utc_now().isoformat(),
        }
        monitoring["approval_requests"] = [*approvals, record][-50:]
        monitoring["last_approval_request"] = record
        metadata["main_agent_monitoring"] = monitoring
        await self.context.goal_repo.save(goal.model_copy(update={"metadata": metadata, "updated_at": utc_now()}))

    @staticmethod
    def _goal_supervisor_action_risk(action: str) -> str:
        if action in {
            "workflow_definition_mutation",
            "tool_definition_mutation",
            "shell_side_effect",
            "external_write",
            "purchase",
            "delete",
            "physical_world_action",
            "high_priority_or_user_created_goal_cancellation",
        }:
            return "high"
        return "medium"

    async def _finding_already_persisted(self, execution_id: str, dedupe_key: str) -> bool:
        for event in await self.context.execution_store.list_events(execution_id):
            if event.event_type != ExecutionEventType.MONITOR_FINDING_CREATED:
                continue
            if event.metadata.get("dedupe_key") == dedupe_key:
                return True
            payload = event.payload if isinstance(event.payload, dict) else {}
            evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
            existing_key = self._finding_key(
                category=str(payload.get("category") or ""),
                workflow_id=str(payload.get("workflow_id") or event.workflow_id or ""),
                execution_id=str(payload.get("execution_id") or event.execution_id),
                status=str(payload.get("status") or ""),
                evidence=evidence,
            )
            if existing_key == dedupe_key:
                return True
        return False

    async def _maybe_record_supervisor_steering_request(
            self,
            *,
            finding: MainAgentMonitorFinding,
            workflow: WorkflowDefinition,
            finding_event_id: str,
    ) -> dict[str, Any] | None:
        action = STEERING_ACTION_BY_FINDING_CATEGORY.get(finding.category)
        if action is None:
            return None
        controls = self._supervision_controls(workflow)
        allowed_actions = set(controls["allowed_steering_actions"])
        if action not in allowed_actions:
            return None
        if action in SUPERVISION_AUTO_APPLY_ACTIONS and not controls["explicit_allowed_steering_actions"]:
            return None
        requires_human_approval = self._steering_action_requires_human_approval(action, controls)
        delegated_hitl = controls["delegate_hitl_to_main_agent"] and action == "request_human_review"
        decision = SupervisorSteeringDecision(
            execution_id=finding.execution_id,
            workflow_id=finding.workflow_id,
            finding_event_id=finding_event_id,
            category=finding.category,
            severity=finding.severity,
            recommended_action=action,
            reason=finding.reason,
            confidence=self._steering_confidence(finding),
            evidence=finding.evidence,
            policy={
                "source": "main_agent_monitor",
                "allowed_steering_actions": sorted(allowed_actions),
                "auto_apply_steering_actions": controls["auto_apply_steering_actions"],
                "delegate_hitl_to_main_agent": controls["delegate_hitl_to_main_agent"],
                "decision_actor": "main_agent" if delegated_hitl else "human",
                "delegated_hitl": delegated_hitl,
                "mutating_action_applied": action in controls["auto_apply_steering_actions"],
                "requires_human_approval": requires_human_approval,
            },
        )
        request_payload = await self._record_supervisor_steering_request(decision)
        if action in controls["auto_apply_steering_actions"]:
            request_payload["applied"] = await self._apply_supervisor_steering(
                decision,
                steering_request_event_id=str(request_payload["event_id"]),
            )
        return request_payload

    def _steering_confidence(self, finding: MainAgentMonitorFinding) -> str:
        if finding.severity in {"critical", "high"}:
            return "high"
        if finding.category in {"subagent_low_confidence", "token_budget_warning"}:
            return "medium"
        return "medium"

    def _steering_action_requires_human_approval(self, action: str, controls: dict[str, Any]) -> bool:
        if controls.get("delegate_hitl_to_main_agent") is True and action == "request_human_review":
            return False
        return action not in SUPERVISION_AUTO_APPLY_ACTIONS and action in {
            "redirect_subagent",
            "replace_task_instructions",
            "request_replan",
            "request_human_review",
            "lower_max_iterations",
            "reduce_tool_scope",
        }

    async def _record_supervisor_steering_request(self, decision: SupervisorSteeringDecision) -> dict[str, Any]:
        payload = asdict(decision)
        event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id=decision.execution_id,
                workflow_id=decision.workflow_id,
                event_type=ExecutionEventType.SUPERVISOR_STEERING_REQUESTED,
                actor_type="system",
                actor_id="main_agent_monitor",
                payload_json=payload,
                metadata={
                    "source": "main_agent_monitor",
                    "finding_event_id": decision.finding_event_id,
                    "category": decision.category,
                    "severity": decision.severity,
                    "recommended_action": decision.recommended_action,
                },
            )
        )
        await self._persist_pending_supervisor_intervention(decision, event)
        payload["event_id"] = event.id
        self.context.runtime_operations.increment("main_agent_monitor.steering_requests")
        self.context.runtime_operations.increment(
            f"main_agent_monitor.steering_requests.{decision.recommended_action}"
        )
        self.context.runtime_operations.record_action("main_agent_monitor.steering_request", **payload)
        logger.info("Main-agent supervisor steering request: %s", payload)
        return payload

    async def _apply_supervisor_steering(
            self,
            decision: SupervisorSteeringDecision,
            *,
            steering_request_event_id: str,
    ) -> dict[str, Any]:
        action = decision.recommended_action
        payload = {
            **asdict(decision),
            "steering_request_event_id": steering_request_event_id,
            "applied_action": action,
            "status": "applied",
        }
        try:
            if action == "pause_execution":
                result = (await self.context.control_plane.pause(decision.execution_id)).model_dump(mode="json")
            elif action == "resume_execution":
                result = (await self.context.control_plane.resume(decision.execution_id)).model_dump(mode="json")
            elif action == "cancel_execution":
                result = (await self.context.control_plane.cancel(decision.execution_id)).model_dump(mode="json")
            elif action == "repair_stale_execution":
                result = {
                    "items": await self.context.control_plane.repair_stale_executions(
                        workflow_id=decision.workflow_id,
                        execution_id=decision.execution_id,
                    )
                }
            else:
                result = {"skipped": True, "reason": "unsupported_auto_apply_action"}
                payload["status"] = "skipped"
            payload["result"] = result
        except Exception as exc:
            logger.exception("Main-agent supervisor steering application failed")
            payload["status"] = "failed"
            payload["error"] = str(exc)

        event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id=decision.execution_id,
                workflow_id=decision.workflow_id,
                event_type=ExecutionEventType.SUPERVISOR_STEERING_APPLIED,
                actor_type="system",
                actor_id="main_agent_monitor",
                payload_json=payload,
                metadata={
                    "source": "main_agent_monitor",
                    "finding_event_id": decision.finding_event_id,
                    "steering_request_event_id": steering_request_event_id,
                    "category": decision.category,
                    "severity": decision.severity,
                    "applied_action": action,
                    "status": payload["status"],
                },
            )
        )
        await self._persist_applied_supervisor_intervention(decision, event, payload)
        payload["event_id"] = event.id
        self.context.runtime_operations.increment("main_agent_monitor.steering_applied")
        self.context.runtime_operations.increment(f"main_agent_monitor.steering_applied.{action}")
        self.context.runtime_operations.record_action("main_agent_monitor.steering_applied", **payload)
        logger.info("Main-agent supervisor steering applied: %s", payload)
        return payload

    async def _persist_pending_supervisor_intervention(
            self,
            decision: SupervisorSteeringDecision,
            event: ExecutionEvent,
    ) -> None:
        execution = await self.context.execution_store.get_execution(decision.execution_id)
        if execution is None:
            return
        metadata = dict(execution.metadata)
        runtime_governance = dict(metadata.get("runtime_governance") or {})
        supervision = dict(runtime_governance.get("supervision") or {})
        pending = list(supervision.get("pending_requests") or [])
        pending.append(
            {
                **asdict(decision),
                "event_id": event.id,
                "requested_at": event.timestamp.isoformat(),
            }
        )
        supervision["pending_requests"] = pending[-50:]
        supervision["last_steering_request_event_id"] = event.id
        supervision["last_updated_at"] = event.timestamp.isoformat()
        runtime_governance["supervision"] = supervision
        metadata["runtime_governance"] = runtime_governance
        execution.metadata = metadata
        execution.updated_at = utc_now()
        await self.context.execution_store.update_execution(execution)

    async def _persist_applied_supervisor_intervention(
            self,
            decision: SupervisorSteeringDecision,
            event: ExecutionEvent,
            payload: dict[str, Any],
    ) -> None:
        execution = await self.context.execution_store.get_execution(decision.execution_id)
        if execution is None:
            return
        metadata = dict(execution.metadata)
        runtime_governance = dict(metadata.get("runtime_governance") or {})
        supervision = dict(runtime_governance.get("supervision") or {})
        pending = list(supervision.get("pending_requests") or [])
        for request in pending:
            if not isinstance(request, dict):
                continue
            if request.get("event_id") == payload.get("steering_request_event_id"):
                request["status"] = payload["status"]
                request["applied_action"] = payload["applied_action"]
                request["applied_event_id"] = event.id
                request["applied_at"] = event.timestamp.isoformat()
                request["result"] = payload.get("result")
                if payload.get("error"):
                    request["error"] = payload["error"]
        supervision["pending_requests"] = pending[-50:]
        supervision["last_steering_applied_event_id"] = event.id
        supervision["last_updated_at"] = event.timestamp.isoformat()
        runtime_governance["supervision"] = supervision
        metadata["runtime_governance"] = runtime_governance
        execution.metadata = metadata
        execution.updated_at = utc_now()
        await self.context.execution_store.update_execution(execution)

    async def _maybe_record_evaluation_review(
            self,
            *,
            finding: MainAgentMonitorFinding,
            workflow: WorkflowDefinition,
            execution: Execution,
            finding_event_id: str,
            quality_signals: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self._workflow_should_request_evaluation_review(workflow, finding):
            return None
        evaluation_agent = await self._evaluation_agent()
        if evaluation_agent is None:
            return {
                "status": "skipped",
                "reason": "evaluation_agent_unavailable",
                "workflow_id": workflow.id,
                "execution_id": execution.id,
                "finding_event_id": finding_event_id,
            }
        safety = self._evaluation_agent_safety(evaluation_agent)
        if not safety["safe"]:
            return {
                "status": "skipped",
                "reason": "evaluation_agent_not_read_only",
                "workflow_id": workflow.id,
                "execution_id": execution.id,
                "finding_event_id": finding_event_id,
                "judge_agent_id": evaluation_agent.id,
                "safety": safety,
            }

        verdict = self._evaluation_verdict(
            finding=finding,
            execution=execution,
            quality_signals=quality_signals,
        )
        deterministic_comparison = self._compare_evaluation_with_deterministic_evidence(
            verdict=verdict,
            finding=finding,
            execution=execution,
            quality_signals=quality_signals,
        )
        payload = {
            "status": "recorded",
            "workflow_id": workflow.id,
            "execution_id": execution.id,
            "finding_event_id": finding_event_id,
            "judge_agent_id": evaluation_agent.id,
            "judge_agent_name": evaluation_agent.name,
            "advisory": True,
            "read_only": True,
            "rubric": self._evaluation_rubric(finding),
            "verdict": verdict,
            "deterministic_comparison": deterministic_comparison,
            "quality_signals": quality_signals,
        }
        event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id=execution.id,
                workflow_id=workflow.id,
                agent_id=evaluation_agent.id,
                event_type=ExecutionEventType.MONITOR_EVALUATION_RECORDED,
                actor_type="agent",
                actor_id=evaluation_agent.name,
                payload_json=payload,
                metadata={
                    "source": "main_agent_monitor",
                    "advisory": True,
                    "judge_agent_id": evaluation_agent.id,
                    "deterministic_alignment": deterministic_comparison["alignment"],
                },
            )
        )
        payload["event_id"] = event.id
        self.context.runtime_operations.increment("main_agent_monitor.evaluation_reviews")
        self.context.runtime_operations.increment(
            f"main_agent_monitor.evaluation_reviews.{deterministic_comparison['alignment']}"
        )
        self.context.runtime_operations.record_action("main_agent_monitor.evaluation_review", **payload)
        logger.info("Main-agent workflow monitor Evaluation-agent review: %s", payload)
        return payload

    def _workflow_should_request_evaluation_review(
            self,
            workflow: WorkflowDefinition,
            finding: MainAgentMonitorFinding,
    ) -> bool:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        if monitoring.get("allow_evaluation_agent_review") is False:
            return False
        if self.workflow_monitoring_level(workflow) == "strict":
            return True
        if monitoring.get("allow_evaluation_agent_review") is True:
            return True
        if finding.category in {"failed_execution", "stale_execution"}:
            return True
        if self._sensitive_workflow_review_reasons(workflow):
            return True
        return self._workflow_has_monitor_change_history(workflow)

    def _workflow_has_monitor_change_history(self, workflow: WorkflowDefinition) -> bool:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        if not isinstance(monitoring, dict):
            return False
        history = monitoring.get("improvement_proposals")
        return isinstance(history, list) and bool(history)

    async def _evaluation_agent(self) -> AgentDefinition | None:
        for agent in await self.context.agent_repo.list(include_deleted=True):
            name = agent.name.strip().lower()
            kind = str(
                agent.framework_hints.metadata.get("agent_kind")
                or agent.metadata.get("agent_kind")
                or ""
            ).strip().lower()
            role = str(
                agent.framework_hints.metadata.get("runtime_role") or agent.metadata.get("runtime_role") or ""
            ).strip().lower()
            if name == "evaluation" or kind in {"evaluation", "eval_judge"} or role == "eval_judge":
                return agent
        return None

    def _evaluation_agent_safety(self, agent: AgentDefinition) -> dict[str, Any]:
        tool_ids = set(agent.tool_ids)
        unsafe_tool_ids = sorted(tool_ids.difference(EVALUATION_AGENT_READ_ONLY_TOOL_IDS))
        memory_enabled = bool(agent.memory.enabled)
        return {
            "safe": not unsafe_tool_ids and not memory_enabled,
            "allowed_tool_ids": sorted(EVALUATION_AGENT_READ_ONLY_TOOL_IDS),
            "assigned_tool_ids": sorted(tool_ids),
            "unsafe_tool_ids": unsafe_tool_ids,
            "memory_enabled": memory_enabled,
        }

    def _evaluation_rubric(self, finding: MainAgentMonitorFinding) -> dict[str, Any]:
        return {
            "criteria": [
                "Use only execution, event, artifact, workflow, and provided monitor evidence.",
                "Prefer deterministic execution status, errors, artifacts, validation output, and approval events.",
                "Treat the verdict as advisory and require human approval for workflow mutations.",
            ],
            "finding_category": finding.category,
        }

    def _evaluation_verdict(
            self,
            *,
            finding: MainAgentMonitorFinding,
            execution: Execution,
            quality_signals: dict[str, Any],
    ) -> dict[str, Any]:
        deterministic_status = self._deterministic_evaluation_status(
            finding=finding,
            execution=execution,
            quality_signals=quality_signals,
        )
        if deterministic_status == "failed":
            score = 35
            passed = False
            confidence = "high" if execution.status == ExecutionStatus.FAILED else "medium"
            failed_criteria = ["execution_health"]
        elif deterministic_status == "needs_review":
            score = 55
            passed = False
            confidence = "medium"
            failed_criteria = ["evidence_completeness"]
        else:
            score = 90
            passed = True
            confidence = "medium"
            failed_criteria = []
        return {
            "score": score,
            "max_score": 100,
            "passed": passed,
            "confidence": confidence,
            "summary": self._evaluation_summary(deterministic_status, finding),
            "reasons": self._evaluation_reasons(deterministic_status, finding, quality_signals),
            "failed_criteria": failed_criteria,
            "evidence": [
                {
                    "source": "execution",
                    "id": execution.id,
                    "note": f"Execution status is {execution.status.value}.",
                },
                {
                    "source": "event",
                    "id": finding.execution_id,
                    "note": finding.reason,
                },
            ],
            "needs_human_review": deterministic_status != "passed" or finding.severity in {"high", "critical"},
        }

    def _deterministic_evaluation_status(
            self,
            *,
            finding: MainAgentMonitorFinding,
            execution: Execution,
            quality_signals: dict[str, Any],
    ) -> str:
        if finding.category in {"failed_execution", "stale_execution"}:
            return "failed"
        if execution.status == ExecutionStatus.CANCELLED:
            return "needs_review"
        if quality_signals.get("missing_artifact_count") or quality_signals.get("missing_validation_output_count"):
            return "needs_review"
        return "passed"

    def _evaluation_summary(self, deterministic_status: str, finding: MainAgentMonitorFinding) -> str:
        if deterministic_status == "failed":
            return f"Evaluation agrees the workflow needs attention for {finding.category}."
        if deterministic_status == "needs_review":
            return "Evaluation found incomplete evidence and recommends human review."
        return "Evaluation found no deterministic blocker in the inspected evidence."

    def _evaluation_reasons(
            self,
            deterministic_status: str,
            finding: MainAgentMonitorFinding,
            quality_signals: dict[str, Any],
    ) -> list[str]:
        reasons = [finding.reason]
        if deterministic_status == "needs_review":
            if quality_signals.get("missing_artifact_count"):
                reasons.append("Recent successful runs have missing artifacts.")
            if quality_signals.get("missing_validation_output_count"):
                reasons.append("Recent successful runs have missing validation output.")
        return reasons

    def _compare_evaluation_with_deterministic_evidence(
            self,
            *,
            verdict: dict[str, Any],
            finding: MainAgentMonitorFinding,
            execution: Execution,
            quality_signals: dict[str, Any],
    ) -> dict[str, Any]:
        deterministic_status = self._deterministic_evaluation_status(
            finding=finding,
            execution=execution,
            quality_signals=quality_signals,
        )
        deterministic_failed = deterministic_status in {"failed", "needs_review"}
        judge_failed = verdict.get("passed") is not True
        alignment = "aligned" if deterministic_failed == judge_failed else "conflict"
        return {
            "alignment": alignment,
            "deterministic_status": deterministic_status,
            "judge_passed": verdict.get("passed"),
            "proposal_allowed": True,
            "proposal_note": (
                "Judge verdict is advisory; deterministic monitor evidence remains the source of truth for proposals."
            ),
        }

    def _proposal_advisory_evidence(self, evaluation_review: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not evaluation_review:
            return []
        comparison = evaluation_review.get("deterministic_comparison")
        verdict = evaluation_review.get("verdict")
        return [
            {
                "source": "evaluation_agent",
                "event_id": evaluation_review.get("event_id"),
                "judge_agent_id": evaluation_review.get("judge_agent_id"),
                "advisory": True,
                "summary": verdict.get("summary") if isinstance(verdict, dict) else None,
                "deterministic_alignment": comparison.get("alignment") if isinstance(comparison, dict) else None,
                "deterministic_status": comparison.get("deterministic_status")
                if isinstance(comparison, dict)
                else None,
            }
        ]

    def _proposal_for_finding(
            self,
            finding: MainAgentMonitorFinding,
            workflow: WorkflowDefinition | None,
            finding_event_id: str,
            quality_signals: dict[str, Any],
            evaluation_review: dict[str, Any] | None = None,
    ) -> MainAgentWorkflowImprovementProposal | None:
        if workflow is None or not self._workflow_allows_improvement_proposals(workflow):
            return None
        evidence = [
            {
                "execution_id": finding.execution_id,
                "event_id": finding_event_id,
                "summary": finding.reason,
                "failure_text": finding.evidence.get("error"),
                "artifact_ids": finding.evidence.get("artifact_ids", []),
            }
        ]
        advisory_evidence = self._proposal_advisory_evidence(evaluation_review)
        if finding.category == "stale_execution":
            runtime_schedule_recommendations = self._runtime_schedule_recommendations()
            memory_recommendations = self._memory_recommendations()
            restart_active_executions = True
            proposed_change = {
                "type": "runtime_stale_repair_review",
                "summary": "Review stale-run handling and decide whether active executions should be replaced.",
                "affected_fields": [
                    "runtime.stale_execution_repair_policy",
                    "workflow.metadata.main_agent_monitoring",
                ],
                "runtime_schedule_recommendations": runtime_schedule_recommendations,
                "memory_recommendations": memory_recommendations,
            }
            proposed_change["schedule_change_approval"] = self._schedule_change_approval_requirements()
            proposed_change["review_requirements"] = self._strong_review_requirements(
                workflow,
                proposed_change,
                restart_active_executions=restart_active_executions,
            )
            expected_benefit = (
                "Future stale runs can be surfaced with a clear repair decision instead of remaining indefinitely active."
            )
            risk = "Medium; replacing active executions can discard in-flight work and should require human approval."
            validation_plan = (
                "Run or inspect the workflow again after approval and verify stale executions transition or get replaced."
            )
            rollback_plan = "Leave the current workflow revision unchanged and do not replace active executions."
            return MainAgentWorkflowImprovementProposal(
                workflow_id=finding.workflow_id,
                diagnosis=self._diagnosis_payload(
                    category="stale_execution",
                    severity=finding.severity,
                    confidence="high",
                    evidence=evidence,
                    proposed_change=proposed_change,
                    expected_benefit=expected_benefit,
                    risk=risk,
                    rollback_plan=rollback_plan,
                    validation_plan=validation_plan,
                    quality_signals=quality_signals,
                    advisory_evidence=advisory_evidence,
                ),
                quality_signals=quality_signals,
                finding={
                    "category": "stale_execution",
                    "severity": finding.severity,
                    "confidence": "high",
                    "evidence": evidence,
                    "advisory_evidence": advisory_evidence,
                },
                proposed_change=proposed_change,
                expected_benefit=expected_benefit,
                risk=risk,
                validation_plan=validation_plan,
                rollback_plan=rollback_plan,
                restart_active_executions=restart_active_executions,
                evaluation_review=evaluation_review,
            )
        if finding.category == "failed_execution":
            recommendations = self._prompt_task_recommendations()
            graph_task_recommendations = self._graph_task_recommendations()
            agent_recommendations = self._agent_recommendations()
            memory_recommendations = self._memory_recommendations()
            tool_recommendations = self._tool_recommendations()
            validation_recommendations = self._validation_recommendations()
            restart_active_executions = False
            proposed_change = {
                "type": "task_instruction_update",
                "summary": "Clarify task instructions, success criteria, expected output, escalation, tool-use boundaries, and evidence requirements.",
                "affected_fields": [
                    "task_definitions[*].instructions",
                    "task_definitions[*].expected_output",
                ],
                "recommendations": recommendations,
                "graph_task_recommendations": graph_task_recommendations,
                "agent_recommendations": agent_recommendations,
                "memory_recommendations": memory_recommendations,
                "tool_recommendations": tool_recommendations,
                "validation_recommendations": validation_recommendations,
            }
            proposed_change["tool_assignment_change_approval"] = self._tool_assignment_change_approval_requirements()
            proposed_change["memory_write_approval"] = self._memory_write_approval_requirements()
            proposed_change["review_requirements"] = self._strong_review_requirements(
                workflow,
                proposed_change,
                restart_active_executions=restart_active_executions,
            )
            expected_benefit = (
                "Future runs should be easier to execute, validate, and escalate when tools or assumptions fail."
            )
            risk = "Low; the proposal is limited to task instructions and requires approval before any workflow change."
            validation_plan = "Run the workflow once after approval and verify the final output includes validation evidence."
            rollback_plan = f"Restore workflow revision {workflow.versioning.revision}."
            category = "tool_failure" if finding.evidence.get("error") else "repeated_failure"
            return MainAgentWorkflowImprovementProposal(
                workflow_id=finding.workflow_id,
                diagnosis=self._diagnosis_payload(
                    category=category,
                    severity=finding.severity,
                    confidence="medium",
                    evidence=evidence,
                    proposed_change=proposed_change,
                    expected_benefit=expected_benefit,
                    risk=risk,
                    rollback_plan=rollback_plan,
                    validation_plan=validation_plan,
                    quality_signals=quality_signals,
                    advisory_evidence=advisory_evidence,
                ),
                quality_signals=quality_signals,
                finding={
                    "category": category,
                    "severity": finding.severity,
                    "confidence": "medium",
                    "evidence": evidence,
                    "advisory_evidence": advisory_evidence,
                },
                proposed_change=proposed_change,
                expected_benefit=expected_benefit,
                risk=risk,
                validation_plan=validation_plan,
                rollback_plan=rollback_plan,
                restart_active_executions=restart_active_executions,
                evaluation_review=evaluation_review,
            )
        return None

    def _prompt_task_recommendations(self) -> list[dict[str, str]]:
        return [
            {
                "kind": "clarify_ambiguous_instructions",
                "summary": "Clarify the exact task objective, inputs, constraints, and done condition.",
            },
            {
                "kind": "add_success_criteria",
                "summary": "State concrete success criteria that the task output must satisfy.",
            },
            {
                "kind": "add_expected_output_shape",
                "summary": "Specify the expected output shape, including artifact paths or structured fields when applicable.",
            },
            {
                "kind": "add_escalation_instructions",
                "summary": "Escalate with the failing step, blocker, and recovery path when the task cannot complete.",
            },
            {
                "kind": "tighten_tool_use_boundaries",
                "summary": "Name allowed tool actions and require approval or escalation for unsafe mutations.",
            },
            {
                "kind": "add_evidence_requirements",
                "summary": "Require validation evidence, produced artifact references, and unresolved failure details.",
            },
        ]

    def _graph_task_recommendations(self) -> list[dict[str, str]]:
        return [
            {
                "kind": "split_overloaded_tasks",
                "summary": "Split tasks that mix planning, execution, validation, and reporting into smaller focused tasks.",
            },
            {
                "kind": "merge_redundant_tasks",
                "summary": "Merge tasks with overlapping responsibilities when they produce duplicate work or unclear handoffs.",
            },
            {
                "kind": "add_verification_tasks",
                "summary": "Add explicit verification tasks for required artifacts, schemas, commands, or acceptance checks.",
            },
            {
                "kind": "add_recovery_tasks",
                "summary": "Add recovery tasks that gather failure evidence and choose a retry, fallback, or escalation path.",
            },
            {
                "kind": "add_human_approval_points",
                "summary": "Add human approval points before high-impact mutations, external sends, credential use, or destructive actions.",
            },
            {
                "kind": "reorder_dependency_tasks",
                "summary": "Reorder dependent tasks when execution evidence shows prerequisites are missing or failing late.",
            },
        ]

    def _agent_recommendations(self) -> list[dict[str, str]]:
        return [
            {
                "kind": "adjust_agent_responsibility",
                "summary": "Clarify which agent owns diagnosis, execution, validation, escalation, and final reporting.",
            },
            {
                "kind": "reduce_agent_overlap",
                "summary": "Reduce overlapping agent responsibilities when failures show duplicate work or unclear handoffs.",
            },
            {
                "kind": "switch_model_profile_strength",
                "summary": "Use a stronger model profile for tasks that need deeper reasoning, planning, code changes, or review.",
            },
            {
                "kind": "add_or_remove_tools",
                "summary": "Add missing tools needed for the workflow or remove tools that create unnecessary risk or confusion.",
            },
            {
                "kind": "tighten_agent_failure_reporting",
                "summary": "Require agents to report the failing step, attempted recovery, evidence, and escalation need.",
            },
        ]

    def _runtime_schedule_recommendations(self) -> list[dict[str, str]]:
        return [
            {
                "kind": "adjust_timeout",
                "summary": "Review task and execution timeouts when stale or slow runs show work regularly exceeds limits.",
            },
            {
                "kind": "adjust_schedule_cadence",
                "summary": "Change schedule cadence when runs overlap, start too frequently, or wait too long for timely monitoring.",
            },
            {
                "kind": "adjust_max_concurrency",
                "summary": "Tune max concurrency to prevent duplicate in-flight runs or unnecessary queue buildup.",
            },
            {
                "kind": "review_execution_host",
                "summary": "Move the workflow to a more appropriate execution host when capacity, permissions, or environment drift causes failures.",
            },
            {
                "kind": "review_runtime_adapter",
                "summary": "Switch or repair the runtime adapter when execution evidence points to adapter-specific instability.",
            },
            {
                "kind": "improve_stale_run_handling",
                "summary": "Define how stale queued, running, paused, and cancelling executions should be repaired or escalated.",
            },
            {
                "kind": "decide_restart_active_executions",
                "summary": "Explicitly decide whether approved runtime changes should replace active executions or leave them untouched.",
            },
        ]

    def _schedule_change_approval_requirements(self) -> dict[str, Any]:
        return {
            "approval_required": True,
            "approval_type": "schedule_update",
            "split_from_workflow_approval": True,
            "affected_fields": [
                "schedule.trigger_config.cron",
                "schedule.trigger_config.interval_seconds",
                "schedule.max_concurrent_executions",
                "schedule.runtime_adapter_override",
                "schedule.execution_host",
            ],
            "reason": (
                "Schedule cadence, concurrency, runtime adapter, and execution host changes must be approved "
                "separately before the monitor or main agent can apply them."
            ),
        }

    def _tool_assignment_change_approval_requirements(self) -> dict[str, Any]:
        return {
            "approval_required": True,
            "approval_type": "workflow_update",
            "split_from_instruction_approval": True,
            "affected_fields": [
                "task_definitions[*].tool_ids",
                "agent_definitions[*].tool_ids",
                "tool_definitions[*].security",
                "tool_definitions[*].credential_references",
            ],
            "restricted_capabilities": [
                "mutation",
                "shell",
                "browser",
                "filesystem",
                "network",
                "mcp",
                "credential_access",
            ],
            "reason": (
                "Tool assignment changes that add mutating, shell, browser, filesystem, network, MCP, or credential "
                "access must be reviewed and approved separately before application."
            ),
        }

    def _memory_write_approval_requirements(self) -> dict[str, Any]:
        return {
            "approval_required": True,
            "approval_type": "workflow_update",
            "split_from_instruction_approval": True,
            "affected_fields": [
                "workflow.metadata.main_agent_monitoring.store_run_summaries",
                "workflow.metadata.main_agent_monitoring.store_failure_summaries",
                "workflow.metadata.main_agent_monitoring.safe_to_summarize",
                "workflow.metadata.persistent_run_summary.enabled",
            ],
            "reason": (
                "Durable workflow memory writes must remain disabled until a human approves summary persistence "
                "for a workflow that did not already allow it."
            ),
        }

    def _memory_recommendations(self) -> list[dict[str, str]]:
        return [
            {
                "kind": "create_or_update_workflow_scoped_memories",
                "summary": "Capture stable lessons as workflow-scoped memory when repeated execution evidence supports them.",
            },
            {
                "kind": "suppress_duplicate_memories",
                "summary": "Avoid creating duplicate memories when an existing workflow memory already captures the same lesson.",
            },
            {
                "kind": "mark_stale_memories",
                "summary": "Mark workflow memories stale when later runs disprove the lesson or make the guidance obsolete.",
            },
        ]

    def _tool_recommendations(self) -> list[dict[str, str]]:
        return [
            {
                "kind": "propose_missing_tool_contracts",
                "summary": "Identify missing tool contracts when workflow evidence shows agents cannot perform a required action.",
            },
            {
                "kind": "improve_tool_schemas",
                "summary": "Tighten tool input and output schemas when failures show ambiguous arguments or unverifiable results.",
            },
            {
                "kind": "add_approval_gates",
                "summary": "Add approval gates before tools that mutate external state, send messages, use credentials, or write code.",
            },
            {
                "kind": "narrow_tool_permissions",
                "summary": "Restrict tool permissions when a workflow only needs a safer subset of available actions.",
            },
            {
                "kind": "flag_flaky_tools_for_repair",
                "summary": "Flag tools for repair when repeated failures indicate instability, missing retries, or weak error reporting.",
            },
        ]

    def _validation_recommendations(self) -> list[dict[str, str]]:
        return [
            {
                "kind": "require_deterministic_checks",
                "summary": "Require deterministic validation checks that can be repeated after the proposed workflow change.",
            },
            {
                "kind": "require_artifact_assertions",
                "summary": "Assert required artifacts by path, id, or metadata instead of relying only on prose completion.",
            },
            {
                "kind": "require_schema_checks",
                "summary": "Validate structured outputs against schemas when the workflow expects machine-readable results.",
            },
            {
                "kind": "require_command_evidence",
                "summary": "Attach command, test, or inspection evidence when validation depends on runtime behavior.",
            },
            {
                "kind": "request_evaluation_agent_review",
                "summary": "Route higher-risk fixes through an Evaluation-agent review before marking the workflow healthy.",
            },
        ]

    def _strong_review_requirements(
            self,
            workflow: WorkflowDefinition,
            proposed_change: dict[str, Any],
            *,
            restart_active_executions: bool,
    ) -> dict[str, Any]:
        reasons = self._sensitive_workflow_review_reasons(workflow)
        if restart_active_executions:
            reasons["approval_boundaries"] = "The proposal can replace active executions after approval."
        monitoring = workflow.metadata.get("main_agent_monitoring")
        if isinstance(monitoring, dict) and monitoring.get("strong_review_required") is True:
            reasons["policy_override"] = "Workflow metadata explicitly requires stronger monitor review."
        review_steps = [
            "Review execution evidence and proposed diff.",
            "Confirm rollback and validation plan.",
        ]
        if reasons:
            review_steps.extend(
                [
                    "Get an explicit human decision from a workflow owner or admin.",
                    "Verify sensitive tools, credentials, approval boundaries, and external effects before approval.",
                ]
            )
        return {
            "required": bool(reasons),
            "level": "strong" if reasons else "standard",
            "reasons": [{"category": category, "summary": summary} for category, summary in sorted(reasons.items())],
            "review_steps": review_steps,
            "affected_fields": proposed_change.get("affected_fields", []),
        }

    def _sensitive_workflow_review_reasons(self, workflow: WorkflowDefinition) -> dict[str, str]:
        reasons: dict[str, str] = {}
        for task in workflow.task_definitions:
            task_text = " ".join(
                str(value or "")
                for value in (
                    task.name,
                    task.description,
                    task.instructions,
                    task.expected_output,
                    task.metadata,
                    task.tool_ids,
                )
            ).lower()
            if task.human_approval_required:
                reasons["approval_boundaries"] = "A workflow task already requires human approval."
            self._add_sensitive_text_reasons(reasons, task_text)
        for node in workflow.nodes:
            node_text = " ".join(
                str(value or "") for value in (node.name, node.node_type.value, node.config, node.metadata)).lower()
            if node.node_type.value == "approval":
                reasons["approval_boundaries"] = "The workflow graph contains approval nodes."
            self._add_sensitive_text_reasons(reasons, node_text)
        for tool in workflow.tool_definitions:
            tool_text = " ".join(
                str(value or "")
                for value in (
                    tool.id,
                    tool.name,
                    tool.display_name,
                    tool.description,
                    tool.tags,
                    tool.tool_type.value,
                    tool.implementation.model_dump(mode="json"),
                    tool.security.model_dump(mode="json"),
                )
            ).lower()
            security = tool.security
            if security.dangerous:
                reasons["destructive_tools"] = "The workflow uses tools marked dangerous."
            if security.credential_references:
                reasons["credentials"] = "The workflow uses credential-backed tools."
            if security.allow_network or security.allow_browser or tool.tool_type.value in {"http_request", "mcp_tool",
                                                                                            "a2a_remote_agent"}:
                reasons["external_channels"] = "The workflow uses tools that can reach external systems."
            if security.allow_shell or security.allow_filesystem or tool.tool_type.value == "shell_command":
                reasons["code_writing_tasks"] = "The workflow uses shell or filesystem-capable tools."
            if security.requires_approval or tool.tool_type.value == "human_approval":
                reasons["approval_boundaries"] = "The workflow uses approval-gated tools."
            self._add_sensitive_text_reasons(reasons, tool_text)
        return reasons

    def _add_sensitive_text_reasons(self, reasons: dict[str, str], text: str) -> None:
        if any(token in text for token in ("delete", "destroy", "drop ", "truncate", "remove production", "wipe")):
            reasons["destructive_tools"] = "Workflow text references destructive operations."
        if any(token in text for token in
               ("email", "slack", "telegram", "webhook", "external", "send message", "post to")):
            reasons["external_channels"] = "Workflow text references external communication channels."
        if any(token in text for token in ("credential", "secret", "token", "password", "api key")):
            reasons["credentials"] = "Workflow text references credentials or secrets."
        if any(token in text for token in
               ("write code", "edit file", "commit", "pull request", "repository", "deploy")):
            reasons["code_writing_tasks"] = "Workflow text references code-writing, repository, or deployment work."
        if any(token in text for token in
               ("approval boundary", "approval gate", "human approval", "requires approval")):
            reasons["approval_boundaries"] = "Workflow text references approval boundaries."

    def _diagnosis_payload(
            self,
            *,
            category: str,
            severity: str,
            confidence: str,
            evidence: list[dict[str, Any]],
            proposed_change: dict[str, Any],
            expected_benefit: str,
            risk: str,
            rollback_plan: str,
            validation_plan: str,
            quality_signals: dict[str, Any],
            advisory_evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "finding_category": category,
            "severity": severity,
            "confidence": confidence,
            "affected_workflow_fields": proposed_change.get("affected_fields", []),
            "evidence_ids": [
                {
                    "execution_id": item.get("execution_id"),
                    "event_id": item.get("event_id"),
                }
                for item in evidence
            ],
            "proposed_change": proposed_change,
            "expected_benefit": expected_benefit,
            "risk": risk,
            "rollback_notes": rollback_plan,
            "validation_plan": validation_plan,
            "quality_signals": quality_signals,
            "advisory_evidence": advisory_evidence or [],
        }

    async def _workflow_quality_signals(
            self,
            workflow_id: str,
            executions: list[Execution],
            *,
            stale_after_seconds: int,
    ) -> dict[str, Any]:
        workflow_executions = [execution for execution in executions if execution.workflow_id == workflow_id]
        total = len(workflow_executions)
        completed = [execution for execution in workflow_executions if execution.status == ExecutionStatus.COMPLETED]
        failed = [execution for execution in workflow_executions if execution.status == ExecutionStatus.FAILED]
        cancelled = [execution for execution in workflow_executions if execution.status == ExecutionStatus.CANCELLED]
        stale = [
            execution
            for execution in workflow_executions
            if classify_execution_staleness(
                execution,
                stale_after_seconds=stale_after_seconds,
                idle_timeout_seconds=getattr(self._settings(), "agent_activity_idle_timeout_seconds", 600),
                run_timeout_seconds=getattr(self._settings(), "agent_run_timeout_seconds", 7200),
            )["is_stale"]
        ]
        durations: list[float] = []
        for execution in workflow_executions:
            if execution.started_at and execution.completed_at:
                durations.append(
                    max(0.0, (ensure_utc(execution.completed_at) - ensure_utc(execution.started_at)).total_seconds()))
        failure_signatures: dict[str, int] = {}
        timeout_count = 0
        for execution in failed:
            signature = self._failure_signature(execution.error)
            failure_signatures[signature] = failure_signatures.get(signature, 0) + 1
            if "timeout" in signature:
                timeout_count += 1
        approval_requested_count = 0
        approval_rejected_count = 0
        missing_artifact_count = 0
        missing_validation_output_count = 0
        human_correction_count = 0
        for execution in workflow_executions:
            events = await self.context.execution_store.list_events(execution.id)
            approval_requested_count += sum(
                1 for event in events if event.event_type == ExecutionEventType.APPROVAL_REQUESTED
            )
            approval_rejected_count += sum(
                1 for event in events if event.event_type == ExecutionEventType.APPROVAL_REJECTED
            )
            if self._execution_indicates_human_correction(execution, events):
                human_correction_count += 1
            if execution.status == ExecutionStatus.COMPLETED:
                artifacts = await self.context.execution_store.list_artifacts(execution.id)
                if not artifacts:
                    missing_artifact_count += 1
                if not self._has_validation_output(execution, events):
                    missing_validation_output_count += 1
        return {
            "execution_count": total,
            "success_count": len(completed),
            "failure_count": len(failed),
            "cancelled_count": len(cancelled),
            "success_rate": (len(completed) / total) if total else None,
            "stale_execution_count": len(stale),
            "stale_execution_rate": (len(stale) / total) if total else None,
            "average_duration_seconds": (sum(durations) / len(durations)) if durations else None,
            "timeout_frequency": (timeout_count / total) if total else None,
            "repeated_failure_signatures": [
                {"signature": signature, "count": count}
                for signature, count in sorted(failure_signatures.items(), key=lambda item: (-item[1], item[0]))
                if count > 1
            ],
            "approval_request_count": approval_requested_count,
            "approval_rejection_count": approval_rejected_count,
            "approval_rejection_rate": (
                approval_rejected_count / approval_requested_count
                if approval_requested_count
                else None
            ),
            "missing_artifact_count": missing_artifact_count,
            "missing_validation_output_count": missing_validation_output_count,
            "human_correction_count": human_correction_count,
            "human_correction_frequency": (human_correction_count / total) if total else None,
        }

    def _failure_signature(self, error: Any) -> str:
        if error is None:
            return "unknown"
        text = str(error).strip().lower()
        return " ".join(text.split())[:160] or "unknown"

    async def _record_post_change_comparisons(
            self,
            *,
            execution: Execution,
            workflow: WorkflowDefinition,
            quality_signals: dict[str, Any],
    ) -> list[dict[str, Any]]:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        if not isinstance(monitoring, dict):
            return []
        history = monitoring.get("improvement_proposals")
        if not isinstance(history, list):
            return []
        existing_events = await self.context.execution_store.list_events(execution.id)
        compared_keys = {
            str(event.payload.get("proposal_event_id"))
            for event in existing_events
            if event.event_type == ExecutionEventType.MONITOR_IMPROVEMENT_COMPARED
        }
        comparisons: list[dict[str, Any]] = []
        for item in history:
            if not isinstance(item, dict):
                continue
            proposal_event_id = item.get("proposal_event_id")
            baseline = item.get("baseline_quality_signals")
            expected_revision = item.get("expected_replacement_revision")
            if not isinstance(proposal_event_id, str) or not isinstance(baseline, dict):
                continue
            if proposal_event_id in compared_keys:
                continue
            if isinstance(expected_revision, int) and workflow.versioning.revision < expected_revision:
                continue
            comparison = self._post_change_comparison_payload(
                workflow=workflow,
                execution=execution,
                proposal=item,
                baseline_quality_signals=baseline,
                current_quality_signals=quality_signals,
            )
            event = await self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id=execution.id,
                    workflow_id=workflow.id,
                    event_type=ExecutionEventType.MONITOR_IMPROVEMENT_COMPARED,
                    actor_type="system",
                    actor_id="main_agent_monitor",
                    payload_json=comparison,
                    metadata={
                        "source": "main_agent_monitor",
                        "proposal_event_id": proposal_event_id,
                        "outcome": comparison["outcome"],
                    },
                )
            )
            comparison["event_id"] = event.id
            comparisons.append(comparison)
            self.context.runtime_operations.increment("main_agent_monitor.post_change_comparisons")
            self.context.runtime_operations.increment(
                f"main_agent_monitor.post_change_comparisons.{comparison['outcome']}"
            )
            self.context.runtime_operations.record_action("main_agent_monitor.post_change_comparison", **comparison)
            logger.info("Main-agent workflow monitor post-change comparison: %s", comparison)
        return comparisons

    def _post_change_comparison_payload(
            self,
            *,
            workflow: WorkflowDefinition,
            execution: Execution,
            proposal: dict[str, Any],
            baseline_quality_signals: dict[str, Any],
            current_quality_signals: dict[str, Any],
    ) -> dict[str, Any]:
        deltas = self._quality_signal_deltas(baseline_quality_signals, current_quality_signals)
        outcome = self._post_change_outcome(deltas)
        return {
            "workflow_id": workflow.id,
            "execution_id": execution.id,
            "proposal_event_id": proposal.get("proposal_event_id"),
            "baseline_revision": proposal.get("baseline_revision"),
            "evaluated_revision": workflow.versioning.revision,
            "baseline_quality_signals": baseline_quality_signals,
            "current_quality_signals": current_quality_signals,
            "deltas": deltas,
            "outcome": outcome,
            "summary": self._post_change_summary(outcome),
        }

    def _quality_signal_deltas(
            self,
            baseline_quality_signals: dict[str, Any],
            current_quality_signals: dict[str, Any],
    ) -> dict[str, dict[str, float | str]]:
        higher_is_better = {"success_rate", "success_count"}
        lower_is_better = {
            "failure_count",
            "cancelled_count",
            "stale_execution_count",
            "stale_execution_rate",
            "average_duration_seconds",
            "timeout_frequency",
            "approval_rejection_rate",
            "missing_artifact_count",
            "missing_validation_output_count",
            "human_correction_frequency",
        }
        deltas: dict[str, dict[str, float | str]] = {}
        for key in sorted(higher_is_better | lower_is_better):
            before = self._numeric_quality_signal(baseline_quality_signals.get(key))
            after = self._numeric_quality_signal(current_quality_signals.get(key))
            if before is None or after is None:
                continue
            delta = after - before
            direction = "neutral"
            if delta:
                if key in higher_is_better:
                    direction = "improved" if delta > 0 else "regressed"
                else:
                    direction = "improved" if delta < 0 else "regressed"
            deltas[key] = {
                "before": before,
                "after": after,
                "delta": delta,
                "direction": direction,
            }
        return deltas

    def _numeric_quality_signal(self, value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def _post_change_outcome(self, deltas: dict[str, dict[str, float | str]]) -> str:
        improved = any(delta.get("direction") == "improved" for delta in deltas.values())
        regressed = any(delta.get("direction") == "regressed" for delta in deltas.values())
        if improved and not regressed:
            return "helped"
        if regressed and not improved:
            return "regressed"
        return "needs_another_revision"

    def _post_change_summary(self, outcome: str) -> str:
        if outcome == "helped":
            return "Post-change quality signals improved compared with the approved proposal baseline."
        if outcome == "regressed":
            return "Post-change quality signals regressed compared with the approved proposal baseline."
        return "Post-change quality signals are mixed or unchanged; another revision may be needed."

    def _execution_indicates_human_correction(
            self,
            execution: Execution,
            events: list[ExecutionEvent],
    ) -> bool:
        trigger = execution.trigger_payload if isinstance(execution.trigger_payload, dict) else {}
        metadata = execution.metadata if isinstance(execution.metadata, dict) else {}
        candidates = [trigger, metadata, metadata.get("trigger") if isinstance(metadata.get("trigger"), dict) else {}]
        for candidate in candidates:
            if candidate.get("human_correction") is True:
                return True
            for key in (
                    "correction_of_execution_id",
                    "corrected_execution_id",
                    "human_correction_of_execution_id",
                    "revision_requested_by",
            ):
                if candidate.get(key):
                    return True
            source = candidate.get("source") or candidate.get("created_by")
            if isinstance(source, str) and "correction" in source.lower():
                return True
        return any(
            event.actor_type == "human"
            and any(token in str(event.payload).lower() for token in ("correction", "revise", "retry", "fix"))
            for event in events
        )

    def _has_validation_output(
            self,
            execution: Execution,
            events: list[ExecutionEvent],
    ) -> bool:
        if self._contains_validation_marker(execution.output_payload):
            return True
        return any(self._contains_validation_marker(event.payload) for event in events)

    def _contains_validation_marker(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key).lower()
                if any(
                        marker in normalized_key
                        for marker in ("validation", "validated", "verified", "check_result", "test_result")
                ):
                    return True
                if self._contains_validation_marker(item):
                    return True
            return False
        if isinstance(value, list):
            return any(self._contains_validation_marker(item) for item in value)
        if isinstance(value, str):
            normalized = value.lower()
            return any(
                marker in normalized
                for marker in (
                    "validation evidence",
                    "validation passed",
                    "validated",
                    "verified",
                    "check passed",
                    "test passed",
                    "assertion passed",
                )
            )
        return False

    def _workflow_allows_improvement_proposals(self, workflow: WorkflowDefinition) -> bool:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        if not isinstance(monitoring, dict):
            return False
        return monitoring.get("allow_improvement_proposals") is True

    async def _monitor_approval_conversation_id(self, workflow: WorkflowDefinition) -> str | None:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        if isinstance(monitoring, dict):
            conversation_id = monitoring.get("approval_conversation_id")
            if isinstance(conversation_id, str) and conversation_id.strip():
                return conversation_id.strip()

        profile = await self._active_main_agent_profile()
        profile_monitoring = profile.metadata.get("main_agent_monitoring") if profile is not None else None
        if isinstance(profile_monitoring, dict):
            conversation_id = profile_monitoring.get("approval_conversation_id")
            if isinstance(conversation_id, str) and conversation_id.strip():
                return conversation_id.strip()
        return None

    async def _create_monitor_conversation_message(self, message: ConversationMessage) -> ConversationMessage:
        created = await self.context.conversation_message_repo.create(message)
        await self.context.conversation_event_broker.publish(
            created.conversation_id,
            {
                "id": created.id,
                "conversation_id": created.conversation_id,
                "event_type": "message.created",
                "occurred_at": created.created_at.isoformat(),
                "message": created.model_dump(mode="json"),
            },
        )
        return created

    async def _maybe_deliver_monitor_message(
            self,
            *,
            conversation: Any,
            message: ConversationMessage,
            approval_request: ApprovalRequest | None = None,
    ) -> None:
        delivery = conversation.metadata.get("monitor_delivery") if isinstance(conversation.metadata, dict) else None
        if not isinstance(delivery, dict):
            return
        provider = delivery.get("provider") or conversation.channel_type.value
        credential_id = delivery.get("credential_id")
        owner_user_id = conversation.created_by_user_id
        if not all(isinstance(value, str) and value for value in (provider, credential_id, owner_user_id)):
            return
        if conversation.channel_type.value not in chat_channel_types():
            return

        try:
            from app.services.conversations.channel_adapters import AdapterApprovalAction, \
                create_channel_outbound_formatter
            from app.services.conversations.channel_delivery import ChannelOutboundDeliveryService
        except ImportError:
            logger.exception("Main-agent workflow monitor could not import channel delivery services")
            return

        target = AdapterApprovalAction(
            channel_type=conversation.channel_type.value,
            channel_thread_id=conversation.channel_thread_id,
            channel_user_id=conversation.channel_user_id or "",
            approval_request_id=message.approval_request_id or (approval_request.id if approval_request else ""),
            action="notify",
            reason="main_agent_monitor",
        )
        transport_message = self._monitor_transport_message(message, approval_request)
        provider_messages = create_channel_outbound_formatter(str(provider)).format_messages(
            [transport_message],
            target=target,
        )
        try:
            result = await ChannelOutboundDeliveryService(self.context).deliver_for_owner(
                provider=str(provider),
                credential_id=str(credential_id),
                owner_user_id=str(owner_user_id),
                provider_outbound_messages=provider_messages,
            )
        except Exception as exc:
            self.context.runtime_operations.record_action(
                "main_agent_monitor.external_delivery_failed",
                conversation_id=conversation.id,
                message_id=message.id,
                provider=str(provider),
                credential_id=str(credential_id),
                error=str(exc),
            )
            return
        self.context.runtime_operations.record_action(
            "main_agent_monitor.external_delivery",
            conversation_id=conversation.id,
            message_id=message.id,
            provider=str(provider),
            credential_id=str(credential_id),
            ok=bool(result and result.get("ok")),
        )

    def _monitor_transport_message(
            self,
            message: ConversationMessage,
            approval_request: ApprovalRequest | None,
    ) -> dict[str, Any]:
        if message.message_type in {ConversationMessageType.APPROVAL_REQUEST,
                                    ConversationMessageType.WORKFLOW_UPDATE_PROPOSAL}:
            return {
                "type": "approval",
                "text": message.plain_text or "",
                "message_type": message.message_type.value,
                "approval_request_id": message.approval_request_id or (
                    approval_request.id if approval_request else None),
                "approval_status": approval_request.status.value if approval_request else "pending",
                "actions": [
                    {"type": "approve", "label": "Approve"},
                    {"type": "reject", "label": "Reject"},
                ],
            }
        return {
            "type": "text",
            "text": message.plain_text or "",
            "message_type": message.message_type.value,
        }

    async def _maybe_persist_monitor_run_summary(
            self,
            *,
            execution: Execution,
            workflow: WorkflowDefinition,
            finding: MainAgentMonitorFinding,
            finding_event: ExecutionEvent,
    ) -> dict[str, Any] | None:
        effective_workflow = self._workflow_for_monitor_run_summary(workflow, execution)
        if effective_workflow is None:
            return None
        result = await ExecutionRunSummaryService(self.context).maybe_persist_run_summary(
            execution=execution,
            workflow=effective_workflow,
            source="main_agent_workflow_monitor",
            extra_metadata={
                "monitor_job": "main_agent_workflow_monitor",
                "monitor_finding_event_id": finding_event.id,
                "monitor_finding_category": finding.category,
                "workflow_id": workflow.id,
                "source_execution_id": execution.id,
            },
        )
        if result.get("status") == "created":
            self.context.runtime_operations.increment("main_agent_monitor.run_summaries")
            self.context.runtime_operations.record_action(
                "main_agent_monitor.run_summary",
                workflow_id=workflow.id,
                execution_id=execution.id,
                finding_event_id=finding_event.id,
                memory_id=result.get("memory_id"),
            )
        return {
            "workflow_id": workflow.id,
            "execution_id": execution.id,
            "finding_event_id": finding_event.id,
            **result,
        }

    def _workflow_for_monitor_run_summary(
            self,
            workflow: WorkflowDefinition,
            execution: Execution,
    ) -> WorkflowDefinition | None:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        if not isinstance(monitoring, dict):
            return None
        if monitoring.get("safe_to_summarize") is not True:
            return None
        if execution.status == ExecutionStatus.COMPLETED:
            if monitoring.get("store_run_summaries") is not True:
                return None
            store_failures = bool(monitoring.get("store_failure_summaries"))
        elif execution.status == ExecutionStatus.FAILED:
            if monitoring.get("store_failure_summaries") is not True:
                return None
            store_failures = True
        else:
            return None
        existing = workflow.metadata.get("persistent_run_summary")
        existing = existing if isinstance(existing, dict) else {}
        metadata = dict(workflow.metadata)
        metadata["persistent_run_summary"] = {
            **existing,
            "enabled": True,
            "scope": existing.get("scope") or "workflow",
            "importance": int(existing.get("importance") or monitoring.get("run_summary_importance") or 55),
            "store_failures": store_failures,
        }
        return workflow.model_copy(update={"metadata": metadata})

    async def _record_proposal(self, proposal: MainAgentWorkflowImprovementProposal) -> ExecutionEvent:
        payload = asdict(proposal)
        execution_id = proposal.finding["evidence"][0]["execution_id"]
        review_requirements = proposal.proposed_change.get("review_requirements")
        if not isinstance(review_requirements, dict):
            review_requirements = {"required": False, "level": "standard", "reasons": []}
        event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id=execution_id,
                workflow_id=proposal.workflow_id,
                event_type=ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
                actor_type="system",
                actor_id="main_agent_monitor",
                payload_json={
                    **payload,
                    "approval_request_template": {
                        "approval_type": "workflow_update",
                        "target_type": "workflow",
                        "target_id": proposal.workflow_id,
                        "summary": proposal.proposed_change["summary"],
                        "diff_summary": proposal.proposed_change["summary"],
                        "proposed_payload": payload,
                        "metadata": {
                            "source": "main_agent_monitor",
                            "proposal_kind": "workflow_improvement",
                            "requires_human_permission": True,
                            "review_requirements": review_requirements,
                            "strong_review_required": review_requirements.get("required") is True,
                        },
                    },
                },
                metadata={
                    "source": "main_agent_monitor",
                    "proposal_kind": "workflow_improvement",
                    "requires_human_permission": True,
                },
            )
        )
        self.context.runtime_operations.increment("main_agent_monitor.improvement_proposals")
        self.context.runtime_operations.increment(
            f"main_agent_monitor.improvement_proposals.{proposal.finding['category']}"
        )
        self.context.runtime_operations.record_action("main_agent_monitor.improvement_proposal", **payload)
        logger.info("Main-agent workflow monitor improvement proposal: %s", payload)
        return event

    async def _maybe_create_approval_request(
            self,
            *,
            proposal: MainAgentWorkflowImprovementProposal,
            proposal_event: ExecutionEvent,
            workflow: WorkflowDefinition | None,
    ) -> ApprovalRequest | None:
        if workflow is None or not self._workflow_allows_approval_requests(workflow):
            return None
        if not MainAgentPolicyService(self.context, settings=self._settings()).workflow_is_mutable(workflow):
            return None
        monitoring = workflow.metadata.get("main_agent_monitoring")
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        conversation_id = await self._monitor_approval_conversation_id(workflow)
        if not isinstance(conversation_id, str) or not conversation_id:
            return None
        conversation = await self.context.conversation_repo.get(conversation_id)
        if conversation is None:
            return None
        proposal_noise_key = self._proposal_noise_key(proposal)
        existing_approval = await self._pending_monitor_approval_for_noise_key(
            workflow.id,
            proposal_noise_key,
            proposal_kind="workflow_improvement",
        )
        if existing_approval is not None:
            self.context.runtime_operations.record_action(
                "main_agent_monitor.approval_request_deduped",
                workflow_id=workflow.id,
                proposal_event_id=proposal_event.id,
                existing_approval_request_id=existing_approval.id,
                proposal_noise_key=proposal_noise_key,
            )
            return None

        profile = await self._active_main_agent_profile()
        proposed_workflow = self._proposed_workflow_for_approval(workflow, proposal, proposal_event)
        review_requirements = proposal.proposed_change.get("review_requirements")
        if not isinstance(review_requirements, dict):
            review_requirements = {
                "required": False,
                "level": "standard",
                "reasons": [],
                "review_steps": [],
                "affected_fields": proposal.proposed_change.get("affected_fields", []),
            }
        schedule_change_approval = proposal.proposed_change.get("schedule_change_approval")
        if not isinstance(schedule_change_approval, dict):
            schedule_change_approval = None
        tool_assignment_change_approval = proposal.proposed_change.get("tool_assignment_change_approval")
        if not isinstance(tool_assignment_change_approval, dict):
            tool_assignment_change_approval = None
        memory_write_approval = proposal.proposed_change.get("memory_write_approval")
        if not isinstance(memory_write_approval, dict):
            memory_write_approval = None
        origin_message = await self._create_monitor_conversation_message(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.SYSTEM_NOTE,
                plain_text=f"Main-agent monitor proposed an improvement for workflow '{workflow.name}'.",
                content={
                    "source": "main_agent_monitor",
                    "proposal_event_id": proposal_event.id,
                    "workflow_id": workflow.id,
                    "proposal": asdict(proposal),
                },
                metadata={"source": "main_agent_monitor"},
            )
        )
        approval = ApprovalRequest(
            approval_type=ApprovalType.WORKFLOW_UPDATE,
            target_type=ApprovalTargetType.WORKFLOW,
            target_id=workflow.id,
            requested_by_agent_id=getattr(profile, "agent_id", None) or "main_agent_monitor",
            requested_by_profile_id=getattr(profile, "id", None),
            conversation_id=conversation_id,
            origin_message_id=origin_message.id,
            summary=proposal.proposed_change["summary"],
            diff_summary=proposal.proposed_change["summary"],
            proposed_payload={
                "workflow_id": workflow.id,
                "current_revision": workflow.versioning.revision,
                "expected_replacement_revision": workflow.versioning.revision + 1,
                "restart_active_executions": proposal.restart_active_executions,
                "workflow": proposed_workflow.model_dump(mode="json"),
                "patch": proposal.proposed_change,
                "diagnosis": proposal.diagnosis,
                "quality_signals": proposal.quality_signals,
                "evidence": proposal.finding["evidence"],
                "review_requirements": review_requirements,
                "strong_review_required": review_requirements.get("required") is True,
                "schedule_change_approval": schedule_change_approval,
                "tool_assignment_change_approval": tool_assignment_change_approval,
                "memory_write_approval": memory_write_approval,
                "risk": proposal.risk,
                "validation_plan": proposal.validation_plan,
                "rollback_plan": proposal.rollback_plan,
            },
            metadata={
                "action": "workflow_update",
                "source": "main_agent_monitor",
                "proposal_kind": "workflow_improvement",
                "requires_human_permission": True,
                "monitor_proposal_event_id": proposal_event.id,
                "proposal_noise_key": proposal_noise_key,
                "finding": proposal.finding,
                "diagnosis": proposal.diagnosis,
                "quality_signals": proposal.quality_signals,
                "review_requirements": review_requirements,
                "strong_review_required": review_requirements.get("required") is True,
                "schedule_change_approval": schedule_change_approval,
                "tool_assignment_change_approval": tool_assignment_change_approval,
                "memory_write_approval": memory_write_approval,
                "risk": proposal.risk,
                "validation_plan": proposal.validation_plan,
                "rollback_plan": proposal.rollback_plan,
                "restart_active_executions": proposal.restart_active_executions,
                "current_revision": workflow.versioning.revision,
                "expected_replacement_revision": workflow.versioning.revision + 1,
            },
        )
        created = await self.context.conversation_approval_repo.create(approval)
        proposal_message = await self._create_monitor_conversation_message(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.WORKFLOW_UPDATE_PROPOSAL,
                plain_text=created.summary,
                approval_request_id=created.id,
                content={
                    "approval_request_id": created.id,
                    "approval_type": created.approval_type.value,
                    "summary": created.summary,
                    "diff_summary": created.diff_summary,
                    "status": created.status.value,
                    "workflow": {
                        "id": workflow.id,
                        "name": workflow.name,
                        "version": workflow.versioning.version,
                        "revision": workflow.versioning.revision,
                    },
                    "restart_active_executions": proposal.restart_active_executions,
                    "source": "main_agent_monitor",
                },
                metadata={
                    "source": "main_agent_monitor",
                    "profile_id": getattr(profile, "id", None),
                    "monitor_proposal_event_id": proposal_event.id,
                },
            )
        )
        await self._maybe_deliver_monitor_message(
            conversation=conversation,
            message=proposal_message,
            approval_request=created,
        )
        self.context.runtime_operations.increment("main_agent_monitor.approval_requests")
        self.context.runtime_operations.record_action(
            "main_agent_monitor.approval_request",
            approval_request_id=created.id,
            workflow_id=workflow.id,
            proposal_event_id=proposal_event.id,
        )
        return created

    async def _pending_monitor_approval_for_noise_key(
            self,
            workflow_id: str,
            proposal_noise_key: str,
            *,
            proposal_kind: str,
    ) -> ApprovalRequest | None:
        for approval in await self.context.conversation_approval_repo.list():
            if approval.status != ApprovalStatus.PENDING:
                continue
            if approval.target_id != workflow_id:
                continue
            metadata = approval.metadata if isinstance(approval.metadata, dict) else {}
            if metadata.get("source") != "main_agent_monitor":
                continue
            if metadata.get("proposal_kind") != proposal_kind:
                continue
            if metadata.get("proposal_noise_key") == proposal_noise_key:
                return approval
        return None

    def _proposal_noise_key(self, proposal: MainAgentWorkflowImprovementProposal) -> str:
        evidence = proposal.finding.get("evidence") if isinstance(proposal.finding, dict) else None
        evidence = evidence if isinstance(evidence, list) and evidence else []
        first_evidence = evidence[0] if evidence and isinstance(evidence[0], dict) else {}
        failure_text = str(first_evidence.get("failure_text") or first_evidence.get("summary") or "")[:160]
        return ":".join(
            [
                proposal.workflow_id,
                str(proposal.finding.get("category") or ""),
                str(proposal.proposed_change.get("type") or ""),
                failure_text,
            ]
        )

    def _workflow_allows_approval_requests(self, workflow: WorkflowDefinition) -> bool:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        if not isinstance(monitoring, dict):
            return True
        return monitoring.get("route_improvement_proposals_to_approval") is not False

    def _workflow_allows_steering_approval_requests(self, workflow: WorkflowDefinition) -> bool:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        if not isinstance(monitoring, dict):
            return True
        return monitoring.get("route_steering_requests_to_approval") is not False

    async def _maybe_create_steering_approval_request(
            self,
            *,
            steering_request: dict[str, Any],
            workflow: WorkflowDefinition,
    ) -> ApprovalRequest | None:
        if not self._workflow_allows_steering_approval_requests(workflow):
            return None
        policy = steering_request.get("policy") if isinstance(steering_request.get("policy"), dict) else {}
        if policy.get("requires_human_approval") is not True:
            return None
        conversation_id = await self._monitor_approval_conversation_id(workflow)
        if not isinstance(conversation_id, str) or not conversation_id:
            return None
        conversation = await self.context.conversation_repo.get(conversation_id)
        if conversation is None:
            return None
        steering_noise_key = self._steering_noise_key(workflow.id, steering_request)
        existing_approval = await self._pending_monitor_approval_for_noise_key(
            workflow.id,
            steering_noise_key,
            proposal_kind="supervisor_steering",
        )
        if existing_approval is not None:
            self.context.runtime_operations.record_action(
                "main_agent_monitor.steering_approval_request_deduped",
                workflow_id=workflow.id,
                steering_request_event_id=steering_request.get("event_id"),
                existing_approval_request_id=existing_approval.id,
                proposal_noise_key=steering_noise_key,
            )
            return None

        profile = await self._active_main_agent_profile()
        action = str(steering_request.get("recommended_action") or "review")
        operator_parameter_schema = self._steering_operator_parameter_schema(
            action=action,
            steering_request=steering_request,
            workflow=workflow,
        )
        summary = f"Supervisor steering requested: {action}"
        origin_message = await self._create_monitor_conversation_message(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.SYSTEM_NOTE,
                plain_text=f"Main-agent monitor requested supervisor steering for workflow '{workflow.name}'.",
                content={
                    "source": "main_agent_monitor",
                    "workflow_id": workflow.id,
                    "execution_id": steering_request.get("execution_id"),
                    "steering_request": steering_request,
                },
                metadata={
                    "source": "main_agent_monitor",
                    "steering_request_event_id": steering_request.get("event_id"),
                },
            )
        )
        approval = ApprovalRequest(
            approval_type=ApprovalType.OTHER,
            target_type=ApprovalTargetType.WORKFLOW,
            target_id=workflow.id,
            requested_by_agent_id=getattr(profile, "agent_id", None) or "main_agent_monitor",
            requested_by_profile_id=getattr(profile, "id", None),
            conversation_id=conversation_id,
            origin_message_id=origin_message.id,
            summary=summary,
            diff_summary=steering_request.get("reason"),
            proposed_payload={
                "workflow_id": workflow.id,
                "execution_id": steering_request.get("execution_id"),
                "steering_request_event_id": steering_request.get("event_id"),
                "finding_event_id": steering_request.get("finding_event_id"),
                "recommended_action": action,
                "category": steering_request.get("category"),
                "severity": steering_request.get("severity"),
                "reason": steering_request.get("reason"),
                "confidence": steering_request.get("confidence"),
                "evidence": steering_request.get("evidence"),
                "policy": policy,
                "operator_parameter_schema": operator_parameter_schema,
                "operator_steering_parameters": {},
            },
            metadata={
                "action": "supervisor_steering",
                "source": "main_agent_monitor",
                "proposal_kind": "supervisor_steering",
                "requires_human_permission": True,
                "proposal_noise_key": steering_noise_key,
                "workflow_id": workflow.id,
                "execution_id": steering_request.get("execution_id"),
                "steering_request_event_id": steering_request.get("event_id"),
                "finding_event_id": steering_request.get("finding_event_id"),
                "recommended_action": action,
                "category": steering_request.get("category"),
                "severity": steering_request.get("severity"),
                "reason": steering_request.get("reason"),
                "operator_parameter_schema": operator_parameter_schema,
            },
        )
        created = await self.context.conversation_approval_repo.create(approval)
        approval_message = await self._create_monitor_conversation_message(
            ConversationMessage(
                conversation_id=conversation_id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.APPROVAL_REQUEST,
                plain_text=created.summary,
                approval_request_id=created.id,
                content={
                    "approval_request_id": created.id,
                    "approval_type": created.approval_type.value,
                    "summary": created.summary,
                    "diff_summary": created.diff_summary,
                    "status": created.status.value,
                    "workflow": {
                        "id": workflow.id,
                        "name": workflow.name,
                        "version": workflow.versioning.version,
                        "revision": workflow.versioning.revision,
                    },
                    "execution_id": steering_request.get("execution_id"),
                    "recommended_action": action,
                    "operator_parameter_schema": operator_parameter_schema,
                    "source": "main_agent_monitor",
                },
                metadata={
                    "source": "main_agent_monitor",
                    "profile_id": getattr(profile, "id", None),
                    "steering_request_event_id": steering_request.get("event_id"),
                },
            )
        )
        await self._maybe_deliver_monitor_message(
            conversation=conversation,
            message=approval_message,
            approval_request=created,
        )
        await self._attach_steering_approval_to_pending_request(
            execution_id=str(steering_request.get("execution_id") or ""),
            steering_request_event_id=str(steering_request.get("event_id") or ""),
            approval_request_id=created.id,
        )
        self.context.runtime_operations.increment("main_agent_monitor.steering_approval_requests")
        self.context.runtime_operations.record_action(
            "main_agent_monitor.steering_approval_request",
            approval_request_id=created.id,
            workflow_id=workflow.id,
            execution_id=steering_request.get("execution_id"),
            steering_request_event_id=steering_request.get("event_id"),
            recommended_action=action,
        )
        return created

    def _steering_noise_key(self, workflow_id: str, steering_request: dict[str, Any]) -> str:
        return ":".join(
            [
                workflow_id,
                str(steering_request.get("category") or ""),
                str(steering_request.get("recommended_action") or "review"),
                str(steering_request.get("reason") or "")[:160],
            ]
        )

    def _steering_operator_parameter_schema(
            self,
            *,
            action: str,
            steering_request: dict[str, Any],
            workflow: WorkflowDefinition,
    ) -> dict[str, Any]:
        evidence = steering_request.get("evidence") if isinstance(steering_request.get("evidence"), dict) else {}
        target_agent_id = evidence.get("agent_id")
        target_task_id = evidence.get("task_id")
        agent_options = [
            {"value": agent.id, "label": agent.display_name or agent.name or agent.id}
            for agent in workflow.agent_definitions
        ]
        task_options = [
            {"value": task.id, "label": task.name or task.id}
            for task in workflow.task_definitions
        ]
        tool_options = sorted(
            {
                tool_id
                for agent in workflow.agent_definitions
                for tool_id in agent.tool_ids
            }.union(
                {
                    tool_id
                    for task in workflow.task_definitions
                    for tool_id in task.tool_ids
                }
            )
        )
        fields: list[dict[str, Any]] = []
        if action in {"request_replan", "replace_task_instructions", "redirect_subagent", "reduce_tool_scope"}:
            fields.append(
                {
                    "name": "target_task_id",
                    "label": "Target task",
                    "type": "select",
                    "required": False,
                    "default": target_task_id,
                    "options": task_options,
                }
            )
        if action in {"redirect_subagent", "lower_max_iterations", "reduce_tool_scope"}:
            fields.append(
                {
                    "name": "target_agent_id",
                    "label": "Target sub-agent",
                    "type": "select",
                    "required": False,
                    "default": target_agent_id,
                    "options": agent_options,
                }
            )
        if action in {"request_replan", "replace_task_instructions", "redirect_subagent"}:
            fields.append(
                {
                    "name": "instructions",
                    "label": "Operator instructions",
                    "type": "textarea",
                    "required": False,
                    "placeholder": "Add concrete steering instructions for the main agent or sub-agent.",
                }
            )
        if action == "lower_max_iterations":
            fields.append(
                {
                    "name": "max_iterations",
                    "label": "Max iterations",
                    "type": "number",
                    "required": False,
                    "min": 1,
                    "max": 20,
                    "placeholder": "Leave blank to decrement by one.",
                }
            )
        if action == "reduce_tool_scope":
            fields.append(
                {
                    "name": "remove_tool_ids",
                    "label": "Tools to remove",
                    "type": "multiselect",
                    "required": False,
                    "options": [{"value": tool_id, "label": tool_id} for tool_id in tool_options],
                }
            )
        if action == "request_human_review":
            fields.append(
                {
                    "name": "review_note",
                    "label": "Review note",
                    "type": "textarea",
                    "required": False,
                    "placeholder": "Describe what the human reviewer should inspect.",
                }
            )
        return {
            "version": 1,
            "action": action,
            "fields": fields,
            "defaults": {
                "target_agent_id": target_agent_id,
                "target_task_id": target_task_id,
            },
        }

    async def _attach_steering_approval_to_pending_request(
            self,
            *,
            execution_id: str,
            steering_request_event_id: str,
            approval_request_id: str,
    ) -> None:
        if not execution_id or not steering_request_event_id:
            return
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            return
        metadata = dict(execution.metadata)
        runtime_governance = dict(metadata.get("runtime_governance") or {})
        supervision = dict(runtime_governance.get("supervision") or {})
        pending = list(supervision.get("pending_requests") or [])
        for request in pending:
            if isinstance(request, dict) and request.get("event_id") == steering_request_event_id:
                request["approval_request_id"] = approval_request_id
                request["status"] = "pending_approval"
        supervision["pending_requests"] = pending[-50:]
        supervision["last_updated_at"] = utc_now().isoformat()
        runtime_governance["supervision"] = supervision
        metadata["runtime_governance"] = runtime_governance
        execution.metadata = metadata
        execution.updated_at = utc_now()
        await self.context.execution_store.update_execution(execution)

    async def _active_main_agent_profile(self) -> Any | None:
        profiles = await self.context.main_agent_profile_repo.list()
        enabled = [profile for profile in profiles if getattr(profile, "enabled", True)]
        return enabled[0] if enabled else (profiles[0] if profiles else None)

    def _proposed_workflow_for_approval(
            self,
            workflow: WorkflowDefinition,
            proposal: MainAgentWorkflowImprovementProposal,
            proposal_event: ExecutionEvent,
    ) -> WorkflowDefinition:
        metadata = dict(workflow.metadata)
        monitoring = dict(metadata.get("main_agent_monitoring") or {})
        history = monitoring.get("improvement_proposals")
        if not isinstance(history, list):
            history = []
        history = history[-9:] + [
            {
                "proposal_event_id": proposal_event.id,
                "finding": proposal.finding,
                "proposed_change": proposal.proposed_change,
                "baseline_revision": workflow.versioning.revision,
                "expected_replacement_revision": workflow.versioning.revision + 1,
                "baseline_quality_signals": proposal.quality_signals,
                "risk": proposal.risk,
                "validation_plan": proposal.validation_plan,
                "rollback_plan": proposal.rollback_plan,
            }
        ]
        monitoring["improvement_proposals"] = history
        monitoring["last_improvement_proposal_event_id"] = proposal_event.id
        metadata["main_agent_monitoring"] = monitoring

        update: dict[str, Any] = {"metadata": metadata}
        if proposal.proposed_change.get("type") == "task_instruction_update" and workflow.task_definitions:
            tasks = list(workflow.task_definitions)
            target = tasks[-1]
            tasks[-1] = target.model_copy(
                update={
                    "instructions": self._append_monitor_instruction(
                        target.instructions,
                        self._prompt_task_instruction_text(proposal.proposed_change),
                    ),
                    "expected_output": self._append_monitor_instruction(
                        target.expected_output,
                        self._prompt_task_expected_output_text(proposal.proposed_change),
                    ),
                }
            )
            update["task_definitions"] = tasks
        elif proposal.proposed_change.get("type") == "runtime_stale_repair_review":
            monitoring["stale_execution_repair_policy"] = "human_review_required"
            update["metadata"] = metadata
        return workflow.model_copy(update=update)

    def _prompt_task_instruction_text(self, proposed_change: dict[str, Any]) -> str:
        summaries = self._recommendation_summaries(proposed_change)
        if not summaries:
            return "When this task fails, report the failing tool or step, include relevant evidence, and state the recovery path."
        return (
                "Tighten the task prompt with these requirements: "
                + " ".join(f"{index}. {summary}" for index, summary in enumerate(summaries, start=1))
        )

    def _prompt_task_expected_output_text(self, proposed_change: dict[str, Any]) -> str:
        summaries = self._recommendation_summaries(proposed_change)
        if not summaries:
            return "Include validation evidence, produced artifact references when applicable, and any unresolved failure details."
        return (
            "Return output that demonstrates the prompt/task recommendations were followed: include explicit success "
            "criteria status, expected output shape, validation evidence, artifact references when applicable, escalation "
            "details for unresolved blockers, and tool-use boundary decisions."
        )

    def _recommendation_summaries(self, proposed_change: dict[str, Any]) -> list[str]:
        recommendations = proposed_change.get("recommendations")
        if not isinstance(recommendations, list):
            return []
        summaries: list[str] = []
        for item in recommendations:
            if not isinstance(item, dict):
                continue
            summary = item.get("summary")
            if isinstance(summary, str) and summary.strip():
                summaries.append(summary.strip())
        return summaries

    def _append_monitor_instruction(self, existing: str | None, addition: str) -> str:
        if existing and addition in existing:
            return existing
        prefix = existing.strip() if isinstance(existing, str) and existing.strip() else ""
        return f"{prefix}\n\nMain-agent monitor recommendation: {addition}".strip()

    def _record_scan(
            self,
            *,
            active_count: int,
            terminal_count: int,
            skipped: int,
            finding_count: int,
            scanned_by_level: dict[str, int],
    ) -> None:
        self.context.runtime_operations.increment("main_agent_monitor.scans")
        self.context.runtime_operations.increment("main_agent_monitor.active_scanned", active_count)
        self.context.runtime_operations.increment("main_agent_monitor.terminal_scanned", terminal_count)
        self.context.runtime_operations.increment("main_agent_monitor.skipped", skipped)
        for level, count in scanned_by_level.items():
            self.context.runtime_operations.increment(f"main_agent_monitor.scanned.{level}", count)
        self.context.runtime_operations.record_action(
            "main_agent_monitor.scan",
            active_scanned=active_count,
            terminal_scanned=terminal_count,
            skipped=skipped,
            finding_count=finding_count,
            scanned_by_level=scanned_by_level,
        )
