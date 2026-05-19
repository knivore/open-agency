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
    Execution,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    MainAgentProfile,
    MemorySettings,
    ScheduleDefinition,
    ScheduleType,
    TaskDefinition,
    WorkflowDefinition,
)
from app.services.conversations.core import ConversationService
from app.services.conversations.policy import MainAgentPolicyService
from app.services.main_agent_workflow_monitor import (
    EVALUATION_AGENT_READ_ONLY_TOOL_IDS,
    MainAgentWorkflowMonitorService,
)
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
            memory_kinds=["run_summary"],
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
            memory_kinds=["run_summary"],
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
        summaries = await self.context.memory_repo.query(memory_kinds=["run_summary"], limit=10)
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
        self.assertEqual(second["run_summaries"][0]["status"], "skipped")
        self.assertEqual(second["run_summaries"][0]["reason"], "duplicate")
        summaries = await self.context.memory_repo.query(
            workflow_id=workflow.id,
            source="main_agent_workflow_monitor",
            memory_kinds=["run_summary"],
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
        self.assertEqual(payload["monitoring"]["is_main_agent_default_workflow"], False)
        self.assertEqual(payload["monitoring"]["operator_actions"]["update_controls"], f"/workflows/{workflow.id}/monitoring")

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
        await MainAgentWorkflowMonitorService(self.context, settings=self.settings).run_once()

        with TestClient(create_app(self.context)) as client:
            events = client.get(f"/workflows/{workflow.id}/monitoring/events")
            history = client.get(f"/workflows/{workflow.id}/executions")

        self.assertEqual(events.status_code, 200)
        body = events.json()
        self.assertEqual(len(body["findings"]), 1)
        self.assertEqual(body["findings"][0]["payload"]["category"], "failed_execution")
        self.assertEqual(len(body["proposals"]), 1)
        self.assertEqual(body["proposals"][0]["payload"]["finding"]["evidence"][0]["execution_id"], "execution-monitoring-events")
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
