from __future__ import annotations

import os
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.core.time import utc_now
from app.domain import (
    AgentDefinition,
    ApprovalRequest,
    ApprovalTargetType,
    ApprovalType,
    Conversation,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    Execution,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    GoalDefinition,
    GoalStatus,
    GraphProjectionEvent,
    GraphContextSettings,
    MainAgentProfile,
    MemoryRecord,
    MemoryScope,
    MemoryType,
    MemorySettings,
    ScheduleDefinition,
    ScheduleType,
    TaskDefinition,
    WorkflowEdgeDefinition,
    WorkflowDefinition,
    WorkflowNodeDefinition,
)
from app.graph.neo4j_read import GraphReadDocument, GraphReadNode
from app.services.conversations.core import ConversationApprovalStateError, ConversationService
from app.services.conversations.policy import MainAgentPolicyService
from app.services.main_agent_workflow_monitor import (
    EVALUATION_AGENT_READ_ONLY_TOOL_IDS,
    MainAgentWorkflowMonitorService,
)
from app.services.workflows import WorkflowService
from fastapi.testclient import TestClient


class MainAgentWorkflowMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.settings = SimpleNamespace(
            main_agent_workflow_monitor_default_enabled=True,
            main_agent_workflow_monitor_stale_after_seconds=300,
            main_agent_workflow_monitor_terminal_lookback_seconds=86400,
            main_agent_workflow_monitor_finding_retention_days=60,
            main_agent_workflow_mutation_enabled=True,
        )

    async def asyncTearDown(self) -> None:
        os.environ.pop("AGENT_PERSISTENT_RUN_SUMMARY_ENABLED", None)
        reset_settings_cache()

    def _workflow(self, workflow_id: str, metadata: dict | None = None) -> WorkflowDefinition:
        return WorkflowDefinition(
            id=workflow_id,
            name=f"Workflow {workflow_id}",
            entrypoint="entry",
            nodes=[],
            edges=[],
            metadata=metadata or {},
        )

    async def _save_monitor_approval_context(self, conversation_id: str = "conversation-monitor-approval") -> None:
        await self.context.conversation_repo.create(
            Conversation(
                id=conversation_id,
                created_by_user_id="user-1",
                main_agent_profile_id="main-agent-profile",
            )
        )
        await self.context.main_agent_profile_repo.save(
            MainAgentProfile(
                id="main-agent-profile",
                name="Main Agent",
                agent_id="main-agent",
                default_workflow_id="main-workflow",
            )
        )

    async def _save_evaluation_agent(self, *, unsafe: bool = False, memory_enabled: bool = False) -> None:
        tool_ids = list(EVALUATION_AGENT_READ_ONLY_TOOL_IDS)
        if unsafe:
            tool_ids.append("agency.memory.remember")
        await self.context.agent_repo.save(
            AgentDefinition(
                id="evaluation",
                name="Evaluation",
                instructions="Judge read-only execution evidence.",
                tool_ids=tool_ids,
                memory=MemorySettings(enabled=memory_enabled, strategy="evaluation_judge"),
                metadata={"agent_kind": "evaluation", "runtime_role": "eval_judge"},
            )
        )

    async def _save_execution(
            self,
            *,
            execution_id: str,
            workflow_id: str,
            status: ExecutionStatus,
            age_seconds: int = 600,
            error: str | None = None,
            duration_seconds: int | None = None,
            output_payload: dict | None = None,
            metadata: dict | None = None,
    ) -> Execution:
        timestamp = utc_now() - timedelta(seconds=age_seconds)
        completed_at = None
        if status in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
            completed_at = (
                timestamp + timedelta(seconds=duration_seconds)
                if duration_seconds is not None
                else timestamp
            )
        execution = Execution(
            id=execution_id,
            workflow_id=workflow_id,
            runtime_adapter_id="native",
            status=status,
            input_payload={},
            output_payload=output_payload,
            error=error,
            metadata=metadata or {},
            created_at=timestamp,
            started_at=timestamp,
            updated_at=timestamp,
            completed_at=completed_at,
            last_heartbeat_at=timestamp if status == ExecutionStatus.RUNNING else None,
        )
        return await self.context.execution_store.save_execution(execution)

    async def test_policy_monitoring_honors_visible_default_and_explicit_exemption(self) -> None:
        policy = MainAgentPolicyService(self.context, settings=self.settings)
        visible = self._workflow("workflow-visible", {"visible_to_main_agent": True})
        exempt = self._workflow(
            "workflow-exempt",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"enabled": False, "reason": "human-managed"},
            },
        )
        hidden = self._workflow(
            "workflow-hidden",
            {
                "main_agent_monitoring": {"enabled": True},
            },
        )
        denied = self._workflow(
            "workflow-denied",
            {
                "visible_to_main_agent": True,
                "hidden_from_main_agent": True,
                "main_agent_monitoring": {"enabled": True},
            },
        )
        protected = self._workflow(
            "workflow-protected",
            {
                "visible_to_main_agent": True,
                "protected_execution": True,
                "main_agent_monitoring": {"level": "strict"},
            },
        )
        default_off_settings = SimpleNamespace(
            **{
                **self.settings.__dict__,
                "main_agent_workflow_monitor_default_enabled": False,
            }
        )
        opt_in_policy = MainAgentPolicyService(self.context, settings=default_off_settings)
        opted_in = self._workflow(
            "workflow-opted-in",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"enabled": True},
            },
        )

        self.assertTrue(policy.workflow_is_monitorable_by_main_agent(visible))
        self.assertFalse(policy.workflow_is_monitorable_by_main_agent(exempt))
        self.assertFalse(policy.workflow_is_monitorable_by_main_agent(hidden))
        self.assertFalse(policy.workflow_is_monitorable_by_main_agent(denied))
        self.assertTrue(policy.workflow_is_monitorable_by_main_agent(protected))
        self.assertEqual(policy.workflow_monitoring_level(protected), "strict")
        self.assertFalse(opt_in_policy.workflow_is_monitorable_by_main_agent(visible))
        self.assertTrue(opt_in_policy.workflow_is_monitorable_by_main_agent(opted_in))
        self.assertEqual(opt_in_policy.workflow_monitoring_level(opted_in), "standard")

    async def test_monitor_reports_stale_active_execution_for_monitorable_workflow(self) -> None:
        workflow = self._workflow("workflow-monitorable", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-stale",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(result["scanned_by_level"], {"standard": 1})
        finding = result["findings"][0]
        self.assertEqual(finding["category"], "stale_execution")
        self.assertEqual(finding["execution_id"], "execution-stale")
        metrics = self.context.runtime_operations.snapshot_dict()
        self.assertEqual(metrics["counters"]["main_agent_monitor.findings"], 1)
        self.assertEqual(metrics["counters"]["main_agent_monitor.findings.stale_execution"], 1)
        self.assertEqual(metrics["counters"]["main_agent_monitor.scans"], 1)
        self.assertEqual(metrics["counters"]["main_agent_monitor.scanned.standard"], 1)
        events = await self.context.execution_store.list_events("execution-stale")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, ExecutionEventType.MONITOR_FINDING_CREATED)
        self.assertEqual(events[0].actor, "main_agent_monitor")
        self.assertEqual(events[0].payload["category"], "stale_execution")
        self.assertEqual(events[0].metadata["source"], "main_agent_monitor")

    async def test_monitor_reports_stalled_goal_without_execution_as_goal_finding(self) -> None:
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-stalled-no-execution",
                objective="Keep release evidence current",
                status=GoalStatus.ACTIVE,
                success_criteria=[{"kind": "artifact", "description": "Evidence exists"}],
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["active_goals_scanned"], 1)
        self.assertEqual(result["goal_finding_count"], 1)
        self.assertEqual(result["finding_count"], 1)
        finding = result["goal_findings"][0]
        self.assertEqual(finding["category"], "stalled_goal")
        self.assertEqual(finding["execution_id"], "goal-monitor:goal-stalled-no-execution")
        self.assertEqual(finding["evidence"]["goal_id"], goal.id)
        self.assertEqual(finding["evidence"]["supervision_policy"]["mode"], "guarded")
        self.assertIn(
            "request_more_work_when_evidence_is_insufficient",
            finding["evidence"]["supervision_policy"]["automatic_actions"],
        )

        persisted = await self.context.goal_repo.get(goal.id)
        monitoring = persisted.metadata["main_agent_monitoring"]
        self.assertEqual(monitoring["findings"][0]["finding"]["category"], "stalled_goal")
        self.assertEqual(monitoring["supervisor_actions"][0]["action"], "record_supervisor_finding")
        self.assertEqual(monitoring["supervisor_actions"][0]["finding_category"], "stalled_goal")
        self.assertTrue(monitoring["supervisor_actions"][0]["allowed_by_policy"])
        self.assertFalse(monitoring["supervisor_actions"][0]["requires_approval"])
        self.assertEqual(
            monitoring["supervisor_actions"][0]["policy_decision"]["reason"],
            "Action is explicitly allowed for automatic supervisor execution.",
        )

        repeated = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        self.assertEqual(repeated["goal_finding_count"], 0)

    async def test_run_for_goal_scopes_goal_supervision(self) -> None:
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-scoped-target",
                objective="Inspect this goal only",
                status=GoalStatus.ACTIVE,
                success_criteria=[{"kind": "artifact", "description": "Evidence exists"}],
            )
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-scoped-other",
                objective="Leave this goal for the full cadence",
                status=GoalStatus.ACTIVE,
                success_criteria=[{"kind": "artifact", "description": "Evidence exists"}],
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_for_goal(
            "goal-scoped-target"
        )

        self.assertEqual(result["scope"], {"goal_ids": ["goal-scoped-target"]})
        self.assertEqual(result["active_goals_scanned"], 1)
        self.assertEqual(result["goal_finding_count"], 1)
        self.assertEqual(result["goal_findings"][0]["evidence"]["goal_id"], "goal-scoped-target")
        target = await self.context.goal_repo.get("goal-scoped-target")
        other = await self.context.goal_repo.get("goal-scoped-other")
        self.assertIn("main_agent_monitoring", target.metadata)
        self.assertNotIn("main_agent_monitoring", other.metadata)

    async def test_monitor_inspects_active_goals_for_blocked_stale_failed_and_incomplete_work(self) -> None:
        workflows = [
            self._workflow("workflow-goal-inspect-blocked", {"visible_to_main_agent": True}),
            self._workflow("workflow-goal-inspect-stale", {"visible_to_main_agent": True}),
            self._workflow("workflow-goal-inspect-failed", {"visible_to_main_agent": True}),
            self._workflow("workflow-goal-inspect-incomplete", {"visible_to_main_agent": True}),
        ]
        for workflow in workflows:
            await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-goal-inspect-blocked",
            workflow_id="workflow-goal-inspect-blocked",
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-goal-inspect-blocked",
                workflow_id="workflow-goal-inspect-blocked",
                agent_id="agent-research",
                event_type=ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
                payload_json={
                    "status": "blocked",
                    "current_task": "Collect approval window",
                    "blocker": "Waiting for deployment owner input",
                },
            )
        )
        await self._save_execution(
            execution_id="execution-goal-inspect-stale",
            workflow_id="workflow-goal-inspect-stale",
            status=ExecutionStatus.RUNNING,
            age_seconds=900,
        )
        await self._save_execution(
            execution_id="execution-goal-inspect-failed-1",
            workflow_id="workflow-goal-inspect-failed",
            status=ExecutionStatus.FAILED,
            error="First evidence collection failed",
            age_seconds=120,
        )
        await self._save_execution(
            execution_id="execution-goal-inspect-failed-2",
            workflow_id="workflow-goal-inspect-failed",
            status=ExecutionStatus.FAILED,
            error="Second evidence collection failed",
            age_seconds=60,
        )
        await self._save_execution(
            execution_id="execution-goal-inspect-incomplete",
            workflow_id="workflow-goal-inspect-incomplete",
            status=ExecutionStatus.COMPLETED,
            age_seconds=45,
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-inspect-blocked",
                objective="Unblock a long-running subagent",
                status=GoalStatus.ACTIVE,
                constraints={"autonomy": "advisory"},
                execution_ids=["execution-goal-inspect-blocked"],
                success_criteria=[{"kind": "artifact", "description": "Owner input is captured"}],
            )
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-inspect-stale",
                objective="Recover stale long-running work",
                status=GoalStatus.ACTIVE,
                constraints={"autonomy": "advisory"},
                execution_ids=["execution-goal-inspect-stale"],
                success_criteria=[{"kind": "artifact", "description": "Fresh evidence is attached"}],
            )
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-inspect-failed",
                objective="Recover repeated failed attempts",
                status=GoalStatus.ACTIVE,
                constraints={"autonomy": "advisory"},
                execution_ids=["execution-goal-inspect-failed-1", "execution-goal-inspect-failed-2"],
                success_criteria=[{"kind": "artifact", "description": "Successful retry evidence exists"}],
            )
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-inspect-incomplete",
                objective="Detect completed work without evidence",
                status=GoalStatus.ACTIVE,
                constraints={"autonomy": "advisory"},
                execution_ids=["execution-goal-inspect-incomplete"],
                success_criteria=[{"kind": "artifact", "description": "Completion evidence exists"}],
            )
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-inspect-stalled",
                objective="Detect active goal without current work",
                status=GoalStatus.ACTIVE,
                constraints={"autonomy": "advisory"},
                success_criteria=[{"kind": "artifact", "description": "Next work is scheduled"}],
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["active_goals_scanned"], 5)
        findings_by_goal = {
            finding["evidence"]["goal_id"]: finding
            for finding in result["goal_findings"]
        }
        self.assertEqual(
            set(findings_by_goal),
            {
                "goal-inspect-blocked",
                "goal-inspect-stale",
                "goal-inspect-failed",
                "goal-inspect-incomplete",
                "goal-inspect-stalled",
            },
        )
        self.assertEqual(findings_by_goal["goal-inspect-blocked"]["category"], "goal_execution_signal")
        self.assertEqual(findings_by_goal["goal-inspect-blocked"]["evidence"]["signal_category"], "subagent_blocked")
        self.assertEqual(findings_by_goal["goal-inspect-stale"]["category"], "goal_execution_signal")
        self.assertEqual(findings_by_goal["goal-inspect-stale"]["evidence"]["signal_category"], "stale_execution")
        self.assertEqual(findings_by_goal["goal-inspect-failed"]["category"], "goal_repeated_failure")
        self.assertEqual(findings_by_goal["goal-inspect-incomplete"]["category"], "goal_missing_evidence")
        self.assertEqual(findings_by_goal["goal-inspect-stalled"]["category"], "stalled_goal")

    async def test_run_for_goal_includes_graph_context_for_goal_supervision(self) -> None:
        workflow = self._workflow("workflow-goal-graph-context", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        execution = await self.context.execution_store.save_execution(
            Execution(
                id="execution-goal-graph-context",
                workflow_id=workflow.id,
                goal_id="goal-graph-context",
                runtime_adapter_id="native",
                status=ExecutionStatus.FAILED,
                error="Research workflow failed before collecting evidence.",
                input_payload={"topic": "goal supervision"},
                created_at=utc_now() - timedelta(minutes=20),
                started_at=utc_now() - timedelta(minutes=20),
                completed_at=utc_now() - timedelta(minutes=18),
            )
        )
        failure_event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id=execution.id,
                workflow_id=workflow.id,
                event_type=ExecutionEventType.EXECUTION_FAILED,
                payload_json={"error": "tool timeout while collecting evidence", "next_action": "retry read-only"},
            )
        )
        await self.context.execution_store.save_artifact(
            ExecutionArtifact(
                id="artifact-goal-graph-context",
                execution_id=execution.id,
                event_id=failure_event.id,
                artifact_type="diagnostic",
                name="failure-summary",
                content_json={"summary": "Timeout before evidence collection"},
            )
        )
        memory = await self.context.memory_repo.create(
            MemoryRecord(
                id="memory-goal-graph-context",
                scope=MemoryScope.GLOBAL,
                content="Goal supervision summary with blocker and next action.",
                summary="Retry with read-only evidence collection.",
                tags=["goal_summary", "goal:goal-graph-context"],
                source="goal_summary",
                memory_type=MemoryType.ARCHIVE,
            )
        )
        await self.context.graph_projection_event_repo.append(
            GraphProjectionEvent(
                event_type="goal.execution_linked",
                aggregate_type="goal",
                aggregate_id="goal-graph-context",
                payload={
                    "goal_id": "goal-graph-context",
                    "execution_id": execution.id,
                    "relationships": [{"from": "goal-graph-context", "to": execution.id, "type": "has_execution"}],
                },
            )
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-graph-context",
                objective="Collect evidence for a long-running goal",
                status=GoalStatus.ACTIVE,
                priority="high",
                constraints={"autonomy": "guarded", "allowed_tools": ["read_only"]},
                execution_ids=[execution.id],
                evidence=[{"kind": "memory", "id": memory.id}],
                success_criteria=[{"kind": "artifact", "description": "Evidence artifact exists"}],
                metadata={
                    "memory_ids": [memory.id],
                    "goal_planning": {
                        "active_plan": {
                            "version": 1,
                            "steps": [
                                {
                                    "id": "step-retry-evidence",
                                    "action": "start_workflow",
                                    "status": "pending",
                                    "workflow_id": workflow.id,
                                    "expected_evidence": ["artifact"],
                                }
                            ],
                        }
                    },
                    "main_agent_monitoring": {
                        "supervisor_decisions": [
                            {
                                "id": "decision-retry",
                                "decision": "retry_read_only_investigation",
                                "reason": "Prior attempt failed before evidence was collected.",
                            }
                        ]
                    },
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_for_goal(
            "goal-graph-context"
        )

        graph_context = result["goal_graph_context"]
        self.assertEqual(graph_context["status"], "ok")
        self.assertEqual(graph_context["query_meta"]["intent"], "supervise_goal")
        self.assertEqual(graph_context["goal"]["id"], "goal-graph-context")
        self.assertEqual(graph_context["prior_attempts"][0]["id"], execution.id)
        self.assertEqual(graph_context["failures"][0]["execution_id"], execution.id)
        self.assertEqual(graph_context["decisions"][0]["id"], "decision-retry")
        self.assertEqual(graph_context["next_actions"][0]["id"], "step-retry-evidence")
        self.assertEqual(graph_context["related_memories"][0]["id"], memory.id)
        self.assertIn(
            {"from": "goal-graph-context", "to": execution.id, "type": "has_execution", "workflow_id": workflow.id},
            graph_context["relationships"],
        )
        self.assertEqual(graph_context["projection_events"][0]["event_type"], "goal.execution_linked")

    async def test_goal_supervision_policy_honors_off_and_high_autonomy_modes(self) -> None:
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-supervision-off",
                objective="Human-managed goal",
                status=GoalStatus.ACTIVE,
                constraints={"autonomy": "off"},
            )
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-supervision-high",
                objective="Investigate safely without writes",
                status=GoalStatus.ACTIVE,
                constraints={"autonomy": "high_autonomy"},
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["active_goals_scanned"], 2)
        self.assertEqual(result["goal_finding_count"], 1)
        policy = result["goal_findings"][0]["evidence"]["supervision_policy"]
        self.assertEqual(result["goal_findings"][0]["evidence"]["goal_id"], "goal-supervision-high")
        self.assertEqual(policy["mode"], "high_autonomy")
        self.assertIn("spawn_read_only_investigation", policy["automatic_actions"])
        self.assertIn("low_risk_replan", policy["automatic_actions"])
        self.assertIn("request_human_approval", policy["automatic_actions"])
        self.assertTrue(policy["requires_approval_for_mutations"])
        self.assertIn("external_write", policy["approval_required_actions"])

    async def test_monitor_records_stalled_goal_with_terminal_execution_as_event(self) -> None:
        await self._save_execution(
            execution_id="execution-goal-terminal",
            workflow_id="workflow-goal-terminal",
            status=ExecutionStatus.COMPLETED,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-stalled-terminal-execution",
                objective="Follow through after a completed attempt",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-terminal"],
                evidence=[{"type": "evaluation", "id": "evaluation-evidence"}],
                success_criteria=[{"kind": "evaluation", "description": "Completion evidence is accepted"}],
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        finding = result["goal_findings"][0]
        self.assertEqual(finding["category"], "stalled_goal")
        self.assertEqual(finding["execution_id"], "execution-goal-terminal")
        self.assertEqual(finding["evidence"]["goal_id"], goal.id)
        events = await self.context.execution_store.list_events("execution-goal-terminal")
        monitor_events = [
            event for event in events if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED
        ]
        self.assertEqual(len(monitor_events), 1)
        self.assertEqual(monitor_events[0].payload["category"], "stalled_goal")
        self.assertEqual(monitor_events[0].payload["evidence"]["goal_id"], goal.id)
        persisted = await self.context.goal_repo.get(goal.id)
        monitoring = persisted.metadata["main_agent_monitoring"]
        self.assertEqual(monitoring["findings"][0]["execution_event_id"], monitor_events[0].id)
        self.assertEqual(monitoring["findings"][0]["finding"]["category"], "stalled_goal")
        self.assertEqual(monitoring["last_supervisor_action"]["action"], "record_supervisor_finding")
        self.assertTrue(monitoring["last_supervisor_action"]["allowed_by_policy"])

    async def test_monitor_detects_active_execution_that_no_longer_matches_goal_plan(self) -> None:
        await self._save_execution(
            execution_id="execution-goal-plan-drift",
            workflow_id="workflow-old-plan",
            status=ExecutionStatus.RUNNING,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-plan-drift",
                objective="Run the current deployment plan",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-plan-drift"],
                success_criteria=[{"kind": "artifact", "description": "Deployment report exists"}],
                metadata={
                    "goal_planning": {
                        "active_plan": {
                            "version": 3,
                            "steps": [
                                {
                                    "action": "start_workflow",
                                    "workflow_id": "workflow-current-plan",
                                    "status": "pending",
                                }
                            ],
                        }
                    }
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        finding = result["goal_findings"][0]
        self.assertEqual(finding["category"], "goal_plan_mismatch")
        self.assertEqual(finding["execution_id"], "execution-goal-plan-drift")
        self.assertEqual(finding["evidence"]["goal_id"], goal.id)
        self.assertEqual(finding["evidence"]["active_plan_version"], 3)
        self.assertEqual(finding["evidence"]["expected_workflow_ids"], ["workflow-current-plan"])
        self.assertEqual(finding["evidence"]["mismatched_workflow_ids"], ["workflow-old-plan"])
        self.assertEqual(finding["evidence"]["recommended_action"], "inspect_or_redirect_active_execution")
        events = await self.context.execution_store.list_events("execution-goal-plan-drift")
        monitor_events = [
            event for event in events if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED
        ]
        self.assertEqual(len(monitor_events), 1)
        self.assertEqual(monitor_events[0].payload["category"], "goal_plan_mismatch")

        repeated = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        self.assertEqual(repeated["goal_finding_count"], 0)

    async def test_monitor_allows_active_execution_matching_goal_plan(self) -> None:
        await self._save_execution(
            execution_id="execution-goal-plan-match",
            workflow_id="workflow-current-plan",
            status=ExecutionStatus.RUNNING,
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-plan-match",
                objective="Run the selected plan",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-plan-match"],
                success_criteria=[{"kind": "artifact", "description": "Report exists"}],
                metadata={
                    "goal_planning": {
                        "active_plan": {
                            "version": 1,
                            "steps": [
                                {
                                    "action": "start_workflow",
                                    "workflow_id": "workflow-current-plan",
                                    "status": "active",
                                }
                            ],
                        }
                    }
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["active_goals_scanned"], 1)
        self.assertEqual(result["goal_finding_count"], 0)

    async def test_goal_supervisor_creates_approval_for_policy_required_action(self) -> None:
        await self._save_monitor_approval_context("conversation-goal-supervisor-approval")
        await self._save_execution(
            execution_id="execution-goal-approval-plan-drift",
            workflow_id="workflow-old-approval-plan",
            status=ExecutionStatus.RUNNING,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-supervisor-approval",
                objective="Guard risky goal steering",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-approval-plan-drift"],
                success_criteria=[{"kind": "artifact", "description": "Steering evidence exists"}],
                constraints={
                    "approval_policy": {
                        "approval_conversation_id": "conversation-goal-supervisor-approval",
                        "approval_required_actions": ["inspect_or_redirect_active_execution"],
                    }
                },
                metadata={
                    "goal_planning": {
                        "active_plan": {
                            "version": 2,
                            "steps": [
                                {
                                    "action": "start_workflow",
                                    "workflow_id": "workflow-current-approval-plan",
                                    "status": "pending",
                                }
                            ],
                        }
                    }
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        self.assertEqual(result["approval_request_count"], 1)
        approval_id = result["approval_requests"][0]["id"]
        approval = await self.context.conversation_approval_repo.get(approval_id)
        assert approval is not None
        self.assertEqual(approval.target_type, ApprovalTargetType.OTHER)
        self.assertEqual(approval.target_id, goal.id)
        self.assertEqual(approval.metadata["proposal_kind"], "goal_supervisor_action")
        self.assertEqual(approval.metadata["recommended_action"], "inspect_or_redirect_active_execution")
        self.assertEqual(approval.proposed_payload["goal_id"], goal.id)
        self.assertTrue(approval.proposed_payload["requires_approval"])
        self.assertTrue(approval.proposed_payload["policy_decision"]["requires_approval"])

        persisted = await self.context.goal_repo.get(goal.id)
        approval_links = persisted.metadata["main_agent_monitoring"]["approval_requests"]
        self.assertEqual(approval_links[0]["approval_request_id"], approval_id)
        listed = await self.context.goal_repo.get(goal.id)
        decisions = listed.metadata["main_agent_monitoring"]["supervisor_decisions"]
        actions = listed.metadata["main_agent_monitoring"]["supervisor_actions"]
        self.assertEqual(decisions[0]["action"], "inspect_or_redirect_active_execution")
        self.assertEqual(decisions[0]["approval_request_id"], approval_id)
        self.assertTrue(decisions[0]["requires_approval"])
        self.assertTrue(decisions[0]["allowed_by_policy"])
        self.assertEqual(decisions[0]["policy_decision"]["action"], "inspect_or_redirect_active_execution")
        self.assertEqual(decisions[0]["policy_decision"]["reason"], "Action requires human approval before execution.")
        self.assertEqual(actions[-1]["action"], "request_human_approval")
        self.assertEqual(actions[-1]["approval_request_id"], approval_id)
        self.assertTrue(actions[-1]["allowed_by_policy"])
        self.assertFalse(actions[-1]["requires_approval"])
        self.assertEqual(actions[-1]["policy_decision"]["action"], "request_human_approval")
        self.assertEqual(
            listed.metadata["main_agent_monitoring"]["last_approval_request"]["recommended_action"],
            "inspect_or_redirect_active_execution",
        )

        repeated = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        self.assertEqual(repeated["approval_request_count"], 0)

    async def test_goal_supervisor_rolls_up_token_budget_signal(self) -> None:
        workflow = self._workflow("workflow-goal-token-signal", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-goal-token-signal",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-token-signal",
                objective="Keep token budget healthy for long-running research",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-token-signal"],
                success_criteria=[{"kind": "artifact", "description": "Research evidence exists"}],
            )
        )
        token_event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-goal-token-signal",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
                payload_json={"budget": {"scope": "goal", "used_tokens": 2100, "budget_tokens": 2000}},
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        self.assertEqual(result["finding_count"], 2)
        finding = result["goal_findings"][0]
        self.assertEqual(finding["category"], "goal_execution_signal")
        self.assertEqual(finding["evidence"]["goal_id"], goal.id)
        self.assertEqual(finding["evidence"]["signal_category"], "token_budget_exceeded")
        self.assertEqual(finding["evidence"]["signal_group"], "token_budget")
        self.assertEqual(finding["evidence"]["source_event_id"], token_event.id)
        self.assertEqual(finding["evidence"]["recommended_action"], "review_goal_budget_or_context")
        events = await self.context.execution_store.list_events("execution-goal-token-signal")
        monitor_events = [
            event for event in events if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED
        ]
        self.assertEqual([event.payload["category"] for event in monitor_events], [
            "token_budget_exceeded",
            "goal_execution_signal",
        ])

    async def test_goal_supervisor_enforces_goal_token_budget(self) -> None:
        workflow = self._workflow("workflow-goal-budget-control", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-goal-budget-control",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-budget-control",
                objective="Keep research within the goal token budget",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-budget-control"],
                success_criteria=[{"kind": "artifact", "description": "Research evidence exists"}],
                constraints={"budget": {"max_tokens": 2000, "warn_ratio": 0.75}},
            )
        )
        token_event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-goal-budget-control",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
                payload_json={"budget": {"scope": "goal", "used_tokens": 2100, "budget_tokens": 2500}},
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        finding = result["goal_findings"][0]
        self.assertEqual(finding["category"], "goal_budget_exceeded")
        self.assertEqual(finding["evidence"]["goal_id"], goal.id)
        self.assertEqual(finding["evidence"]["used_tokens"], 2100)
        self.assertEqual(finding["evidence"]["max_tokens"], 2000)
        self.assertEqual(finding["evidence"]["source_event_id"], token_event.id)
        self.assertTrue(finding["evidence"]["budget_exceeded"])
        self.assertEqual(finding["evidence"]["recommended_action"], "review_goal_budget_or_context")

    async def test_goal_supervisor_warns_before_goal_token_budget_is_exceeded(self) -> None:
        workflow = self._workflow("workflow-goal-budget-warning", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-goal-budget-warning",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-budget-warning",
                objective="Warn before budget is exhausted",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-budget-warning"],
                success_criteria=[{"kind": "artifact", "description": "Research evidence exists"}],
                constraints={"token_budget": {"max_tokens": "2000", "warn_ratio": "0.75"}},
            )
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-goal-budget-warning",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.TOKEN_BUDGET_WARNING,
                payload_json={"budget": {"scope": "goal", "used_tokens": 1600, "budget_tokens": 2500}},
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        finding = result["goal_findings"][0]
        self.assertEqual(finding["category"], "goal_budget_warning")
        self.assertEqual(finding["evidence"]["used_tokens"], 1600)
        self.assertEqual(finding["evidence"]["max_tokens"], 2000)
        self.assertEqual(finding["evidence"]["warn_ratio"], 0.75)
        self.assertFalse(finding["evidence"]["budget_exceeded"])
        self.assertTrue(finding["evidence"]["budget_warning"])

    async def test_goal_supervisor_rolls_up_subagent_input_signal(self) -> None:
        workflow = self._workflow("workflow-goal-subagent-signal", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-goal-subagent-signal",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-subagent-signal",
                objective="Resolve subagent blocker for deployment evidence",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-subagent-signal"],
                success_criteria=[{"kind": "artifact", "description": "Deployment evidence exists"}],
            )
        )
        input_event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-goal-subagent-signal",
                workflow_id=workflow.id,
                agent_id="agent-deploy",
                task_id="task-deploy",
                event_type=ExecutionEventType.SUBAGENT_NEEDS_INPUT,
                payload_json={"question": "Which deployment window should I use?"},
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        finding = result["goal_findings"][0]
        self.assertEqual(finding["category"], "goal_execution_signal")
        self.assertEqual(finding["evidence"]["goal_id"], goal.id)
        self.assertEqual(finding["evidence"]["signal_category"], "subagent_needs_input")
        self.assertEqual(finding["evidence"]["signal_group"], "subagent")
        self.assertEqual(finding["evidence"]["source_event_id"], input_event.id)
        self.assertEqual(finding["evidence"]["recommended_action"], "inspect_or_redirect_subagent")

    async def test_goal_supervisor_redacts_sensitive_evidence_in_records_and_approvals(self) -> None:
        await self._save_monitor_approval_context("conversation-goal-redaction")
        workflow = self._workflow("workflow-goal-redaction", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-goal-redaction",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-redaction",
                objective="Keep supervisor artifacts clean",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-redaction"],
                success_criteria=[{"kind": "artifact", "description": "Evidence exists"}],
                constraints={
                    "approval_policy": {
                        "approval_conversation_id": "conversation-goal-redaction",
                        "approval_required_actions": ["inspect_or_redirect_subagent"],
                    }
                },
            )
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-goal-redaction",
                workflow_id=workflow.id,
                agent_id="agent-secret",
                task_id="task-secret",
                event_type=ExecutionEventType.SUBAGENT_NEEDS_INPUT,
                payload_json={
                    "question": "Use Bearer super-secret-token to inspect the deployment window",
                    "api_key": "sk-thisisnotarealkeybutlookslikesecret12345",
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        self.assertEqual(result["approval_request_count"], 1)
        events = await self.context.execution_store.list_events("execution-goal-redaction")
        monitor_payloads = [
            event.payload for event in events if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED
        ]
        self.assertEqual(len(monitor_payloads), 2)
        for payload in monitor_payloads:
            serialized = str(payload)
            self.assertNotIn("super-secret-token", serialized)
            self.assertNotIn("sk-thisisnotarealkey", serialized)
            self.assertIn("[REDACTED]", serialized)

        persisted = await self.context.goal_repo.get(goal.id)
        serialized_monitoring = str(persisted.metadata["main_agent_monitoring"])
        self.assertNotIn("super-secret-token", serialized_monitoring)
        self.assertNotIn("sk-thisisnotarealkey", serialized_monitoring)
        self.assertIn("[REDACTED]", serialized_monitoring)

        approval = await self.context.conversation_approval_repo.get(result["approval_requests"][0]["id"])
        assert approval is not None
        serialized_approval = str(approval.proposed_payload)
        self.assertNotIn("super-secret-token", serialized_approval)
        self.assertNotIn("sk-thisisnotarealkey", serialized_approval)
        self.assertIn("[REDACTED]", serialized_approval)

    async def test_monitor_detects_missing_evidence_after_completed_goal_execution(self) -> None:
        await self._save_execution(
            execution_id="execution-goal-completed-without-evidence",
            workflow_id="workflow-goal-evidence",
            status=ExecutionStatus.COMPLETED,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-missing-completion-evidence",
                objective="Publish deployment report",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-completed-without-evidence"],
                success_criteria=[{"id": "report", "kind": "artifact", "description": "Report artifact exists"}],
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        finding = result["goal_findings"][0]
        self.assertEqual(finding["category"], "goal_missing_evidence")
        self.assertEqual(finding["execution_id"], "execution-goal-completed-without-evidence")
        self.assertEqual(finding["evidence"]["goal_id"], goal.id)
        self.assertEqual(finding["evidence"]["evaluation_status"], "missing_evidence")
        self.assertEqual(finding["evidence"]["recommended_action"], "attach_or_request_completion_evidence")
        self.assertEqual(finding["evidence"]["missing_evidence"][0]["criterion_id"], "report")
        events = await self.context.execution_store.list_events("execution-goal-completed-without-evidence")
        monitor_events = [
            event for event in events if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED
        ]
        self.assertEqual(len(monitor_events), 1)
        self.assertEqual(monitor_events[0].payload["category"], "goal_missing_evidence")
        self.assertEqual(monitor_events[0].payload["evidence"]["intent"], "evaluate_evidence")

        repeated = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        self.assertEqual(repeated["goal_finding_count"], 0)

    async def test_goal_supervisor_spawns_read_only_investigation_under_high_autonomy(self) -> None:
        investigation_workflow = self._workflow(
            "workflow-read-only-investigation",
            {"visible_to_main_agent": True, "read_only": True},
        )
        await self.context.workflow_repo.create(investigation_workflow)
        await self._save_execution(
            execution_id="execution-goal-investigation-source",
            workflow_id="workflow-goal-investigation-source",
            status=ExecutionStatus.COMPLETED,
            age_seconds=60,
            output_payload={"summary": "Finished without producing artifact evidence"},
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-read-only-investigation",
                objective="Collect missing release evidence",
                status=GoalStatus.ACTIVE,
                constraints={
                    "autonomy": "high_autonomy",
                    "read_only_investigation_workflow_id": investigation_workflow.id,
                },
                execution_ids=["execution-goal-investigation-source"],
                success_criteria=[
                    {"id": "release-artifact", "kind": "artifact", "description": "Release artifact"}
                ],
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        self.assertEqual(result["goal_findings"][0]["category"], "goal_missing_evidence")
        self.assertEqual(result["auto_investigation_count"], 1)
        investigation = result["auto_investigations"][0]
        self.assertEqual(investigation["goal_id"], goal.id)
        self.assertEqual(investigation["workflow_id"], investigation_workflow.id)
        self.assertEqual(investigation["policy_decision"]["action"], "spawn_read_only_investigation")
        self.assertFalse(investigation["policy_decision"]["requires_approval"])

        spawned = await self.context.execution_store.get_execution(investigation["execution_id"])
        assert spawned is not None
        self.assertEqual(spawned.goal_id, goal.id)
        self.assertEqual(spawned.workflow_id, investigation_workflow.id)
        self.assertEqual(spawned.status, ExecutionStatus.CREATED)
        self.assertEqual(spawned.input_payload["investigation_mode"], "read_only")
        self.assertEqual(spawned.input_payload["finding_category"], "goal_missing_evidence")
        self.assertTrue(spawned.trigger_payload["read_only"])

        persisted = await self.context.goal_repo.get(goal.id)
        self.assertIn(investigation["execution_id"], persisted.execution_ids)
        monitoring = persisted.metadata["main_agent_monitoring"]
        self.assertEqual(monitoring["supervisor_decisions"][-1]["action"], "spawn_read_only_investigation")
        self.assertEqual(monitoring["supervisor_actions"][-1]["action"], "spawn_read_only_investigation")
        self.assertEqual(result["approval_request_count"], 0)

        repeated = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        self.assertEqual(repeated["auto_investigation_count"], 0)

    async def test_goal_supervisor_skips_investigation_workflow_without_read_only_marker(self) -> None:
        investigation_workflow = self._workflow(
            "workflow-unsafe-investigation",
            {"visible_to_main_agent": True},
        )
        await self.context.workflow_repo.create(investigation_workflow)
        await self._save_execution(
            execution_id="execution-goal-unsafe-investigation-source",
            workflow_id="workflow-goal-unsafe-investigation-source",
            status=ExecutionStatus.COMPLETED,
            age_seconds=60,
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-unsafe-investigation",
                objective="Avoid launching an unmarked investigation workflow",
                status=GoalStatus.ACTIVE,
                constraints={
                    "autonomy": "high_autonomy",
                    "read_only_investigation_workflow_id": investigation_workflow.id,
                },
                execution_ids=["execution-goal-unsafe-investigation-source"],
                success_criteria=[{"id": "report", "kind": "artifact", "description": "Report artifact"}],
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        self.assertEqual(result["auto_investigation_count"], 0)

    async def test_monitor_detects_repeated_failures_for_same_goal(self) -> None:
        await self._save_execution(
            execution_id="execution-goal-failed-earlier",
            workflow_id="workflow-goal-retry",
            status=ExecutionStatus.FAILED,
            error="Search service timed out",
            age_seconds=120,
        )
        await self._save_execution(
            execution_id="execution-goal-failed-latest",
            workflow_id="workflow-goal-retry",
            status=ExecutionStatus.FAILED,
            error="Search service timed out again",
            age_seconds=30,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-repeated-failure",
                objective="Collect external release evidence",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-failed-earlier", "execution-goal-failed-latest"],
                success_criteria=[{"kind": "artifact", "description": "Release evidence is attached"}],
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        finding = result["goal_findings"][0]
        self.assertEqual(finding["category"], "goal_repeated_failure")
        self.assertEqual(finding["execution_id"], "execution-goal-failed-latest")
        self.assertEqual(finding["evidence"]["goal_id"], goal.id)
        self.assertEqual(finding["evidence"]["failure_count"], 2)
        self.assertEqual(finding["evidence"]["recommended_action"], "request_replan")
        self.assertEqual(
            finding["evidence"]["source_execution_ids"],
            ["execution-goal-failed-latest", "execution-goal-failed-earlier"],
        )
        events = await self.context.execution_store.list_events("execution-goal-failed-latest")
        monitor_events = [
            event for event in events if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED
        ]
        self.assertEqual(len(monitor_events), 1)
        self.assertEqual(monitor_events[0].payload["category"], "goal_repeated_failure")
        self.assertEqual(monitor_events[0].payload["evidence"]["intent"], "replan_goal")

        repeated = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        self.assertEqual(repeated["goal_finding_count"], 0)

    async def test_monitor_auto_replans_repeated_goal_failure_under_high_autonomy(self) -> None:
        await self._save_execution(
            execution_id="execution-goal-auto-replan-earlier",
            workflow_id="workflow-goal-auto-replan",
            status=ExecutionStatus.FAILED,
            error="Search service timed out",
            age_seconds=120,
        )
        await self._save_execution(
            execution_id="execution-goal-auto-replan-latest",
            workflow_id="workflow-goal-auto-replan",
            status=ExecutionStatus.FAILED,
            error="Search service timed out again",
            age_seconds=30,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-auto-replan",
                objective="Collect external release evidence without mutating definitions",
                status=GoalStatus.ACTIVE,
                constraints={"autonomy": "high_autonomy"},
                execution_ids=["execution-goal-auto-replan-earlier", "execution-goal-auto-replan-latest"],
                success_criteria=[{"kind": "artifact", "description": "Release evidence is attached"}],
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        self.assertEqual(result["auto_replan_count"], 1)
        auto_replan = result["auto_replans"][0]
        self.assertEqual(auto_replan["goal_id"], goal.id)
        self.assertEqual(auto_replan["finding_category"], "goal_repeated_failure")
        self.assertEqual(auto_replan["policy_decision"]["action"], "low_risk_replan")
        self.assertFalse(auto_replan["policy_decision"]["requires_approval"])
        self.assertEqual(auto_replan["plan_version"], 1)
        self.assertEqual(
            [step["action"] for step in auto_replan["plan"]["steps"]],
            ["inspect_execution", "inspect_execution", "retrieve_memory", "start_workflow", "evaluate_evidence"],
        )
        self.assertEqual(auto_replan["plan"]["steps"][3]["workflow_id"], "workflow-goal-auto-replan")
        replanned = await self.context.goal_repo.get(goal.id)
        planning = replanned.metadata["goal_planning"]
        self.assertEqual(planning["active_plan"]["version"], 1)
        self.assertEqual(planning["last_planned_by"], "main_agent_monitor")
        monitoring = replanned.metadata["main_agent_monitoring"]
        self.assertEqual(monitoring["supervisor_decisions"][0]["action"], "low_risk_replan")
        self.assertEqual(monitoring["supervisor_actions"][-1]["action"], "low_risk_replan")
        self.assertEqual(monitoring["supervisor_actions"][-1]["status"], "completed")
        self.assertEqual(result["approval_request_count"], 0)

    async def test_monitor_escalates_when_goal_retry_limit_is_reached(self) -> None:
        await self._save_execution(
            execution_id="execution-goal-retry-limit-1",
            workflow_id="workflow-goal-retry-limit",
            status=ExecutionStatus.FAILED,
            error="First retry failed",
            age_seconds=120,
        )
        await self._save_execution(
            execution_id="execution-goal-retry-limit-2",
            workflow_id="workflow-goal-retry-limit",
            status=ExecutionStatus.FAILED,
            error="Second retry failed",
            age_seconds=30,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-retry-limit",
                objective="Stop looping after repeated failed work",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-retry-limit-1", "execution-goal-retry-limit-2"],
                success_criteria=[{"kind": "artifact", "description": "Evidence exists"}],
                constraints={"supervision_limits": {"max_retry_count": 2}},
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        finding = result["goal_findings"][0]
        self.assertEqual(finding["category"], "goal_supervision_limit_reached")
        self.assertEqual(finding["evidence"]["goal_id"], goal.id)
        self.assertEqual(finding["evidence"]["failed_execution_count"], 2)
        self.assertEqual(finding["evidence"]["max_retry_count"], 2)
        self.assertTrue(finding["evidence"]["retry_limit_reached"])
        self.assertEqual(finding["evidence"]["recommended_action"], "escalate_goal_loop_guard")

    async def test_monitor_escalates_when_goal_replan_limit_is_reached(self) -> None:
        await self._save_execution(
            execution_id="execution-goal-replan-limit",
            workflow_id="workflow-goal-replan-limit",
            status=ExecutionStatus.COMPLETED,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-replan-limit",
                objective="Stop unproductive replanning",
                status=GoalStatus.ACTIVE,
                execution_ids=["execution-goal-replan-limit"],
                evidence=[{"type": "artifact", "id": "artifact-1"}],
                success_criteria=[{"kind": "artifact", "description": "Evidence exists"}],
                constraints={"supervision_limits": {"max_replan_count": "2"}},
                metadata={
                    "goal_planning": {
                        "active_plan": {"version": 3, "steps": [{"action": "evaluate_evidence"}]},
                        "plan_history": [
                            {"version": 1, "steps": [{"action": "start_workflow", "workflow_id": "workflow-a"}]},
                            {"version": 2, "steps": [{"action": "start_workflow", "workflow_id": "workflow-b"}]},
                        ],
                    },
                    "active_plan": {"version": 3, "steps": [{"action": "evaluate_evidence"}]},
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        finding = result["goal_findings"][0]
        self.assertEqual(finding["category"], "goal_supervision_limit_reached")
        self.assertEqual(finding["evidence"]["goal_id"], goal.id)
        self.assertEqual(finding["evidence"]["replan_count"], 2)
        self.assertEqual(finding["evidence"]["max_replan_count"], 2)
        self.assertTrue(finding["evidence"]["replan_limit_reached"])
        self.assertEqual(finding["evidence"]["active_plan_version"], 3)

    async def test_monitor_uses_agent_timeout_policy_for_idle_activity(self) -> None:
        now = utc_now()
        workflow = self._workflow("workflow-agent-timeout", {"visible_to_main_agent": True})
        workflow.agent_definitions = [
            AgentDefinition(
                id="agent-long-running",
                name="Long Running Agent",
                metadata={"timeout_policy": {"idle_timeout_seconds": 1200, "run_timeout_seconds": 7200}},
            )
        ]
        execution = Execution(
            id="execution-agent-timeout",
            workflow_id=workflow.id,
            runtime_adapter_id="native",
            status=ExecutionStatus.RUNNING,
            started_at=now - timedelta(minutes=20),
            updated_at=now - timedelta(minutes=15),
            last_heartbeat_at=now,
            metadata={
                "runtime_activity": {
                    "last_activity_at": (now - timedelta(minutes=15)).isoformat(),
                    "last_activity_agent_id": "agent-long-running",
                    "last_activity_event_type": "llm.request.created",
                }
            },
        )

        finding = MainAgentWorkflowMonitorService(self.context, settings=self.settings)._stale_execution_finding(
            execution,
            workflow=workflow,
            settings=self.settings,
        )

        self.assertIsNone(finding)

    async def test_monitor_does_not_flag_intentional_waits_as_stale(self) -> None:
        workflow = self._workflow("workflow-intentional-waits", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-paused-wait",
            workflow_id=workflow.id,
            status=ExecutionStatus.PAUSED,
            age_seconds=900,
            metadata={"pending_subagent_input": {"status": "needs_input", "step_id": "step-input"}},
        )
        await self._save_execution(
            execution_id="execution-approval-wait",
            workflow_id=workflow.id,
            status=ExecutionStatus.WAITING_FOR_APPROVAL,
            age_seconds=900,
            metadata={"pending_subagent_approval": {"status": "needs_approval", "step_id": "step-approval"}},
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 0)
        self.assertEqual(result["proposal_count"], 0)
        self.assertEqual(result["approval_request_count"], 0)

    async def test_monitor_requests_steering_for_token_budget_exceeded(self) -> None:
        workflow = self._workflow("workflow-token-supervision", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-token-supervision",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        token_event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-token-supervision",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
                payload_json={
                    "budget": {
                        "scope": "run",
                        "used_tokens": 1200,
                        "budget_tokens": 1000,
                        "usage_ratio": 1.2,
                    }
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(result["steering_request_count"], 1)
        finding = result["findings"][0]
        self.assertEqual(finding["category"], "token_budget_exceeded")
        self.assertEqual(finding["evidence"]["source_event_id"], token_event.id)
        steering = result["steering_requests"][0]
        self.assertEqual(steering["recommended_action"], "request_replan")
        self.assertEqual(steering["category"], "token_budget_exceeded")
        events = await self.context.execution_store.list_events("execution-token-supervision")
        self.assertEqual(
            [event.event_type for event in events],
            [
                ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
                ExecutionEventType.MONITOR_FINDING_CREATED,
                ExecutionEventType.SUPERVISOR_STEERING_REQUESTED,
            ],
        )
        persisted = await self.context.execution_store.get_execution("execution-token-supervision")
        assert persisted is not None
        pending = persisted.metadata["runtime_governance"]["supervision"]["pending_requests"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["recommended_action"], "request_replan")

    async def test_monitor_auto_applies_opted_in_stale_execution_repair(self) -> None:
        workflow = self._workflow(
            "workflow-auto-repair",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "allowed_steering_actions": ["repair_stale_execution"],
                    "auto_apply_steering_actions": ["repair_stale_execution"],
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-auto-repair",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=900,
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(result["findings"][0]["category"], "stale_execution")
        self.assertEqual(result["steering_request_count"], 1)
        self.assertEqual(result["steering_applied_count"], 1)
        applied = result["steering_applied"][0]
        self.assertEqual(applied["applied_action"], "repair_stale_execution")
        self.assertEqual(applied["status"], "applied")
        repaired = await self.context.execution_store.get_execution("execution-auto-repair")
        assert repaired is not None
        self.assertEqual(repaired.status, ExecutionStatus.QUEUED)
        pending = repaired.metadata["runtime_governance"]["supervision"]["pending_requests"]
        self.assertEqual(pending[0]["status"], "applied")
        self.assertEqual(pending[0]["applied_action"], "repair_stale_execution")
        events = await self.context.execution_store.list_events("execution-auto-repair")
        event_types = [event.event_type for event in events]
        self.assertIn(ExecutionEventType.EXECUTION_REPAIRED, event_types)
        self.assertIn(ExecutionEventType.SUPERVISOR_STEERING_APPLIED, event_types)

    async def test_goal_supervisor_repairs_stale_execution_under_guarded_autonomy(self) -> None:
        workflow = self._workflow("workflow-goal-stale-repair", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-goal-stale-repair",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=900,
        )
        goal = await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-stale-repair",
                objective="Recover stale long-running research",
                status=GoalStatus.ACTIVE,
                constraints={"autonomy": "guarded"},
                execution_ids=["execution-goal-stale-repair"],
                success_criteria=[{"kind": "artifact", "description": "Recovered evidence exists"}],
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["goal_finding_count"], 1)
        self.assertEqual(result["goal_findings"][0]["category"], "goal_execution_signal")
        self.assertEqual(result["goal_findings"][0]["evidence"]["signal_category"], "stale_execution")
        self.assertEqual(result["goal_findings"][0]["evidence"]["recommended_action"], "repair_stale_execution")
        self.assertEqual(result["auto_repair_count"], 1)
        auto_repair = result["auto_repairs"][0]
        self.assertEqual(auto_repair["goal_id"], goal.id)
        self.assertEqual(auto_repair["execution_id"], "execution-goal-stale-repair")
        self.assertEqual(auto_repair["policy_decision"]["action"], "repair_stale_execution")
        self.assertFalse(auto_repair["policy_decision"]["requires_approval"])
        repaired = await self.context.execution_store.get_execution("execution-goal-stale-repair")
        assert repaired is not None
        self.assertEqual(repaired.status, ExecutionStatus.QUEUED)
        persisted = await self.context.goal_repo.get(goal.id)
        monitoring = persisted.metadata["main_agent_monitoring"]
        self.assertEqual(monitoring["supervisor_decisions"][0]["action"], "repair_stale_execution")
        self.assertEqual(monitoring["supervisor_actions"][-1]["action"], "repair_stale_execution")
        self.assertEqual(result["approval_request_count"], 0)
        audit_events = [
            event for event in await self.context.graph_projection_event_repo.list_events(limit=100)
            if event.aggregate_type == "goal"
            and event.aggregate_id == goal.id
            and event.event_type == "goal.supervisor_decision.audit_recorded"
        ]
        self.assertEqual(len(audit_events), 1)
        audit_payload = audit_events[0].payload
        self.assertEqual(audit_events[0].source, "main_agent_monitor")
        self.assertEqual(audit_payload["audit"]["action"], "repair_stale_execution")
        self.assertEqual(audit_payload["audit"]["risk"], "medium")
        self.assertTrue(audit_payload["audit"]["allowed_by_policy"])
        self.assertFalse(audit_payload["audit"]["requires_approval"])
        self.assertEqual(audit_payload["relationships"]["execution_ids"], ["execution-goal-stale-repair"])
        self.assertEqual(
            audit_payload["relationships"]["supervisor_decision_ids"],
            [monitoring["supervisor_decisions"][0]["id"]],
        )
        events = await self.context.execution_store.list_events("execution-goal-stale-repair")
        event_types = [event.event_type for event in events]
        self.assertIn(ExecutionEventType.EXECUTION_REPAIRED, event_types)

    async def test_monitor_routes_risky_steering_to_approval(self) -> None:
        await self._save_monitor_approval_context("conversation-steering-approval")
        workflow = self._workflow(
            "workflow-steering-approval",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "allowed_steering_actions": ["request_replan"],
                    "route_steering_requests_to_approval": True,
                    "approval_conversation_id": "conversation-steering-approval",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-steering-approval",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-steering-approval",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
                payload_json={
                    "budget": {
                        "scope": "run",
                        "used_tokens": 1200,
                        "budget_tokens": 1000,
                        "usage_ratio": 1.2,
                    }
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["steering_request_count"], 1)
        self.assertEqual(result["approval_request_count"], 1)
        approval = result["approval_requests"][0]
        self.assertEqual(approval["metadata"]["action"], "supervisor_steering")
        self.assertEqual(approval["metadata"]["recommended_action"], "request_replan")
        self.assertEqual(approval["proposed_payload"]["execution_id"], "execution-steering-approval")
        persisted = await self.context.execution_store.get_execution("execution-steering-approval")
        assert persisted is not None
        pending = persisted.metadata["runtime_governance"]["supervision"]["pending_requests"]
        self.assertEqual(pending[0]["status"], "pending_approval")
        self.assertEqual(pending[0]["approval_request_id"], approval["id"])

    async def test_approved_steering_request_emits_applied_event(self) -> None:
        await self._save_monitor_approval_context("conversation-steering-apply")
        workflow = self._workflow(
            "workflow-steering-apply",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "allowed_steering_actions": ["request_replan"],
                    "route_steering_requests_to_approval": True,
                    "approval_conversation_id": "conversation-steering-apply",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-steering-apply",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-steering-apply",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
                payload_json={"budget": {"scope": "run", "used_tokens": 1200, "budget_tokens": 1000}},
            )
        )
        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        approval_id = result["approval_requests"][0]["id"]

        approved = await ConversationService(self.context).approve_request(
            approval_id,
            actor_user_id="user-1",
            reason="Replan the run",
        )

        self.assertEqual(approved["steering"]["status"], "applied")
        self.assertEqual(approved["steering"]["applied_action"], "request_replan")
        self.assertEqual(approved["steering"]["result"]["status"], "recorded_guidance")
        events = await self.context.execution_store.list_events("execution-steering-apply")
        applied_events = [
            event for event in events if event.event_type == ExecutionEventType.SUPERVISOR_STEERING_APPLIED
        ]
        self.assertEqual(len(applied_events), 1)
        self.assertEqual(applied_events[0].payload["approval_request_id"], approval_id)
        persisted = await self.context.execution_store.get_execution("execution-steering-apply")
        assert persisted is not None
        pending = persisted.metadata["runtime_governance"]["supervision"]["pending_requests"]
        self.assertEqual(pending[0]["status"], "applied")
        self.assertEqual(pending[0]["applied_event_id"], applied_events[0].id)
        self.assertEqual(pending[0]["result"]["status"], "recorded_guidance")

    async def test_approved_steering_request_updates_mutable_workflow_revision(self) -> None:
        await self._save_monitor_approval_context("conversation-steering-mutation")
        workflow = WorkflowDefinition(
            id="workflow-steering-mutation",
            name="Workflow Steering Mutation",
            entrypoint="entry",
            nodes=[],
            edges=[],
            task_definitions=[
                TaskDefinition(
                    id="task-steering",
                    name="Steering Task",
                    description="Finish the governed run.",
                    instructions="Complete the current task.",
                    expected_output="A final result.",
                )
            ],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allowed_steering_actions": ["request_replan"],
                    "route_steering_requests_to_approval": True,
                    "approval_conversation_id": "conversation-steering-mutation",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        replace_mock = AsyncMock(return_value=["replacement-execution"])
        self.context.control_plane.replace_active_executions_for_workflow_revision = replace_mock
        await self._save_execution(
            execution_id="execution-steering-mutation",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-steering-mutation",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
                payload_json={"budget": {"scope": "run", "used_tokens": 1200, "budget_tokens": 1000}},
            )
        )
        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        approval_id = result["approval_requests"][0]["id"]
        self.assertEqual(
            result["approval_requests"][0]["proposed_payload"]["operator_parameter_schema"]["action"],
            "request_replan",
        )
        schema_fields = result["approval_requests"][0]["proposed_payload"]["operator_parameter_schema"]["fields"]
        self.assertIn("instructions", {field["name"] for field in schema_fields})

        approved = await ConversationService(self.context).approve_request(
            approval_id,
            actor_user_id="user-1",
            reason="Apply replan guidance",
            steering_parameters={
                "target_task_id": "task-steering",
                "instructions": "Prioritize a lower-token validation path before continuing.",
            },
        )

        steering_result = approved["steering"]["result"]
        self.assertEqual(steering_result["status"], "workflow_updated")
        self.assertEqual(steering_result["workflow_revision"], 2)
        self.assertEqual(steering_result["replaced_execution_ids"], ["replacement-execution"])
        persisted_workflow = await self.context.workflow_repo.get(workflow.id)
        assert persisted_workflow is not None
        self.assertEqual(persisted_workflow.versioning.revision, 2)
        self.assertIn("Supervisor steering", persisted_workflow.task_definitions[0].instructions or "")
        self.assertIn("lower-token validation path", persisted_workflow.task_definitions[0].instructions or "")
        self.assertEqual(
            persisted_workflow.metadata["main_agent_monitoring"]["last_steering_approval_request_id"],
            approval_id,
        )
        persisted_execution = await self.context.execution_store.get_execution("execution-steering-mutation")
        assert persisted_execution is not None
        supervision = persisted_execution.metadata["runtime_governance"]["supervision"]
        self.assertEqual(supervision["pending_requests"][0]["result"]["status"], "workflow_updated")
        self.assertEqual(
            supervision["pending_requests"][0]["result"]["operator_parameters"]["target_task_id"],
            "task-steering",
        )
        self.assertEqual(supervision["last_applied_guidance"]["workflow_revision"], 2)
        replace_mock.assert_awaited_once()

    async def test_steering_approval_rejects_invalid_operator_task_target(self) -> None:
        await self._save_monitor_approval_context("conversation-steering-invalid-target")
        workflow = WorkflowDefinition(
            id="workflow-steering-invalid-target",
            name="Workflow Steering Invalid Target",
            entrypoint="entry",
            nodes=[],
            edges=[],
            task_definitions=[
                TaskDefinition(
                    id="task-steering",
                    name="Steering Task",
                    description="Finish the governed run.",
                    instructions="Complete the current task.",
                    expected_output="A final result.",
                )
            ],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allowed_steering_actions": ["request_replan"],
                    "route_steering_requests_to_approval": True,
                    "approval_conversation_id": "conversation-steering-invalid-target",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-steering-invalid-target",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-steering-invalid-target",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
                payload_json={"budget": {"scope": "run", "used_tokens": 1200, "budget_tokens": 1000}},
            )
        )
        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        approval_id = result["approval_requests"][0]["id"]

        with self.assertRaisesRegex(ConversationApprovalStateError, "target_task_id"):
            await ConversationService(self.context).approve_request(
                approval_id,
                actor_user_id="user-1",
                reason="Apply replan guidance",
                steering_parameters={
                    "target_task_id": "missing-task",
                    "instructions": "Apply this only if the target is valid.",
                },
            )

        approval = await self.context.conversation_approval_repo.get(approval_id)
        assert approval is not None
        self.assertEqual(approval.status.value, "pending")

    async def test_steering_approval_rejects_invalid_max_iterations_parameter(self) -> None:
        await self._save_monitor_approval_context("conversation-steering-invalid-max")
        workflow = WorkflowDefinition(
            id="workflow-steering-invalid-max",
            name="Workflow Steering Invalid Max",
            entrypoint="entry",
            nodes=[],
            edges=[],
            agent_definitions=[
                AgentDefinition(
                    id="agent-steering",
                    name="Steering Agent",
                    instructions="Run a governed task.",
                    tool_ids=[],
                )
            ],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allowed_steering_actions": ["lower_max_iterations"],
                    "route_steering_requests_to_approval": True,
                    "approval_conversation_id": "conversation-steering-invalid-max",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-steering-invalid-max",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        approval = await self.context.conversation_approval_repo.create(
            ApprovalRequest(
                approval_type=ApprovalType.OTHER,
                target_type=ApprovalTargetType.WORKFLOW,
                target_id=workflow.id,
                requested_by_agent_id="main-agent",
                conversation_id="conversation-steering-invalid-max",
                origin_message_id="message-steering-invalid-max",
                summary="Supervisor steering requested: lower_max_iterations",
                proposed_payload={
                    "workflow_id": workflow.id,
                    "execution_id": "execution-steering-invalid-max",
                    "recommended_action": "lower_max_iterations",
                    "operator_parameter_schema": {
                        "action": "lower_max_iterations",
                        "fields": [
                            {"name": "target_agent_id", "type": "select"},
                            {"name": "max_iterations", "type": "number", "min": 1, "max": 20},
                        ],
                    },
                },
                metadata={
                    "action": "supervisor_steering",
                    "recommended_action": "lower_max_iterations",
                },
            )
        )

        with self.assertRaisesRegex(ConversationApprovalStateError, "max_iterations"):
            await ConversationService(self.context).approve_request(
                approval.id,
                actor_user_id="user-1",
                reason="Lower iteration ceiling",
                steering_parameters={
                    "target_agent_id": "agent-steering",
                    "max_iterations": 99,
                },
            )

        persisted = await self.context.conversation_approval_repo.get(approval.id)
        assert persisted is not None
        self.assertEqual(persisted.status.value, "pending")

    async def test_rejected_steering_request_updates_pending_status_without_applied_event(self) -> None:
        await self._save_monitor_approval_context("conversation-steering-reject")
        workflow = self._workflow(
            "workflow-steering-reject",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "allowed_steering_actions": ["request_replan"],
                    "route_steering_requests_to_approval": True,
                    "approval_conversation_id": "conversation-steering-reject",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-steering-reject",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-steering-reject",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
                payload_json={"budget": {"scope": "run", "used_tokens": 1200, "budget_tokens": 1000}},
            )
        )
        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        approval_id = result["approval_requests"][0]["id"]

        rejected = await ConversationService(self.context).reject_request(
            approval_id,
            actor_user_id="user-1",
            reason="Do not replan",
        )

        self.assertEqual(rejected["steering"]["status"], "rejected")
        events = await self.context.execution_store.list_events("execution-steering-reject")
        self.assertFalse(
            any(event.event_type == ExecutionEventType.SUPERVISOR_STEERING_APPLIED for event in events)
        )
        persisted = await self.context.execution_store.get_execution("execution-steering-reject")
        assert persisted is not None
        pending = persisted.metadata["runtime_governance"]["supervision"]["pending_requests"]
        self.assertEqual(pending[0]["status"], "rejected")
        self.assertEqual(pending[0]["approval_decision_reason"], "Do not replan")

    async def test_monitor_detects_subagent_blocker_and_respects_steering_policy(self) -> None:
        workflow = self._workflow(
            "workflow-subagent-supervision",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "allowed_steering_actions": ["request_replan"],
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-subagent-supervision",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        blocker_event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-subagent-supervision",
                workflow_id=workflow.id,
                agent_id="agent-research",
                event_type=ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
                payload_json={
                    "status": "blocked",
                    "current_task": "Gather deployment window",
                    "blocker": "Missing deployment approval window",
                    "confidence": 0.6,
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(result["steering_request_count"], 0)
        self.assertEqual(result["findings"][0]["category"], "subagent_blocked")
        self.assertEqual(result["findings"][0]["evidence"]["source_event_id"], blocker_event.id)
        events = await self.context.execution_store.list_events("execution-subagent-supervision")
        self.assertEqual(events[-1].event_type, ExecutionEventType.MONITOR_FINDING_CREATED)

    async def test_monitor_detects_repeated_subagent_progress_without_completion(self) -> None:
        class FakeGraphReader:
            def __init__(self):
                self.calls = []

            async def get_neighborhood(self, node_id, **kwargs):
                self.calls.append(("get_neighborhood", {"node_id": node_id, **kwargs}))
                return GraphReadDocument(
                    nodes=[
                        GraphReadNode(
                            id="task-research",
                            type="Task",
                            labels=["Task"],
                            properties={"name": "Research Task"},
                        ),
                        GraphReadNode(
                            id="decision-keep-context-small",
                            type="Decision",
                            labels=["Decision"],
                            properties={"summary": "Keep graph steering context concise."},
                        ),
                        GraphReadNode(
                            id="error-prior-timeout",
                            type="Error",
                            labels=["Error"],
                            properties={"message": "Prior deployment research timed out."},
                        ),
                    ],
                    edges=[],
                )

        previous = {
            key: os.environ.get(key)
            for key in (
                "GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED",
                "GRAPH_CONTEXT_SUBAGENT_STEERING_ENABLED",
            )
        }
        os.environ["GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED"] = "true"
        os.environ["GRAPH_CONTEXT_SUBAGENT_STEERING_ENABLED"] = "true"
        reset_settings_cache()
        graph_reader = FakeGraphReader()
        self.context.graph_read_service = graph_reader
        workflow = self._workflow(
            "workflow-subagent-repeated-progress",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "allowed_steering_actions": ["request_replan"],
                },
            },
        )
        workflow.agent_definitions = [
            AgentDefinition(
                id="agent-research",
                name="Research Agent",
                graph_context=GraphContextSettings(
                    enabled=True,
                    auto_retrieval_enabled=True,
                    subagent_steering_enabled=True,
                    include_events=True,
                ),
            )
        ]
        workflow.task_definitions = [
            TaskDefinition(
                id="task-approval",
                name="Approval Task",
                description="Collect deployment approval.",
                agent_id="agent-research",
                expected_output="Approved deployment window",
            ),
            TaskDefinition(
                id="task-research",
                name="Research Task",
                description="Research deployment notes",
                agent_id="agent-research",
                expected_output="Deployment notes with decisions and next steps",
                depends_on_task_ids=["task-approval"],
            )
        ]
        workflow.nodes = [
            WorkflowNodeDefinition(
                id="node-approval",
                name="Approval Node",
                node_type="task",
                task_id="task-approval",
                agent_id="agent-research",
            ),
            WorkflowNodeDefinition(
                id="node-research",
                name="Research Node",
                node_type="task",
                task_id="task-research",
                agent_id="agent-research",
            ),
        ]
        workflow.edges = [
            WorkflowEdgeDefinition(
                id="edge-approval-research",
                source_node_id="node-approval",
                target_node_id="node-research",
            )
        ]
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-subagent-repeated-progress",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-subagent-repeated-progress",
                workflow_id=workflow.id,
                agent_id="agent-research",
                task_id="task-research",
                event_type=ExecutionEventType.TOOL_CALL_FAILED,
                payload_json={
                    "status": "failed",
                    "error": "Prior search timed out",
                    "tool_name": "search",
                },
                sequence=1,
            )
        )
        for index in range(3):
            await self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id="execution-subagent-repeated-progress",
                    workflow_id=workflow.id,
                    agent_id="agent-research",
                    task_id="task-research",
                    event_type=ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
                    payload_json={
                        "status": "running",
                        "current_task": "Research deployment notes",
                        "progress_percent": 20 + index * 5,
                        "next_action": "Continue research",
                    },
                    sequence=index + 2,
                )
            )

        try:
            result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

            self.assertEqual(result["finding_count"], 1)
            self.assertEqual(result["steering_request_count"], 1)
            self.assertEqual(result["findings"][0]["category"], "subagent_repeated_progress")
            evidence = result["findings"][0]["evidence"]
            self.assertEqual(evidence["anchor_type"], "task")
            self.assertEqual(evidence["anchor_id"], "task-research")
            self.assertEqual(evidence["intent"], "steer")
            self.assertEqual(evidence["progress_update_count"], 3)
            self.assertEqual(evidence["expected_output"], "Deployment notes with decisions and next steps")
            self.assertEqual(evidence["dependencies"][0]["task_id"], "task-approval")
            self.assertEqual(evidence["dependencies"][0]["expected_output"], "Approved deployment window")
            self.assertEqual(evidence["prior_failures"][0]["error"], "Prior search timed out")
            self.assertIn("Keep graph steering context concise.", str(evidence["linked_decisions"]))
            self.assertEqual(evidence["graph_context"]["query_meta"]["intent"], "steer")
            self.assertEqual(graph_reader.calls[0][1]["node_id"], "task-research")
            self.assertEqual(result["steering_requests"][0]["recommended_action"], "request_replan")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reset_settings_cache()

    async def test_monitor_skips_excluded_subagent_and_task_events(self) -> None:
        workflow = self._workflow(
            "workflow-subagent-exclusions",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "excluded_subagent_ids": ["agent-excluded"],
                    "excluded_task_ids": ["task-excluded"],
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-subagent-exclusions",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-subagent-exclusions",
                workflow_id=workflow.id,
                agent_id="agent-excluded",
                task_id="task-monitored",
                event_type=ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
                payload_json={
                    "status": "blocked",
                    "blocker": "Excluded agent blocker",
                },
            )
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-subagent-exclusions",
                workflow_id=workflow.id,
                agent_id="agent-monitored",
                task_id="task-excluded",
                event_type=ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
                payload_json={
                    "status": "blocked",
                    "blocker": "Excluded task blocker",
                },
            )
        )
        monitored_event = await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-subagent-exclusions",
                workflow_id=workflow.id,
                agent_id="agent-monitored",
                task_id="task-monitored",
                event_type=ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
                payload_json={
                    "status": "blocked",
                    "blocker": "Monitored blocker",
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(result["findings"][0]["category"], "subagent_blocked")
        self.assertEqual(result["findings"][0]["evidence"]["source_event_id"], monitored_event.id)

    async def test_monitor_groups_repeated_tool_failures_into_one_governance_finding(self) -> None:
        workflow = self._workflow("workflow-tool-supervision", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-tool-supervision",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        for index in range(3):
            await self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id="execution-tool-supervision",
                    workflow_id=workflow.id,
                    event_type=ExecutionEventType.TOOL_CALL_FAILED,
                    payload_json={
                        "tool_name": "search",
                        "error": f"timeout {index}",
                    },
                )
            )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(result["findings"][0]["category"], "repeated_tool_call_failure")
        self.assertEqual(len(result["findings"][0]["evidence"]["source_event_ids"]), 3)
        self.assertEqual(result["steering_request_count"], 1)
        self.assertEqual(result["steering_requests"][0]["recommended_action"], "request_human_review")

    async def test_monitor_skips_active_main_agent_default_workflow_unless_explicitly_allowed(self) -> None:
        workflow = self._workflow("main-workflow", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self.context.main_agent_profile_repo.save(
            MainAgentProfile(
                id="main-agent-profile",
                name="Main Agent",
                agent_id="main-agent",
                default_workflow_id=workflow.id,
            )
        )
        await self._save_execution(
            execution_id="execution-main-workflow-self",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
        )

        skipped = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(skipped["finding_count"], 0)
        self.assertEqual(skipped["skipped"], 1)
        await self.context.workflow_repo.update(
            workflow.id,
            {
                "metadata": {
                    **workflow.metadata,
                    "main_agent_monitoring": {"allow_self_monitoring": True},
                }
            },
        )

        allowed = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(allowed["finding_count"], 1)
        self.assertEqual(allowed["findings"][0]["execution_id"], "execution-main-workflow-self")

    async def test_monitor_uses_strict_level_for_scheduled_workflows_without_explicit_level(self) -> None:
        workflow = self._workflow("workflow-scheduled-monitoring", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self.context.schedule_repo.create(
            ScheduleDefinition(
                name="Scheduled monitoring workflow",
                workflow_id=workflow.id,
                trigger_type=ScheduleType.CRON,
                trigger_config={"cron": "* * * * *"},
            )
        )
        await self._save_execution(
            execution_id="execution-scheduled-strict",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(result["scanned_by_level"], {"strict": 1})

    async def test_monitor_retention_purges_old_unlinked_findings_only(self) -> None:
        workflow = self._workflow("workflow-retention", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        execution = await self._save_execution(
            execution_id="execution-retention",
            workflow_id=workflow.id,
            status=ExecutionStatus.COMPLETED,
            age_seconds=70 * 86400,
            output_payload={"final_output": "Old result"},
        )
        old_timestamp = utc_now() - timedelta(days=61)
        recent_timestamp = utc_now() - timedelta(days=10)
        unlinked = await self.context.execution_store.save_event(
            ExecutionEvent(
                id="finding-unlinked",
                execution_id=execution.id,
                workflow_id=workflow.id,
                event_type=ExecutionEventType.MONITOR_FINDING_CREATED,
                timestamp=old_timestamp,
                payload_json={"category": "failed_execution"},
                metadata={"source": "main_agent_monitor"},
            )
        )
        proposal_linked = await self.context.execution_store.save_event(
            ExecutionEvent(
                id="finding-proposal-linked",
                execution_id=execution.id,
                workflow_id=workflow.id,
                event_type=ExecutionEventType.MONITOR_FINDING_CREATED,
                timestamp=old_timestamp,
                payload_json={"category": "tool_failure"},
                metadata={"source": "main_agent_monitor"},
            )
        )
        approval_linked = await self.context.execution_store.save_event(
            ExecutionEvent(
                id="finding-approval-linked",
                execution_id=execution.id,
                workflow_id=workflow.id,
                event_type=ExecutionEventType.MONITOR_FINDING_CREATED,
                timestamp=old_timestamp,
                payload_json={"category": "missing_validation"},
                metadata={"source": "main_agent_monitor"},
            )
        )
        recent = await self.context.execution_store.save_event(
            ExecutionEvent(
                id="finding-recent",
                execution_id=execution.id,
                workflow_id=workflow.id,
                event_type=ExecutionEventType.MONITOR_FINDING_CREATED,
                timestamp=recent_timestamp,
                payload_json={"category": "completed_execution"},
                metadata={"source": "main_agent_monitor"},
            )
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                id="proposal-protects-finding",
                execution_id=execution.id,
                workflow_id=workflow.id,
                event_type=ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
                timestamp=old_timestamp,
                payload_json={"finding": {"evidence": [{"event_id": proposal_linked.id}]}},
                metadata={"source": "main_agent_monitor"},
            )
        )
        await self.context.conversation_approval_repo.create(
            ApprovalRequest(
                approval_type=ApprovalType.WORKFLOW_UPDATE,
                target_type=ApprovalTargetType.WORKFLOW,
                target_id=workflow.id,
                requested_by_agent_id="main_agent_monitor",
                conversation_id="conversation-retention",
                origin_message_id="message-retention",
                summary="Protected monitor proposal",
                metadata={"finding": {"evidence": [{"event_id": approval_linked.id}]}},
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        remaining_ids = {event.id for event in await self.context.execution_store.list_events(execution.id)}
        self.assertNotIn(unlinked.id, remaining_ids)
        self.assertIn(proposal_linked.id, remaining_ids)
        self.assertIn(approval_linked.id, remaining_ids)
        self.assertIn(recent.id, remaining_ids)
        self.assertEqual(result["retention"]["retention_days"], 60)
        self.assertEqual(result["retention"]["purged_finding_count"], 1)

    async def test_monitor_retention_trims_goal_monitoring_records(self) -> None:
        old_recorded_at = (utc_now() - timedelta(days=61)).isoformat()
        recent_recorded_at = (utc_now() - timedelta(days=10)).isoformat()
        await self.context.conversation_approval_repo.create(
            ApprovalRequest(
                id="approval-goal-retention-pending",
                approval_type=ApprovalType.OTHER,
                target_type=ApprovalTargetType.OTHER,
                target_id="goal-retention",
                requested_by_agent_id="main_agent_monitor",
                conversation_id="conversation-goal-retention",
                origin_message_id="message-goal-retention",
                summary="Pending goal supervisor approval",
            )
        )
        await self.context.goal_repo.create(
            GoalDefinition(
                id="goal-retention",
                objective="Retain only useful goal monitor records",
                status=GoalStatus.COMPLETED,
                metadata={
                    "main_agent_monitoring": {
                        "findings": [
                            {"dedupe_key": "old-finding", "recorded_at": old_recorded_at, "finding": {"category": "stalled_goal"}},
                            {"dedupe_key": "recent-finding", "recorded_at": recent_recorded_at, "finding": {"category": "stalled_goal"}},
                        ],
                        "supervisor_actions": [
                            {"id": "old-action", "recorded_at": old_recorded_at, "action": "record_supervisor_finding"},
                            {"id": "last-action", "recorded_at": old_recorded_at, "action": "request_human_approval"},
                        ],
                        "last_supervisor_action": {
                            "id": "last-action",
                            "recorded_at": old_recorded_at,
                            "action": "request_human_approval",
                        },
                        "supervisor_decisions": [
                            {"id": "old-decision", "recorded_at": old_recorded_at, "action": "request_replan"}
                        ],
                        "approval_requests": [
                            {
                                "approval_request_id": "approval-goal-retention-resolved",
                                "recorded_at": old_recorded_at,
                                "recommended_action": "request_replan",
                            },
                            {
                                "approval_request_id": "approval-goal-retention-pending",
                                "recorded_at": old_recorded_at,
                                "recommended_action": "request_replan",
                            },
                        ],
                        "last_approval_request": {
                            "approval_request_id": "approval-goal-retention-pending",
                            "recorded_at": old_recorded_at,
                            "recommended_action": "request_replan",
                        },
                    }
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        persisted = await self.context.goal_repo.get("goal-retention")
        monitoring = persisted.metadata["main_agent_monitoring"]
        self.assertEqual([item["dedupe_key"] for item in monitoring["findings"]], ["recent-finding"])
        self.assertEqual([item["id"] for item in monitoring["supervisor_actions"]], ["last-action"])
        self.assertEqual(monitoring["supervisor_decisions"], [])
        self.assertEqual(
            [item["approval_request_id"] for item in monitoring["approval_requests"]],
            ["approval-goal-retention-pending"],
        )
        self.assertEqual(result["retention"]["purged_goal_finding_count"], 1)
        self.assertEqual(result["retention"]["purged_goal_supervisor_action_count"], 1)
        self.assertEqual(result["retention"]["purged_goal_supervisor_decision_count"], 1)
        self.assertEqual(result["retention"]["purged_goal_approval_request_count"], 1)

    async def test_execution_api_exposes_stale_classification(self) -> None:
        workflow = self._workflow("workflow-api-stale", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-api-stale",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
        )

        with TestClient(create_app(self.context)) as client:
            detail = client.get("/executions/execution-api-stale")
            active = client.get("/executions/active")

        self.assertEqual(detail.status_code, 200)
        stale = detail.json()["execution"]["stale_classification"]
        self.assertEqual(stale["is_stale"], True)
        self.assertEqual(stale["status"], "running")
        self.assertEqual(stale["stale_after_seconds"], 300)
        active_items = active.json()["items"]
        self.assertEqual(len(active_items), 1)
        self.assertEqual(active_items[0]["stale_classification"]["is_stale"], True)

    async def test_monitor_skips_explicitly_exempt_workflow(self) -> None:
        workflow = self._workflow(
            "workflow-exempt",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"enabled": False},
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-exempt",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 0)
        self.assertEqual(result["skipped"], 1)

    async def test_monitor_reports_recent_failed_execution(self) -> None:
        workflow = self._workflow("workflow-failing", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-failed",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 1)
        finding = result["findings"][0]
        self.assertEqual(finding["category"], "failed_execution")
        self.assertEqual(finding["evidence"]["error"], "tool failed")

    async def test_monitor_proposes_improvement_only_when_workflow_allows_it(self) -> None:
        workflow = self._workflow(
            "workflow-proposals",
            {
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {"allow_improvement_proposals": True},
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-proposal-failed",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(result["proposal_count"], 1)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["workflow_id"], workflow.id)
        self.assertEqual(proposal["finding"]["category"], "tool_failure")
        self.assertEqual(proposal["finding"]["evidence"][0]["execution_id"], "execution-proposal-failed")
        self.assertEqual(proposal["diagnosis"]["finding_category"], "tool_failure")
        self.assertEqual(proposal["diagnosis"]["affected_workflow_fields"], proposal["proposed_change"]["affected_fields"])
        self.assertEqual(proposal["diagnosis"]["evidence_ids"][0]["execution_id"], "execution-proposal-failed")
        self.assertEqual(proposal["quality_signals"]["execution_count"], 1)
        self.assertEqual(proposal["quality_signals"]["failure_count"], 1)
        self.assertEqual(proposal["proposed_change"]["type"], "task_instruction_update")
        self.assertEqual(
            [item["kind"] for item in proposal["proposed_change"]["recommendations"]],
            [
                "clarify_ambiguous_instructions",
                "add_success_criteria",
                "add_expected_output_shape",
                "add_escalation_instructions",
                "tighten_tool_use_boundaries",
                "add_evidence_requirements",
            ],
        )
        self.assertEqual(
            [item["kind"] for item in proposal["proposed_change"]["graph_task_recommendations"]],
            [
                "split_overloaded_tasks",
                "merge_redundant_tasks",
                "add_verification_tasks",
                "add_recovery_tasks",
                "add_human_approval_points",
                "reorder_dependency_tasks",
            ],
        )
        self.assertEqual(
            [item["kind"] for item in proposal["proposed_change"]["agent_recommendations"]],
            [
                "adjust_agent_responsibility",
                "reduce_agent_overlap",
                "switch_model_profile_strength",
                "add_or_remove_tools",
                "tighten_agent_failure_reporting",
            ],
        )
        self.assertEqual(
            [item["kind"] for item in proposal["proposed_change"]["memory_recommendations"]],
            [
                "create_or_update_workflow_scoped_memories",
                "suppress_duplicate_memories",
                "mark_stale_memories",
            ],
        )
        self.assertEqual(
            [item["kind"] for item in proposal["proposed_change"]["tool_recommendations"]],
            [
                "propose_missing_tool_contracts",
                "improve_tool_schemas",
                "add_approval_gates",
                "narrow_tool_permissions",
                "flag_flaky_tools_for_repair",
            ],
        )
        self.assertEqual(
            [item["kind"] for item in proposal["proposed_change"]["validation_recommendations"]],
            [
                "require_deterministic_checks",
                "require_artifact_assertions",
                "require_schema_checks",
                "require_command_evidence",
                "request_evaluation_agent_review",
            ],
        )
        self.assertEqual(proposal["proposed_change"]["tool_assignment_change_approval"]["approval_required"], True)
        self.assertIn(
            "credential_access",
            proposal["proposed_change"]["tool_assignment_change_approval"]["restricted_capabilities"],
        )
        self.assertEqual(proposal["proposed_change"]["memory_write_approval"]["approval_required"], True)
        self.assertIn(
            "workflow.metadata.persistent_run_summary.enabled",
            proposal["proposed_change"]["memory_write_approval"]["affected_fields"],
        )
        self.assertEqual(proposal["proposed_change"]["review_requirements"]["level"], "standard")
        self.assertEqual(proposal["proposed_change"]["review_requirements"]["required"], False)
        self.assertEqual(proposal["requires_human_permission"], True)
        self.assertEqual(proposal["restart_active_executions"], False)
        events = await self.context.execution_store.list_events("execution-proposal-failed")
        self.assertEqual(
            [event.event_type for event in events],
            [
                ExecutionEventType.MONITOR_FINDING_CREATED,
                ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
            ],
        )
        approval_template = events[-1].payload["approval_request_template"]
        self.assertEqual(approval_template["approval_type"], "workflow_update")
        self.assertEqual(approval_template["metadata"]["source"], "main_agent_monitor")
        self.assertEqual(approval_template["metadata"]["requires_human_permission"], True)
        metrics = self.context.runtime_operations.snapshot_dict()
        self.assertEqual(metrics["counters"]["main_agent_monitor.improvement_proposals"], 1)
        persisted = await self.context.workflow_repo.get(workflow.id)
        assert persisted is not None
        self.assertEqual(persisted.model_dump(mode="json"), workflow.model_dump(mode="json"))
        approvals = await self.context.conversation_approval_repo.list()
        self.assertEqual(approvals, [])

    async def test_monitor_routes_opted_in_improvement_proposal_to_workflow_update_approval(self) -> None:
        await self._save_monitor_approval_context()
        workflow = WorkflowDefinition(
            id="workflow-monitor-approval",
            name="Workflow Monitor Approval",
            entrypoint="entry",
            nodes=[],
            edges=[],
            task_definitions=[
                TaskDefinition(
                    id="task-final",
                    name="Final Task",
                    description="Finish the work.",
                    instructions="Do the work.",
                    expected_output="A result.",
                )
            ],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allow_improvement_proposals": True,
                    "route_improvement_proposals_to_approval": True,
                    "approval_conversation_id": "conversation-monitor-approval",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-monitor-approval",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["proposal_count"], 1)
        self.assertEqual(result["approval_request_count"], 1)
        approvals = await self.context.conversation_approval_repo.list_by_conversation("conversation-monitor-approval")
        self.assertEqual(len(approvals), 1)
        approval = approvals[0]
        self.assertEqual(approval.approval_type.value, "workflow_update")
        self.assertEqual(approval.metadata["source"], "main_agent_monitor")
        self.assertEqual(approval.metadata["proposal_kind"], "workflow_improvement")
        self.assertEqual(approval.metadata["current_revision"], 1)
        self.assertEqual(approval.metadata["expected_replacement_revision"], 2)
        self.assertEqual(approval.proposed_payload["current_revision"], 1)
        self.assertEqual(approval.proposed_payload["expected_replacement_revision"], 2)
        self.assertEqual(approval.proposed_payload["restart_active_executions"], False)
        self.assertEqual(approval.proposed_payload["risk"], approval.metadata["risk"])
        self.assertEqual(approval.proposed_payload["validation_plan"], approval.metadata["validation_plan"])
        self.assertEqual(approval.proposed_payload["rollback_plan"], approval.metadata["rollback_plan"])
        self.assertEqual(approval.proposed_payload["evidence"][0]["event_id"], approval.metadata["finding"]["evidence"][0]["event_id"])
        self.assertEqual(approval.proposed_payload["diagnosis"], approval.metadata["diagnosis"])
        self.assertEqual(approval.proposed_payload["quality_signals"], approval.metadata["quality_signals"])
        self.assertEqual(approval.metadata["strong_review_required"], False)
        self.assertEqual(approval.metadata["review_requirements"]["level"], "standard")
        self.assertEqual(approval.proposed_payload["strong_review_required"], False)
        self.assertEqual(
            approval.proposed_payload["tool_assignment_change_approval"],
            approval.metadata["tool_assignment_change_approval"],
        )
        self.assertEqual(approval.proposed_payload["tool_assignment_change_approval"]["approval_required"], True)
        self.assertEqual(approval.proposed_payload["memory_write_approval"], approval.metadata["memory_write_approval"])
        self.assertEqual(approval.proposed_payload["memory_write_approval"]["approval_required"], True)
        self.assertEqual(
            [item["kind"] for item in approval.proposed_payload["patch"]["recommendations"]],
            [
                "clarify_ambiguous_instructions",
                "add_success_criteria",
                "add_expected_output_shape",
                "add_escalation_instructions",
                "tighten_tool_use_boundaries",
                "add_evidence_requirements",
            ],
        )
        self.assertEqual(
            [item["kind"] for item in approval.proposed_payload["patch"]["graph_task_recommendations"]],
            [
                "split_overloaded_tasks",
                "merge_redundant_tasks",
                "add_verification_tasks",
                "add_recovery_tasks",
                "add_human_approval_points",
                "reorder_dependency_tasks",
            ],
        )

        self.assertEqual(
            [item["kind"] for item in approval.proposed_payload["patch"]["agent_recommendations"]],
            [
                "adjust_agent_responsibility",
                "reduce_agent_overlap",
                "switch_model_profile_strength",
                "add_or_remove_tools",
                "tighten_agent_failure_reporting",
            ],
        )
        self.assertEqual(
            [item["kind"] for item in approval.proposed_payload["patch"]["memory_recommendations"]],
            [
                "create_or_update_workflow_scoped_memories",
                "suppress_duplicate_memories",
                "mark_stale_memories",
            ],
        )
        self.assertEqual(
            [item["kind"] for item in approval.proposed_payload["patch"]["tool_recommendations"]],
            [
                "propose_missing_tool_contracts",
                "improve_tool_schemas",
                "add_approval_gates",
                "narrow_tool_permissions",
                "flag_flaky_tools_for_repair",
            ],
        )
        self.assertEqual(
            [item["kind"] for item in approval.proposed_payload["patch"]["validation_recommendations"]],
            [
                "require_deterministic_checks",
                "require_artifact_assertions",
                "require_schema_checks",
                "require_command_evidence",
                "request_evaluation_agent_review",
            ],
        )
        proposed_task = approval.proposed_payload["workflow"]["task_definitions"][0]
        self.assertIn("success criteria", proposed_task["instructions"])
        self.assertIn("allowed tool actions", proposed_task["instructions"])
        self.assertIn("escalation", proposed_task["expected_output"])
        self.assertIn("validation evidence", approval.proposed_payload["workflow"]["task_definitions"][0]["expected_output"])
        persisted = await self.context.workflow_repo.get(workflow.id)
        assert persisted is not None
        self.assertEqual(persisted.task_definitions[0].expected_output, "A result.")

        messages = await self.context.conversation_message_repo.list_by_conversation("conversation-monitor-approval")
        self.assertEqual([message.message_type.value for message in messages], ["system_note", "workflow_update_proposal"])

    async def test_dispatch_monitor_proposal_to_main_agent_uses_profile_monitor_conversation(self) -> None:
        await self.context.conversation_repo.create(
            Conversation(
                id="conversation-main-monitor",
                created_by_user_id="user-1",
                main_agent_profile_id="main-agent-profile",
            )
        )
        await self.context.main_agent_profile_repo.save(
            MainAgentProfile(
                id="main-agent-profile",
                name="Main Agent",
                agent_id="main-agent",
                default_workflow_id="main-workflow",
                metadata={
                    "main_agent_monitoring": {
                        "approval_conversation_id": "conversation-main-monitor",
                    }
                },
            )
        )
        workflow = WorkflowDefinition(
            id="workflow-monitor-dispatch",
            name="Workflow Monitor Dispatch",
            entrypoint="entry",
            nodes=[],
            edges=[],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allow_improvement_proposals": True,
                    "route_improvement_proposals_to_approval": False,
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-monitor-dispatch",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )
        proposal_event = await self.context.execution_store.save_event(
            ExecutionEvent(
                id="proposal-monitor-dispatch",
                execution_id="execution-monitor-dispatch",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
                payload_json={
                    "summary": "Tighten validation instructions.",
                    "expected_benefit": "Reduce repeated failed runs.",
                    "risk": "Low",
                    "validation_plan": "Run the workflow once after the change.",
                    "rollback_plan": "Restore the previous revision.",
                    "finding": {
                        "evidence": [{"event_id": "finding-1"}],
                    },
                },
            )
        )

        with patch("app.services.conversations.core.ConversationService.post_message", new_callable=AsyncMock) as post_message:
            post_message.return_value = {
                "message": {
                    "id": "message-monitor-dispatch",
                    "conversation_id": "conversation-main-monitor",
                },
                "stream_url": "/conversations/conversation-main-monitor/stream",
            }
            result = await WorkflowService(self.context).dispatch_monitor_proposal_to_main_agent(
                workflow.id,
                proposal_event.id,
                actor_user_id="user-1",
                operator_note="Keep the approval gates intact and avoid broadening tool scope.",
            )

        self.assertEqual(result["conversation_id"], "conversation-main-monitor")
        self.assertEqual(result["proposal_event_id"], proposal_event.id)
        post_message.assert_awaited_once()
        dispatched_payload = post_message.await_args.args[1]
        self.assertEqual(post_message.await_args.args[0], "conversation-main-monitor")
        self.assertEqual(dispatched_payload["response_mode"], "async")
        message = dispatched_payload["message"]
        self.assertEqual(message["metadata"]["monitor_proposal_event_id"], proposal_event.id)
        self.assertEqual(
            message["metadata"]["operator_note"],
            "Keep the approval gates intact and avoid broadening tool scope.",
        )
        self.assertEqual(message["metadata"]["page_context"]["selection"]["workflowId"], workflow.id)
        self.assertEqual(
            message["metadata"]["assistant_providers"]["providers"][0]["selection"]["workflowId"],
            workflow.id,
        )
        self.assertIn("Review monitor improvement proposal", message["plain_text"])
        self.assertIn(workflow.id, message["plain_text"])
        self.assertIn("Operator note: Keep the approval gates intact", message["plain_text"])

    async def test_dispatch_monitor_proposal_creates_conversation_when_monitor_route_is_missing(self) -> None:
        await self.context.main_agent_profile_repo.save(
            MainAgentProfile(
                id="main-agent-profile",
                name="Main Agent",
                agent_id="main-agent",
                default_workflow_id="main-workflow",
            )
        )
        workflow = WorkflowDefinition(
            id="workflow-monitor-dispatch-missing",
            name="Workflow Monitor Dispatch Missing",
            entrypoint="entry",
            nodes=[],
            edges=[],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-monitor-dispatch-missing",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )
        proposal_event = await self.context.execution_store.save_event(
            ExecutionEvent(
                id="proposal-monitor-dispatch-missing",
                execution_id="execution-monitor-dispatch-missing",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
                payload_json={"summary": "Tighten validation instructions."},
            )
        )

        with patch("app.services.conversations.core.ConversationService.post_message", new_callable=AsyncMock) as post_message:
            post_message.return_value = {
                "message": {
                    "id": "message-monitor-dispatch-created",
                }
            }
            result = await WorkflowService(self.context).dispatch_monitor_proposal_to_main_agent(
                workflow.id,
                proposal_event.id,
                actor_user_id="user-1",
            )

        self.assertTrue(result["conversation_id"])
        created_conversation = await self.context.conversation_repo.get(result["conversation_id"])
        assert created_conversation is not None
        self.assertEqual(created_conversation.main_agent_profile_id, "main-agent-profile")
        self.assertEqual(created_conversation.metadata["conversation_purpose"], "monitor_proposal_dispatch")
        self.assertEqual(created_conversation.metadata["workflow_id"], workflow.id)

    async def test_monitor_requires_stronger_review_for_sensitive_workflow_improvement(self) -> None:
        await self._save_monitor_approval_context("conversation-sensitive-review")
        workflow = WorkflowDefinition(
            id="workflow-sensitive-review",
            name="Workflow Sensitive Review",
            entrypoint="entry",
            nodes=[],
            edges=[],
            task_definitions=[
                TaskDefinition(
                    id="task-sensitive",
                    name="Deploy Code",
                    description="Write code, deploy, send Slack updates, and delete stale production resources.",
                    instructions="Use credentials and API keys carefully. Human approval is required before deployment.",
                    expected_output="Pull request, deployment result, and external notification status.",
                    human_approval_required=True,
                )
            ],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allow_improvement_proposals": True,
                    "route_improvement_proposals_to_approval": True,
                    "approval_conversation_id": "conversation-sensitive-review",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-sensitive-review",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        approval = (await self.context.conversation_approval_repo.list_by_conversation("conversation-sensitive-review"))[0]
        review = approval.metadata["review_requirements"]
        self.assertEqual(result["approval_request_count"], 1)
        self.assertEqual(approval.metadata["strong_review_required"], True)
        self.assertEqual(approval.proposed_payload["strong_review_required"], True)
        self.assertEqual(review["level"], "strong")
        self.assertEqual(
            sorted(reason["category"] for reason in review["reasons"]),
            [
                "approval_boundaries",
                "code_writing_tasks",
                "credentials",
                "destructive_tools",
                "external_channels",
            ],
        )
        self.assertEqual(approval.proposed_payload["review_requirements"], review)

    async def test_monitor_routes_improvement_proposal_to_main_agent_default_inbox(self) -> None:
        await self.context.conversation_repo.create(
            Conversation(
                id="conversation-main-monitor",
                created_by_user_id="user-1",
                main_agent_profile_id="main-agent-profile",
            )
        )
        await self.context.main_agent_profile_repo.save(
            MainAgentProfile(
                id="main-agent-profile",
                name="Main Agent",
                agent_id="main-agent",
                default_workflow_id="main-workflow",
                metadata={
                    "main_agent_monitoring": {
                        "approval_conversation_id": "conversation-main-monitor",
                        "route_improvement_proposals_to_approval": True,
                    }
                },
            )
        )
        workflow = WorkflowDefinition(
            id="workflow-default-monitor-inbox",
            name="Workflow Default Monitor Inbox",
            entrypoint="entry",
            nodes=[],
            edges=[],
            task_definitions=[
                TaskDefinition(
                    id="task-final",
                    name="Final Task",
                    description="Finish the work.",
                    instructions="Do the work.",
                    expected_output="A result.",
                )
            ],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allow_improvement_proposals": True,
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-default-monitor-inbox",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["proposal_count"], 1)
        self.assertEqual(result["approval_request_count"], 1)
        approvals = await self.context.conversation_approval_repo.list_by_conversation("conversation-main-monitor")
        self.assertEqual(len(approvals), 1)
        messages = await self.context.conversation_message_repo.list_by_conversation("conversation-main-monitor")
        self.assertTrue(any(item.message_type.value == "workflow_update_proposal" for item in messages))

    async def test_rejected_monitor_improvement_approval_does_not_mutate_workflow(self) -> None:
        await self._save_monitor_approval_context("conversation-monitor-reject")
        workflow = WorkflowDefinition(
            id="workflow-monitor-reject",
            name="Workflow Monitor Reject",
            entrypoint="entry",
            nodes=[],
            edges=[],
            task_definitions=[
                TaskDefinition(id="task-final", name="Final Task", description="Finish.", expected_output="A result.")
            ],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allow_improvement_proposals": True,
                    "route_improvement_proposals_to_approval": True,
                    "approval_conversation_id": "conversation-monitor-reject",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-monitor-reject",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )
        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        approval_id = result["approval_requests"][0]["id"]

        rejected = await ConversationService(self.context).reject_request(
            approval_id,
            actor_user_id="user-1",
            reason="Not this change",
            store_reason_as_memory=True,
        )

        self.assertEqual(rejected["approval_request"]["status"], "rejected")
        self.assertEqual(rejected["memory"]["status"], "created")
        persisted = await self.context.workflow_repo.get(workflow.id)
        assert persisted is not None
        self.assertEqual(persisted.versioning.revision, 1)
        self.assertEqual(persisted.task_definitions[0].expected_output, "A result.")
        memories = await self.context.memory_repo.list()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].workflow_id, workflow.id)
        self.assertEqual(memories[0].metadata["approval_request_id"], approval_id)
        self.assertIn("Not this change", memories[0].content)

    async def test_monitor_improvement_approval_can_request_changes_without_mutating_workflow(self) -> None:
        await self._save_monitor_approval_context("conversation-monitor-request-changes")
        workflow = WorkflowDefinition(
            id="workflow-monitor-request-changes",
            name="Workflow Monitor Request Changes",
            entrypoint="entry",
            nodes=[],
            edges=[],
            task_definitions=[
                TaskDefinition(id="task-final", name="Final Task", description="Finish.", expected_output="A result.")
            ],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allow_improvement_proposals": True,
                    "route_improvement_proposals_to_approval": True,
                    "approval_conversation_id": "conversation-monitor-request-changes",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-monitor-request-changes",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )
        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        approval_id = result["approval_requests"][0]["id"]

        requested = await ConversationService(self.context).request_changes_to_approval(
            approval_id,
            actor_user_id="user-1",
            reason="Split the tool and memory changes into separate approvals",
        )

        self.assertEqual(requested["approval_request"]["status"], "cancelled")
        self.assertEqual(requested["approval_request"]["metadata"]["revision_requested"], True)
        self.assertEqual(
            requested["approval_request"]["metadata"]["last_revision_request"]["reason"],
            "Split the tool and memory changes into separate approvals",
        )
        persisted = await self.context.workflow_repo.get(workflow.id)
        assert persisted is not None
        self.assertEqual(persisted.versioning.revision, 1)
        self.assertEqual(persisted.task_definitions[0].expected_output, "A result.")
        messages = await self.context.conversation_message_repo.list_by_conversation(
            "conversation-monitor-request-changes"
        )
        self.assertEqual(
            [message.message_type.value for message in messages],
            ["system_note", "workflow_update_proposal", "system_note"],
        )
        self.assertEqual(messages[-1].content["revision_requested"], True)

    async def test_monitor_improvement_approval_can_split_for_partial_approval(self) -> None:
        await self._save_monitor_approval_context("conversation-monitor-split")
        workflow = WorkflowDefinition(
            id="workflow-monitor-split",
            name="Workflow Monitor Split",
            entrypoint="entry",
            nodes=[],
            edges=[],
            task_definitions=[
                TaskDefinition(id="task-final", name="Final Task", description="Finish.", expected_output="A result.")
            ],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allow_improvement_proposals": True,
                    "route_improvement_proposals_to_approval": True,
                    "approval_conversation_id": "conversation-monitor-split",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-monitor-split",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )
        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        parent_approval_id = result["approval_requests"][0]["id"]

        split = await ConversationService(self.context).split_approval_request(
            parent_approval_id,
            actor_user_id="user-1",
            reason="Approve workflow text separately from tool and memory changes",
        )

        self.assertEqual(split["approval_request"]["status"], "cancelled")
        self.assertEqual(split["approval_request"]["metadata"]["split_requested"], True)
        split_parts = {item["metadata"]["split_part"]: item for item in split["approval_requests"]}
        self.assertEqual(set(split_parts), {"workflow_definition", "tool_assignment", "memory_write"})
        workflow_child = split_parts["workflow_definition"]
        approved = await ConversationService(self.context).approve_request(
            workflow_child["id"],
            actor_user_id="user-1",
            reason="Approve the workflow definition only",
        )

        self.assertEqual(approved["approval_request"]["status"], "approved")
        self.assertEqual(approved["workflow"]["versioning"]["revision"], 2)
        pending_children = await self.context.conversation_approval_repo.list_by_conversation(
            "conversation-monitor-split"
        )
        child_statuses = {
            item.metadata.get("split_part"): item.status.value
            for item in pending_children
            if item.metadata.get("parent_approval_request_id") == parent_approval_id
        }
        self.assertEqual(child_statuses["workflow_definition"], "approved")
        self.assertEqual(child_statuses["tool_assignment"], "pending")
        self.assertEqual(child_statuses["memory_write"], "pending")

    async def test_approved_monitor_improvement_approval_creates_new_revision_with_provenance(self) -> None:
        await self._save_monitor_approval_context("conversation-monitor-approve")
        workflow = WorkflowDefinition(
            id="workflow-monitor-approve",
            name="Workflow Monitor Approve",
            entrypoint="entry",
            nodes=[],
            edges=[],
            task_definitions=[
                TaskDefinition(id="task-final", name="Final Task", description="Finish.", expected_output="A result.")
            ],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allow_improvement_proposals": True,
                    "route_improvement_proposals_to_approval": True,
                    "approval_conversation_id": "conversation-monitor-approve",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-monitor-approve",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )
        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        approval_id = result["approval_requests"][0]["id"]

        approved = await ConversationService(self.context).approve_request(
            approval_id,
            actor_user_id="user-1",
            reason="Apply it",
        )

        self.assertEqual(approved["approval_request"]["status"], "approved")
        self.assertEqual(approved["workflow"]["versioning"]["revision"], 2)
        persisted = await self.context.workflow_repo.get(workflow.id)
        assert persisted is not None
        self.assertEqual(persisted.versioning.revision, 2)
        self.assertIn("success criteria", persisted.task_definitions[0].instructions or "")
        self.assertIn("tool-use boundary", persisted.task_definitions[0].expected_output or "")
        self.assertIn("validation evidence", persisted.task_definitions[0].expected_output or "")
        self.assertEqual(persisted.metadata["provenance"]["approval_request_id"], approval_id)
        self.assertEqual(persisted.metadata["provenance"]["conversation_id"], "conversation-monitor-approve")
        self.assertEqual(persisted.metadata["provenance"]["action"], "workflow_update")
        self.assertEqual(persisted.metadata["provenance"]["decision"], "approved")
        self.assertEqual(
            persisted.metadata["main_agent_monitoring"]["last_improvement_proposal_event_id"],
            result["approval_requests"][0]["metadata"]["monitor_proposal_event_id"],
        )
        proposal_history = persisted.metadata["main_agent_monitoring"]["improvement_proposals"]
        self.assertEqual(proposal_history[0]["baseline_revision"], 1)
        self.assertEqual(proposal_history[0]["expected_replacement_revision"], 2)
        self.assertEqual(proposal_history[0]["baseline_quality_signals"]["failure_count"], 1)
        messages = await self.context.conversation_message_repo.list_by_conversation("conversation-monitor-approve")
        self.assertEqual(
            [message.message_type.value for message in messages],
            ["system_note", "workflow_update_proposal", "approval_result"],
        )

    async def test_monitor_records_post_change_comparison_after_approved_improvement(self) -> None:
        await self._save_monitor_approval_context("conversation-post-change")
        workflow = WorkflowDefinition(
            id="workflow-post-change",
            name="Workflow Post Change",
            entrypoint="entry",
            nodes=[],
            edges=[],
            task_definitions=[
                TaskDefinition(id="task-final", name="Final Task", description="Finish.", expected_output="A result.")
            ],
            metadata={
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allow_improvement_proposals": True,
                    "route_improvement_proposals_to_approval": True,
                    "approval_conversation_id": "conversation-post-change",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-post-change-failed",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )
        service = MainAgentWorkflowMonitorService(self.context, settings=self.settings)
        first_result = await service.run_once()
        approval_id = first_result["approval_requests"][0]["id"]
        await ConversationService(self.context).approve_request(
            approval_id,
            actor_user_id="user-1",
            reason="Apply it",
        )
        await self._save_execution(
            execution_id="execution-post-change-success",
            workflow_id=workflow.id,
            status=ExecutionStatus.COMPLETED,
            output_payload={"validation_result": "passed"},
        )
        await self.context.execution_store.save_artifact(
            ExecutionArtifact(
                execution_id="execution-post-change-success",
                artifact_type="text",
                name="final_output.txt",
                content_text="validated",
                size_bytes=len("validated".encode("utf-8")),
            )
        )

        second_result = await service.run_once()

        self.assertEqual(second_result["post_change_comparison_count"], 1)
        comparison = second_result["post_change_comparisons"][0]
        self.assertEqual(comparison["workflow_id"], workflow.id)
        self.assertEqual(comparison["execution_id"], "execution-post-change-success")
        self.assertEqual(comparison["outcome"], "helped")
        self.assertEqual(comparison["baseline_revision"], 1)
        self.assertEqual(comparison["evaluated_revision"], 2)
        self.assertEqual(comparison["baseline_quality_signals"]["failure_count"], 1)
        self.assertEqual(comparison["current_quality_signals"]["success_count"], 1)
        self.assertEqual(comparison["deltas"]["success_rate"]["direction"], "improved")
        events = await self.context.execution_store.list_events("execution-post-change-success")
        self.assertIn(ExecutionEventType.MONITOR_IMPROVEMENT_COMPARED, [event.event_type for event in events])
        metrics = self.context.runtime_operations.snapshot_dict()
        self.assertEqual(metrics["counters"]["main_agent_monitor.post_change_comparisons"], 1)
        self.assertEqual(metrics["counters"]["main_agent_monitor.post_change_comparisons.helped"], 1)

    async def test_monitor_skips_improvement_proposal_without_explicit_opt_in(self) -> None:
        workflow = self._workflow("workflow-no-proposals", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-no-proposal-failed",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(result["proposal_count"], 0)
        events = await self.context.execution_store.list_events("execution-no-proposal-failed")
        self.assertEqual([event.event_type for event in events], [ExecutionEventType.MONITOR_FINDING_CREATED])

    async def test_monitor_stale_improvement_recommends_restarting_active_executions(self) -> None:
        workflow = self._workflow(
            "workflow-stale-proposal",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"allow_improvement_proposals": True},
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-stale-proposal",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["proposal_count"], 1)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["finding"]["category"], "stale_execution")
        self.assertEqual(proposal["restart_active_executions"], True)
        self.assertEqual(proposal["proposed_change"]["type"], "runtime_stale_repair_review")
        self.assertEqual(
            [item["kind"] for item in proposal["proposed_change"]["runtime_schedule_recommendations"]],
            [
                "adjust_timeout",
                "adjust_schedule_cadence",
                "adjust_max_concurrency",
                "review_execution_host",
                "review_runtime_adapter",
                "improve_stale_run_handling",
                "decide_restart_active_executions",
            ],
        )
        self.assertEqual(proposal["proposed_change"]["schedule_change_approval"]["approval_required"], True)
        self.assertEqual(proposal["proposed_change"]["schedule_change_approval"]["approval_type"], "schedule_update")
        self.assertEqual(proposal["proposed_change"]["schedule_change_approval"]["split_from_workflow_approval"], True)
        self.assertIn(
            "schedule.max_concurrent_executions",
            proposal["proposed_change"]["schedule_change_approval"]["affected_fields"],
        )
        self.assertEqual(
            [item["kind"] for item in proposal["proposed_change"]["memory_recommendations"]],
            [
                "create_or_update_workflow_scoped_memories",
                "suppress_duplicate_memories",
                "mark_stale_memories",
            ],
        )
        self.assertEqual(proposal["diagnosis"]["finding_category"], "stale_execution")
        self.assertEqual(proposal["quality_signals"]["stale_execution_count"], 1)

    async def test_monitor_routes_schedule_change_requirements_to_approval_payload(self) -> None:
        await self._save_monitor_approval_context("conversation-schedule-approval")
        workflow = self._workflow(
            "workflow-schedule-approval",
            {
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
                "main_agent_monitoring": {
                    "allow_improvement_proposals": True,
                    "route_improvement_proposals_to_approval": True,
                    "approval_conversation_id": "conversation-schedule-approval",
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-schedule-approval",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["approval_request_count"], 1)
        approval = (await self.context.conversation_approval_repo.list_by_conversation("conversation-schedule-approval"))[0]
        schedule_approval = approval.proposed_payload["schedule_change_approval"]
        self.assertEqual(schedule_approval, approval.metadata["schedule_change_approval"])
        self.assertEqual(schedule_approval["approval_required"], True)
        self.assertEqual(schedule_approval["approval_type"], "schedule_update")
        self.assertEqual(schedule_approval["split_from_workflow_approval"], True)
        self.assertIn("schedule.trigger_config.cron", schedule_approval["affected_fields"])
        self.assertIn("schedule.runtime_adapter_override", schedule_approval["affected_fields"])

    async def test_monitor_records_evaluation_agent_advisory_review_for_strict_workflow(self) -> None:
        await self._save_evaluation_agent()
        workflow = self._workflow(
            "workflow-evaluation-review",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "level": "strict",
                    "allow_improvement_proposals": True,
                    "allow_evaluation_agent_review": True,
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-evaluation-review",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["evaluation_review_count"], 1)
        review = result["evaluation_reviews"][0]
        self.assertEqual(review["status"], "recorded")
        self.assertEqual(review["judge_agent_id"], "evaluation")
        self.assertEqual(review["advisory"], True)
        self.assertEqual(review["read_only"], True)
        self.assertEqual(review["deterministic_comparison"]["alignment"], "aligned")
        self.assertEqual(review["deterministic_comparison"]["deterministic_status"], "failed")
        proposal = result["proposals"][0]
        advisory = proposal["diagnosis"]["advisory_evidence"][0]
        self.assertEqual(advisory["source"], "evaluation_agent")
        self.assertEqual(advisory["event_id"], review["event_id"])
        self.assertEqual(advisory["deterministic_alignment"], "aligned")
        events = await self.context.execution_store.list_events("execution-evaluation-review")
        self.assertIn(ExecutionEventType.MONITOR_EVALUATION_RECORDED, [event.event_type for event in events])

    async def test_monitor_skips_evaluation_when_agent_is_missing(self) -> None:
        workflow = self._workflow(
            "workflow-evaluation-missing",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "level": "strict",
                    "allow_improvement_proposals": True,
                    "allow_evaluation_agent_review": True,
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-evaluation-missing",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["evaluation_review_count"], 1)
        self.assertEqual(result["evaluation_reviews"][0]["status"], "skipped")
        self.assertEqual(result["evaluation_reviews"][0]["reason"], "evaluation_agent_unavailable")
        self.assertEqual(result["proposal_count"], 1)

    async def test_monitor_refuses_evaluation_agent_with_mutating_tools_or_memory(self) -> None:
        await self._save_evaluation_agent(unsafe=True, memory_enabled=True)
        workflow = self._workflow(
            "workflow-evaluation-unsafe",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "level": "strict",
                    "allow_improvement_proposals": True,
                    "allow_evaluation_agent_review": True,
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-evaluation-unsafe",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["evaluation_review_count"], 1)
        review = result["evaluation_reviews"][0]
        self.assertEqual(review["status"], "skipped")
        self.assertEqual(review["reason"], "evaluation_agent_not_read_only")
        self.assertEqual(review["safety"]["memory_enabled"], True)
        self.assertIn("agency.memory.remember", review["safety"]["unsafe_tool_ids"])
        events = await self.context.execution_store.list_events("execution-evaluation-unsafe")
        self.assertNotIn(ExecutionEventType.MONITOR_EVALUATION_RECORDED, [event.event_type for event in events])

    async def test_monitor_quality_signals_include_repeated_failures_and_duration(self) -> None:
        workflow = self._workflow(
            "workflow-quality-signals",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"allow_improvement_proposals": True},
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-quality-completed",
            workflow_id=workflow.id,
            status=ExecutionStatus.COMPLETED,
            duration_seconds=20,
        )
        await self._save_execution(
            execution_id="execution-quality-failed-1",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="Tool timeout",
            duration_seconds=10,
            metadata={"trigger": {"source": "human_correction", "correction_of_execution_id": "prior-run"}},
        )
        await self._save_execution(
            execution_id="execution-quality-failed-2",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="Tool timeout",
            duration_seconds=30,
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-quality-failed-1",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.APPROVAL_REQUESTED,
                payload={"tool_id": "tool-needs-approval"},
            )
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-quality-failed-1",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.APPROVAL_REJECTED,
                payload={"tool_id": "tool-needs-approval", "reason": "unsafe"},
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["proposal_count"], 2)
        failed_proposals = [
            proposal for proposal in result["proposals"]
            if proposal["finding"]["category"] == "tool_failure"
        ]
        self.assertEqual(len(failed_proposals), 2)
        signals = failed_proposals[0]["quality_signals"]
        self.assertEqual(signals["execution_count"], 3)
        self.assertEqual(signals["success_count"], 1)
        self.assertEqual(signals["failure_count"], 2)
        self.assertAlmostEqual(signals["success_rate"], 1 / 3)
        self.assertAlmostEqual(signals["average_duration_seconds"], 20.0)
        self.assertAlmostEqual(signals["timeout_frequency"], 2 / 3)
        self.assertEqual(signals["repeated_failure_signatures"], [{"signature": "tool timeout", "count": 2}])
        self.assertEqual(signals["approval_request_count"], 1)
        self.assertEqual(signals["approval_rejection_count"], 1)
        self.assertEqual(signals["approval_rejection_rate"], 1.0)
        self.assertEqual(signals["missing_artifact_count"], 1)
        self.assertEqual(signals["missing_validation_output_count"], 1)
        self.assertEqual(signals["human_correction_count"], 1)
        self.assertAlmostEqual(signals["human_correction_frequency"], 1 / 3)

    async def test_monitor_stores_success_run_summary_when_safely_opted_in(self) -> None:
        workflow = self._workflow(
            "workflow-summary-success",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "store_run_summaries": True,
                    "safe_to_summarize": True,
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-summary-success",
            workflow_id=workflow.id,
            status=ExecutionStatus.COMPLETED,
            output_payload={"final_output": "Useful result"},
        )

        with patch.dict(os.environ, {"AGENT_PERSISTENT_RUN_SUMMARY_ENABLED": "true"}, clear=False):
            reset_settings_cache()
            result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["run_summary_count"], 1)
        self.assertEqual(result["run_summaries"][0]["status"], "created")
        summaries = await self.context.memory_repo.query(
            workflow_id=workflow.id,
            source="main_agent_workflow_monitor",
            memory_types=["run_summary"],
            statuses=["active"],
            limit=10,
        )
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].source_execution_id, "execution-summary-success")
        self.assertEqual(summaries[0].metadata["monitor_job"], "main_agent_workflow_monitor")
        self.assertEqual(summaries[0].metadata["workflow_id"], workflow.id)
        self.assertEqual(summaries[0].metadata["source_execution_id"], "execution-summary-success")
        self.assertEqual(summaries[0].sensitive, False)

    async def test_monitor_stores_failure_run_summary_only_when_failure_summaries_allowed(self) -> None:
        workflow = self._workflow(
            "workflow-summary-failure",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "store_failure_summaries": True,
                    "safe_to_summarize": True,
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-summary-failure",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="Tool failed after validation",
        )

        with patch.dict(os.environ, {"AGENT_PERSISTENT_RUN_SUMMARY_ENABLED": "true"}, clear=False):
            reset_settings_cache()
            result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["run_summary_count"], 1)
        summaries = await self.context.memory_repo.query(
            workflow_id=workflow.id,
            source="main_agent_workflow_monitor",
            memory_types=["run_summary"],
            statuses=["active"],
            limit=10,
        )
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].metadata["execution_status"], "failed")

    async def test_monitor_run_summary_requires_global_flag_and_safe_summarization(self) -> None:
        unsafe = self._workflow(
            "workflow-summary-unsafe",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"store_run_summaries": True},
            },
        )
        safe = self._workflow(
            "workflow-summary-flag-disabled",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "store_run_summaries": True,
                    "safe_to_summarize": True,
                },
            },
        )
        await self.context.workflow_repo.create(unsafe)
        await self.context.workflow_repo.create(safe)
        await self._save_execution(
            execution_id="execution-summary-unsafe",
            workflow_id=unsafe.id,
            status=ExecutionStatus.COMPLETED,
            output_payload={"final_output": "Do not store"},
        )
        await self._save_execution(
            execution_id="execution-summary-flag-disabled",
            workflow_id=safe.id,
            status=ExecutionStatus.COMPLETED,
            output_payload={"final_output": "Flag disabled"},
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["run_summary_count"], 1)
        self.assertEqual(result["run_summaries"][0]["status"], "disabled")
        summaries = await self.context.memory_repo.query(memory_types=["run_summary"], limit=10)
        self.assertEqual(summaries, [])

    async def test_monitor_run_summary_dedupes_across_monitor_instances(self) -> None:
        workflow = self._workflow(
            "workflow-summary-dedupe",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "store_run_summaries": True,
                    "safe_to_summarize": True,
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-summary-dedupe",
            workflow_id=workflow.id,
            status=ExecutionStatus.COMPLETED,
            output_payload={"final_output": "Same result"},
        )

        with patch.dict(os.environ, {"AGENT_PERSISTENT_RUN_SUMMARY_ENABLED": "true"}, clear=False):
            reset_settings_cache()
            first = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
            second = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(first["run_summaries"][0]["status"], "created")
        self.assertEqual(second["run_summaries"], [])
        summaries = await self.context.memory_repo.query(
            workflow_id=workflow.id,
            source="main_agent_workflow_monitor",
            memory_types=["run_summary"],
            statuses=["active"],
            limit=10,
        )
        self.assertEqual(len(summaries), 1)

    async def test_monitor_levels_control_completed_execution_findings(self) -> None:
        minimal_workflow = self._workflow(
            "workflow-minimal",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"level": "minimal"},
            },
        )
        standard_workflow = self._workflow(
            "workflow-standard",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"level": "standard"},
            },
        )
        await self.context.workflow_repo.create(minimal_workflow)
        await self.context.workflow_repo.create(standard_workflow)
        await self._save_execution(
            execution_id="execution-minimal-completed",
            workflow_id=minimal_workflow.id,
            status=ExecutionStatus.COMPLETED,
        )
        await self._save_execution(
            execution_id="execution-standard-completed",
            workflow_id=standard_workflow.id,
            status=ExecutionStatus.COMPLETED,
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["finding_count"], 1)
        self.assertEqual(result["findings"][0]["category"], "completed_execution")
        self.assertEqual(result["findings"][0]["execution_id"], "execution-standard-completed")
        self.assertEqual(result["scanned_by_level"], {"standard": 1})

    async def test_monitor_dedupes_findings_in_one_service_instance(self) -> None:
        workflow = self._workflow("workflow-dedupe", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-dedupe",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="same failure",
        )
        service = MainAgentWorkflowMonitorService(self.context, settings=self.settings)

        first = await service.run_once()
        second = await service.run_once()

        self.assertEqual(first["finding_count"], 1)
        self.assertEqual(second["finding_count"], 0)
        events = await self.context.execution_store.list_events("execution-dedupe")
        monitor_events = [
            event for event in events if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED
        ]
        self.assertEqual(len(monitor_events), 1)

    async def test_monitor_dedupes_persisted_terminal_findings_across_service_instances(self) -> None:
        workflow = self._workflow("workflow-persisted-dedupe", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-persisted-dedupe",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="same failure",
        )

        first = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        second = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(first["finding_count"], 1)
        self.assertEqual(second["finding_count"], 0)
        events = await self.context.execution_store.list_events("execution-persisted-dedupe")
        monitor_events = [
            event for event in events if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED
        ]
        self.assertEqual(len(monitor_events), 1)
        self.assertEqual(monitor_events[0].metadata["dedupe_key"], f"failed_execution:{workflow.id}:execution-persisted-dedupe:failed:")

    async def test_monitor_dedupes_completed_execution_findings_in_persisted_history(self) -> None:
        workflow = self._workflow("workflow-completed-dedupe", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-completed-dedupe",
            workflow_id=workflow.id,
            status=ExecutionStatus.COMPLETED,
        )

        first = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        second = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(first["finding_count"], 1)
        self.assertEqual(first["findings"][0]["category"], "completed_execution")
        self.assertEqual(second["finding_count"], 0)
        events = await self.context.execution_store.list_events("execution-completed-dedupe")
        self.assertEqual(
            sum(1 for event in events if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED),
            1,
        )

    async def test_workflow_api_includes_monitoring_summary(self) -> None:
        workflow = self._workflow(
            "workflow-api-monitoring",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"level": "minimal"},
            },
        )
        await self.context.workflow_repo.create(workflow)

        with TestClient(create_app(self.context)) as client:
            response = client.get(f"/workflows/{workflow.id}")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["monitoring"]["enabled"], True)
        self.assertEqual(payload["monitoring"]["level"], "minimal")
        self.assertEqual(payload["monitoring"]["visible_to_main_agent"], True)
        self.assertEqual(payload["monitoring"]["status_label"], "minimal_monitoring")
        self.assertEqual(payload["monitoring"]["controls"]["enabled"], True)
        self.assertEqual(payload["monitoring"]["controls"]["supervise_token_usage"], True)
        self.assertEqual(payload["monitoring"]["controls"]["supervise_context_health"], True)
        self.assertEqual(payload["monitoring"]["controls"]["supervise_subagents"], True)
        self.assertEqual(payload["monitoring"]["controls"]["allow_evaluation_agent_review"], True)
        self.assertIsNone(payload["monitoring"]["explicit_controls"]["allow_evaluation_agent_review"])
        self.assertEqual(payload["monitoring"]["control_sources"]["allow_evaluation_agent_review"], "policy_default")
        self.assertEqual(payload["monitoring"]["is_main_agent_default_workflow"], False)
        self.assertEqual(payload["monitoring"]["operator_actions"]["update_controls"], f"/workflows/{workflow.id}/monitoring")

    async def test_workflow_api_distinguishes_explicit_monitoring_controls_from_policy_defaults(self) -> None:
        workflow = self._workflow(
            "workflow-api-monitoring-explicit",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"allow_evaluation_agent_review": False},
            },
        )
        await self.context.workflow_repo.create(workflow)

        with TestClient(create_app(self.context)) as client:
            response = client.get(f"/workflows/{workflow.id}")

        self.assertEqual(response.status_code, 200)
        monitoring = response.json()["monitoring"]
        self.assertEqual(monitoring["controls"]["allow_evaluation_agent_review"], False)
        self.assertEqual(monitoring["explicit_controls"]["allow_evaluation_agent_review"], False)
        self.assertEqual(monitoring["control_sources"]["allow_evaluation_agent_review"], "explicit")

    async def test_workflow_api_identifies_active_main_agent_default_workflow(self) -> None:
        workflow = self._workflow(
            "workflow-api-main-agent-default",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"allow_self_monitoring": True},
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self.context.main_agent_profile_repo.save(
            MainAgentProfile(
                id="main-agent-profile-api",
                name="Main Agent",
                agent_id="main-agent",
                default_workflow_id=workflow.id,
            )
        )

        with TestClient(create_app(self.context)) as client:
            response = client.get(f"/workflows/{workflow.id}")

        self.assertEqual(response.status_code, 200)
        monitoring = response.json()["monitoring"]
        self.assertEqual(monitoring["is_main_agent_default_workflow"], True)
        self.assertEqual(monitoring["controls"]["allow_self_monitoring"], True)

    async def test_workflow_monitoring_operator_controls_can_exempt_workflow(self) -> None:
        workflow = self._workflow(
            "workflow-monitoring-controls",
            {
                "visible_to_main_agent": True,
                "created_by": "user-owner",
                "owner_ids": ["user-owner"],
                "main_agent_monitoring": {
                    "level": "standard",
                    "store_run_summaries": True,
                    "allow_improvement_proposals": True,
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self.context.main_agent_profile_repo.save(
            MainAgentProfile(
                id="main-agent-profile-controls",
                name="Main Agent",
                agent_id="main-agent",
                default_workflow_id=workflow.id,
            )
        )
        headers = {"x-agency-user-id": "user-owner", "x-agency-user-email": "owner@example.com"}

        with TestClient(create_app(self.context)) as client:
            client.post(
                "/users/sync",
                json={"id": "user-owner", "email": "owner@example.com", "display_name": "Owner"},
            )
            detail = client.get(f"/workflows/{workflow.id}/monitoring")
            updated = client.patch(
                f"/workflows/{workflow.id}/monitoring",
                headers=headers,
                json={
                    "enabled": False,
                    "reason": "Human-managed workflow",
                    "store_failure_summaries": True,
                    "allow_evaluation_agent_review": True,
                    "allow_self_monitoring": True,
                    "delegate_hitl_to_main_agent": True,
                    "supervise_token_usage": False,
                    "supervise_context_health": False,
                    "supervise_subagents": False,
                    "route_steering_requests_to_approval": True,
                    "allowed_steering_actions": ["request_human_review", "repair_stale_execution"],
                    "auto_apply_steering_actions": ["repair_stale_execution"],
                    "excluded_subagent_ids": ["agent-excluded"],
                    "excluded_task_ids": ["task-excluded"],
                },
            )

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["controls"]["store_run_summaries"], True)
        self.assertEqual(updated.status_code, 200)
        monitoring = updated.json()["monitoring"]
        self.assertEqual(monitoring["enabled"], False)
        self.assertEqual(monitoring["exempted"], True)
        self.assertEqual(monitoring["status_label"], "exempt")
        self.assertEqual(monitoring["exemption"]["reason"], "Human-managed workflow")
        self.assertEqual(monitoring["controls"]["store_failure_summaries"], True)
        self.assertEqual(monitoring["controls"]["allow_evaluation_agent_review"], True)
        self.assertEqual(monitoring["controls"]["allow_self_monitoring"], True)
        self.assertEqual(monitoring["controls"]["delegate_hitl_to_main_agent"], True)
        self.assertEqual(monitoring["controls"]["supervise_token_usage"], False)
        self.assertEqual(monitoring["controls"]["supervise_context_health"], False)
        self.assertEqual(monitoring["controls"]["supervise_subagents"], False)
        self.assertEqual(monitoring["controls"]["route_steering_requests_to_approval"], True)
        self.assertEqual(
            monitoring["controls"]["allowed_steering_actions"],
            ["request_human_review", "repair_stale_execution"],
        )
        self.assertEqual(monitoring["controls"]["auto_apply_steering_actions"], ["repair_stale_execution"])
        self.assertEqual(monitoring["controls"]["excluded_subagent_ids"], ["agent-excluded"])
        self.assertEqual(monitoring["controls"]["excluded_task_ids"], ["task-excluded"])

    async def test_delegated_hitl_steering_does_not_create_human_approval(self) -> None:
        await self._save_monitor_approval_context("conversation-delegated-hitl")
        workflow = self._workflow(
            "workflow-delegated-hitl",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {
                    "allowed_steering_actions": ["request_human_review"],
                    "route_steering_requests_to_approval": True,
                    "approval_conversation_id": "conversation-delegated-hitl",
                    "delegate_hitl_to_main_agent": True,
                },
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-delegated-hitl",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            age_seconds=30,
        )
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="execution-delegated-hitl",
                workflow_id=workflow.id,
                event_type=ExecutionEventType.SUBAGENT_NEEDS_APPROVAL,
                actor_type="agent",
                actor_id="agent-review",
                agent_id="agent-review",
                payload_json={
                    "approval_type": "tool_scope_change",
                    "reason": "Sub-agent needs approval to continue.",
                },
            )
        )

        result = await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        self.assertEqual(result["steering_request_count"], 1)
        self.assertEqual(result["approval_request_count"], 0)
        steering = result["steering_requests"][0]
        self.assertEqual(steering["recommended_action"], "request_human_review")
        self.assertEqual(steering["policy"]["delegate_hitl_to_main_agent"], True)
        self.assertEqual(steering["policy"]["decision_actor"], "main_agent")
        self.assertEqual(steering["policy"]["delegated_hitl"], True)
        self.assertEqual(steering["policy"]["requires_human_approval"], False)

    async def test_workflow_monitoring_rejects_self_monitoring_for_non_main_agent_workflow(self) -> None:
        workflow = self._workflow(
            "workflow-monitoring-non-main",
            {
                "visible_to_main_agent": True,
                "created_by": "user-owner",
                "owner_ids": ["user-owner"],
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self.context.main_agent_profile_repo.save(
            MainAgentProfile(
                id="main-agent-profile-controls",
                name="Main Agent",
                agent_id="main-agent",
                default_workflow_id="another-workflow",
            )
        )
        headers = {"x-agency-user-id": "user-owner", "x-agency-user-email": "owner@example.com"}

        with TestClient(create_app(self.context)) as client:
            client.post(
                "/users/sync",
                json={"id": "user-owner", "email": "owner@example.com", "display_name": "Owner"},
            )
            response = client.patch(
                f"/workflows/{workflow.id}/monitoring",
                headers=headers,
                json={"allow_self_monitoring": True},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("active main-agent default workflow", response.json()["detail"])
        persisted = await self.context.workflow_repo.get(workflow.id)
        assert persisted is not None
        monitoring = persisted.metadata.get("main_agent_monitoring")
        self.assertTrue(not isinstance(monitoring, dict) or "allow_self_monitoring" not in monitoring)

    async def test_workflow_monitoring_events_and_execution_history_include_findings_and_proposals(self) -> None:
        workflow = self._workflow(
            "workflow-monitoring-events",
            {
                "visible_to_main_agent": True,
                "main_agent_monitoring": {"allow_improvement_proposals": True},
            },
        )
        await self.context.workflow_repo.create(workflow)
        await self._save_execution(
            execution_id="execution-monitoring-events",
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            error="tool failed",
        )
        await self.context.main_agent_profile_repo.save(
            MainAgentProfile(
                id="main-agent-profile",
                name="Main Agent",
                agent_id="main-agent",
                default_workflow_id="main-workflow",
            )
        )
        await self.context.conversation_repo.create(
            Conversation(
                id="conversation-monitoring-events",
                created_by_user_id="user-1",
                main_agent_profile_id="main-agent-profile",
            )
        )
        await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()
        proposal_event = next(
            event
            for event in await self.context.execution_store.list_events("execution-monitoring-events")
            if event.event_type == ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED
        )
        await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-monitoring-events",
                conversation_id="conversation-monitoring-events",
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text="Review monitor improvement proposal.",
                metadata={
                    "source": "main_agent_monitor",
                    "monitor_action": "dispatch_proposal_to_main_agent",
                    "monitor_proposal_event_id": proposal_event.id,
                    "operator_note": "Keep approval gates in place.",
                    "page_context": {
                        "selection": {"workflowId": workflow.id},
                    },
                },
            )
        )

        with TestClient(create_app(self.context)) as client:
            events = client.get(f"/workflows/{workflow.id}/monitoring/events")
            history = client.get(f"/workflows/{workflow.id}/executions")

        self.assertEqual(events.status_code, 200)
        body = events.json()
        self.assertEqual(len(body["findings"]), 1)
        self.assertEqual(body["findings"][0]["payload"]["category"], "failed_execution")
        self.assertEqual(len(body["proposals"]), 1)
        self.assertEqual(body["proposals"][0]["payload"]["finding"]["evidence"][0]["execution_id"], "execution-monitoring-events")
        self.assertEqual(len(body["proposals"][0]["dispatches"]), 1)
        self.assertEqual(body["proposals"][0]["dispatches"][0]["conversation_id"], "conversation-monitoring-events")
        self.assertEqual(body["proposals"][0]["dispatches"][0]["operator_note"], "Keep approval gates in place.")
        self.assertEqual(history.status_code, 200)
        execution = history.json()["items"][0]
        self.assertEqual(execution["stale_repair_action"]["available"], False)
        self.assertTrue(
            any(event["event_type"] == "monitor.finding.created" for event in execution["monitor_events"])
        )

    async def test_workflow_scoped_stale_repair_action_only_repairs_target_workflow(self) -> None:
        target = self._workflow("workflow-stale-repair-target", {"visible_to_main_agent": True})
        other = self._workflow("workflow-stale-repair-other", {"visible_to_main_agent": True})
        await self.context.workflow_repo.create(target)
        await self.context.workflow_repo.create(other)
        await self._save_execution(
            execution_id="execution-stale-repair-target",
            workflow_id=target.id,
            status=ExecutionStatus.RUNNING,
        )
        await self._save_execution(
            execution_id="execution-stale-repair-other",
            workflow_id=other.id,
            status=ExecutionStatus.RUNNING,
        )

        with TestClient(create_app(self.context)) as client:
            history = client.get(f"/workflows/{target.id}/executions")
            repaired = client.post(f"/workflows/{target.id}/stale-executions/repair")

        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["items"][0]["stale_repair_action"]["available"], True)
        self.assertEqual(repaired.status_code, 200)
        self.assertEqual(repaired.json()["repaired_count"], 1)
        self.assertEqual(repaired.json()["items"][0]["execution_id"], "execution-stale-repair-target")
        target_execution = await self.context.execution_store.get_execution("execution-stale-repair-target")
        other_execution = await self.context.execution_store.get_execution("execution-stale-repair-other")
        assert target_execution is not None
        assert other_execution is not None
        self.assertEqual(target_execution.status.value, "queued")
        self.assertEqual(other_execution.status.value, "running")


class MainAgentWorkflowMonitorLifespanTests(unittest.TestCase):
    def tearDown(self) -> None:
        for key in [
            "MAIN_AGENT_WORKFLOW_MONITOR_ENABLED",
            "MAIN_AGENT_WORKFLOW_MONITOR_INTERVAL_SECONDS",
        ]:
            os.environ.pop(key, None)
        reset_settings_cache()

    def test_app_lifespan_starts_main_agent_workflow_monitor_loop_when_enabled(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "MAIN_AGENT_WORKFLOW_MONITOR_ENABLED": "true",
                    "MAIN_AGENT_WORKFLOW_MONITOR_INTERVAL_SECONDS": "1",
                },
                clear=False,
        ):
            reset_settings_cache()
            run_once_mock = AsyncMock(return_value={"status": "ok"})
            with patch(
                    "app.services.main_agent_workflow_monitor.MainAgentWorkflowMonitorService.run_once",
                    run_once_mock,
            ):
                with TestClient(create_app(context=create_test_api_context())):
                    time.sleep(0.1)

        self.assertGreaterEqual(run_once_mock.await_count, 1)


if __name__ == "__main__":
    unittest.main()
