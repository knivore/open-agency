from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app.core.config import Settings, get_settings
from app.core.time import ensure_utc, utc_now
from app.domain import (
    AgentDefinition,
    ApprovalRequest,
    ApprovalTargetType,
    ApprovalType,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    Execution,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    WorkflowDefinition,
)
from app.services.agent_tools import (
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    SYSTEM_EXECUTION_EVENTS_TOOL_ID,
    SYSTEM_EXECUTION_GET_TOOL_ID,
    SYSTEM_WORKFLOW_GET_TOOL_ID,
    SYSTEM_WORKFLOW_LIST_TOOL_ID,
)
from app.services.conversations.policy import MainAgentPolicyService
from app.services.execution_classification import STALE_EXECUTION_STATUSES, classify_execution_staleness
from app.services.execution_run_summary import ExecutionRunSummaryService

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
        settings = self._settings()
        workflows = {workflow.id: workflow for workflow in await self.context.workflow_repo.list()}
        scheduled_workflow_ids = await self._scheduled_workflow_ids()
        self_monitor_workflow_id = await self._self_monitor_workflow_id()
        all_executions = await self.context.execution_store.list_executions()
        retention_result = await self._purge_expired_unlinked_findings(settings, all_executions)
        active_executions = await self.context.execution_store.list_active_executions()
        executions_by_id = {execution.id: execution for execution in all_executions}
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
                stale_after_seconds=settings.main_agent_workflow_monitor_stale_after_seconds,
            )
            if finding is not None:
                findings.append(finding)

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

        emitted_findings = [finding for finding in findings if self._mark_seen(finding)]
        proposals: list[MainAgentWorkflowImprovementProposal] = []
        approval_requests: list[dict[str, Any]] = []
        run_summary_results: list[dict[str, Any]] = []
        post_change_comparisons: list[dict[str, Any]] = []
        evaluation_reviews: list[dict[str, Any]] = []
        for finding in emitted_findings:
            finding_event = await self._record_finding(finding)
            workflow = workflows.get(finding.workflow_id)
            execution = executions_by_id.get(finding.execution_id)
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

        self._record_scan(
            active_count=len(active_executions),
            terminal_count=len(terminal_executions),
            skipped=skipped,
            finding_count=len(emitted_findings),
            scanned_by_level=scanned_by_level,
        )
        return {
            "status": "ok",
            "active_scanned": len(active_executions),
            "terminal_scanned": len(terminal_executions),
            "skipped": skipped,
            "scanned_by_level": scanned_by_level,
            "findings": [asdict(finding) for finding in emitted_findings],
            "finding_count": len(emitted_findings),
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
            "retention": retention_result,
        }

    def _settings(self) -> Settings:
        return self.settings or get_settings()

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
        }

    def _referenced_monitor_event_ids(self, events: list[ExecutionEvent]) -> set[str]:
        referenced: set[str] = set()
        for event in events:
            if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED:
                continue
            if event.event_type in {
                ExecutionEventType.MONITOR_EVALUATION_RECORDED,
                ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
                ExecutionEventType.MONITOR_IMPROVEMENT_COMPARED,
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
            stale_after_seconds: int,
    ) -> MainAgentMonitorFinding | None:
        if execution.status not in STALE_EXECUTION_STATUSES:
            return None
        classification = classify_execution_staleness(execution, stale_after_seconds=stale_after_seconds)
        if not classification["is_stale"]:
            return None
        return MainAgentMonitorFinding(
            category="stale_execution",
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            status=execution.status.value,
            severity="high" if execution.status in {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLING} else "medium",
            reason=classification["reason"] or "Execution is stale.",
            evidence={
                "last_heartbeat_at": execution.last_heartbeat_at.isoformat() if execution.last_heartbeat_at else None,
                "updated_at": execution.updated_at.isoformat(),
                "started_at": execution.started_at.isoformat() if execution.started_at else None,
                "stale_after_seconds": stale_after_seconds,
                "age_seconds": classification["age_seconds"],
                "reference_at": classification["reference_at"],
            },
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

    def _mark_seen(self, finding: MainAgentMonitorFinding) -> bool:
        key = f"{finding.category}:{finding.workflow_id}:{finding.execution_id}:{finding.status}"
        if key in self._seen_finding_keys:
            return False
        self._seen_finding_keys.add(key)
        return True

    async def _record_finding(self, finding: MainAgentMonitorFinding) -> ExecutionEvent:
        payload = asdict(finding)
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
            node_text = " ".join(str(value or "") for value in (node.name, node.node_type.value, node.config, node.metadata)).lower()
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
            if security.allow_network or security.allow_browser or tool.tool_type.value in {"http_request", "mcp_tool", "a2a_remote_agent"}:
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
        if any(token in text for token in ("email", "slack", "telegram", "webhook", "external", "send message", "post to")):
            reasons["external_channels"] = "Workflow text references external communication channels."
        if any(token in text for token in ("credential", "secret", "token", "password", "api key")):
            reasons["credentials"] = "Workflow text references credentials or secrets."
        if any(token in text for token in ("write code", "edit file", "commit", "pull request", "repository", "deploy")):
            reasons["code_writing_tasks"] = "Workflow text references code-writing, repository, or deployment work."
        if any(token in text for token in ("approval boundary", "approval gate", "human approval", "requires approval")):
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
            if classify_execution_staleness(execution, stale_after_seconds=stale_after_seconds)["is_stale"]
        ]
        durations: list[float] = []
        for execution in workflow_executions:
            if execution.started_at and execution.completed_at:
                durations.append(max(0.0, (ensure_utc(execution.completed_at) - ensure_utc(execution.started_at)).total_seconds()))
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
        assert isinstance(monitoring, dict)
        conversation_id = monitoring.get("approval_conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            return None
        conversation = await self.context.conversation_repo.get(conversation_id)
        if conversation is None:
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
        origin_message = await self.context.conversation_message_repo.create(
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
        await self.context.conversation_message_repo.create(
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
        self.context.runtime_operations.increment("main_agent_monitor.approval_requests")
        self.context.runtime_operations.record_action(
            "main_agent_monitor.approval_request",
            approval_request_id=created.id,
            workflow_id=workflow.id,
            proposal_event_id=proposal_event.id,
        )
        return created

    def _workflow_allows_approval_requests(self, workflow: WorkflowDefinition) -> bool:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        if not isinstance(monitoring, dict):
            return False
        return monitoring.get("route_improvement_proposals_to_approval") is True

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
