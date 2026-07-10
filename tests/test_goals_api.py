from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.domain import (
    Execution,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    NodeType,
    TaskDefinition,
    WorkflowDefinition,
    WorkflowNodeDefinition,
)
from app.services.goals import GoalStartupReconciler
from app.tools.runtime.executor import ToolRuntimeExecutor


class GoalsApiTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.headers = {
            "x-agency-user-id": "user-goals",
            "x-agency-user-email": "goals@example.com",
        }
        self.client.post(
            "/users/sync",
            json={"id": "user-goals", "email": "goals@example.com", "display_name": "Goals User"},
        )

    def tearDown(self):
        reset_settings_cache()

    def test_goal_lifecycle(self):
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={
                "objective": "Keep the release evidence current",
                "successCriteria": [{"kind": "artifact", "description": "Release evidence exists"}],
                "constraints": {"autonomy": "guarded"},
            },
        )
        self.assertEqual(created.status_code, 201)
        goal = created.json()
        self.assertEqual(goal["objective"], "Keep the release evidence current")
        self.assertEqual(goal["status"], "created")
        self.assertEqual(goal["owner_actor"], "user-goals")

        paused = self.client.post(f"/goals/{goal['id']}/pause", headers=self.headers)
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["status"], "paused")

        resumed = self.client.post(f"/goals/{goal['id']}/resume", headers=self.headers)
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["status"], "active")

        completed = self.client.post(
            f"/goals/{goal['id']}/complete",
            headers=self.headers,
            json={"evidence": [{"type": "artifact", "id": "artifact-1"}], "evaluation": {"confidence": "high"}},
        )
        self.assertEqual(completed.status_code, 200)
        payload = completed.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["evidence"][0]["id"], "artifact-1")
        self.assertEqual(payload["evaluation"]["status"], "sufficient")
        self.assertEqual(payload["evaluation"]["reviewer_evaluation"]["confidence"], "high")
        self.assertIsNotNone(payload["completed_at"])

    def test_goal_completion_requires_sufficient_evidence(self):
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={
                "objective": "Collect approval evidence",
                "successCriteria": [{"id": "approval", "kind": "approval", "description": "Approval exists"}],
            },
        )
        self.assertEqual(created.status_code, 201)
        goal_id = created.json()["id"]

        missing = self.client.post(f"/goals/{goal_id}/complete", headers=self.headers, json={"evidence": []})
        self.assertEqual(missing.status_code, 409)

        evaluated = self.client.post(
            f"/goals/{goal_id}/evaluate",
            headers=self.headers,
            json={"evidence": [{"type": "artifact", "id": "artifact-1"}]},
        )
        self.assertEqual(evaluated.status_code, 200)
        self.assertEqual(evaluated.json()["status"], "missing_evidence")
        self.assertFalse(evaluated.json()["sufficient"])

        attached = self.client.post(
            f"/goals/{goal_id}/evidence",
            headers=self.headers,
            json={"evidence": [{"type": "approval", "id": "approval-1", "criterion_id": "approval"}]},
        )
        self.assertEqual(attached.status_code, 200)
        self.assertEqual(attached.json()["evidence"][0]["id"], "approval-1")

        completed = self.client.post(f"/goals/{goal_id}/complete", headers=self.headers, json={})
        self.assertEqual(completed.status_code, 200)
        payload = completed.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["evaluation"]["status"], "sufficient")
        self.assertEqual(payload["evaluation"]["criteria_results"][0]["criterion_id"], "approval")

    def test_high_risk_goal_completion_requires_independent_review(self):
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={
                "objective": "Publish production deployment",
                "priority": "high",
                "successCriteria": [{"id": "artifact", "kind": "artifact", "description": "Deployment proof"}],
                "constraints": {"risk_level": "high"},
            },
        )
        self.assertEqual(created.status_code, 201)
        goal_id = created.json()["id"]

        worker_only = self.client.post(
            f"/goals/{goal_id}/complete",
            headers=self.headers,
            json={
                "evidence": [
                    {
                        "type": "artifact",
                        "id": "artifact-worker-1",
                        "criterion_id": "artifact",
                        "agent_id": "worker-agent",
                    }
                ]
            },
        )

        self.assertEqual(worker_only.status_code, 409)
        self.assertIn("independent review", worker_only.json()["detail"])
        inspected = self.client.get(f"/goals/{goal_id}", headers=self.headers)
        self.assertEqual(inspected.status_code, 200)
        evaluation = inspected.json()["evaluation"]
        self.assertEqual(evaluation["status"], "completion_review_required")
        self.assertFalse(evaluation["sufficient"])
        self.assertEqual(evaluation["completion_review"]["risk_values"], ["high"])
        self.assertEqual(
            evaluation["missing_evidence"][-1]["criterion_id"],
            "independent_completion_review",
        )

        self_approved = self.client.post(
            f"/goals/{goal_id}/complete",
            headers=self.headers,
            json={
                "evidence": [
                    {
                        "type": "final_approval",
                        "id": "approval-worker-self",
                        "status": "approved",
                        "approved_by": "worker-agent",
                        "agent_id": "worker-agent",
                    }
                ]
            },
        )
        self.assertEqual(self_approved.status_code, 409)
        self.assertIn("independent review", self_approved.json()["detail"])

        completed = self.client.post(
            f"/goals/{goal_id}/complete",
            headers=self.headers,
            json={
                "evaluation": {
                    "reviewer_role": "evaluation_agent",
                    "reviewer_actor": "evaluation-agent",
                    "confidence": "high",
                    "rationale": "Independent evidence review passed.",
                }
            },
        )
        self.assertEqual(completed.status_code, 200)
        payload = completed.json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["evaluation"]["status"], "sufficient")
        self.assertEqual(payload["evaluation"]["reviewer_evaluation"]["reviewer_role"], "evaluation_agent")

    def test_goal_completion_supports_human_final_approval_policy(self):
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={
                "objective": "Send external launch notice",
                "successCriteria": [{"id": "artifact", "kind": "artifact", "description": "Launch proof"}],
                "constraints": {"approval_policy": {"final_approval_required": True}},
            },
        )
        self.assertEqual(created.status_code, 201)
        goal_id = created.json()["id"]

        blocked = self.client.post(
            f"/goals/{goal_id}/complete",
            headers=self.headers,
            json={"evidence": [{"type": "artifact", "id": "artifact-launch", "criterion_id": "artifact"}]},
        )
        self.assertEqual(blocked.status_code, 409)

        approved = self.client.post(
            f"/goals/{goal_id}/complete",
            headers=self.headers,
            json={
                "evidence": [
                    {
                        "type": "final_approval",
                        "id": "approval-launch-final",
                        "status": "approved",
                        "approved_by": "operator-1",
                    }
                ]
            },
        )
        self.assertEqual(approved.status_code, 200)
        payload = approved.json()
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["evaluation"]["completion_review"]["final_approval_required"])

    def test_goal_planning_versions_active_plan(self):
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={
                "objective": "Run a release evidence workflow",
                "successCriteria": [{"id": "artifact", "kind": "artifact", "description": "Artifact exists"}],
                "constraints": {"workflow_id": "workflow-release-evidence", "workflow_input": {"topic": "release"}},
            },
        )
        self.assertEqual(created.status_code, 201)
        goal_id = created.json()["id"]

        planned = self.client.post(f"/goals/{goal_id}/plan", headers=self.headers, json={})
        self.assertEqual(planned.status_code, 200)
        plan = planned.json()["metadata"]["goal_planning"]["active_plan"]
        self.assertEqual(plan["version"], 1)
        self.assertEqual(plan["steps"][0]["action"], "start_workflow")
        self.assertEqual(plan["steps"][0]["workflow_id"], "workflow-release-evidence")
        self.assertEqual(plan["steps"][0]["input_payload"], {"topic": "release"})
        self.assertEqual(planned.json()["status"], "planning")

        replacement = {
            "summary": "Inspect then evaluate evidence",
            "steps": [
                {"action": "inspect_execution", "execution_id": "execution-1"},
                {"action": "evaluate_evidence", "expected_evidence": [{"id": "artifact"}]},
            ],
        }
        replanned = self.client.post(
            f"/goals/{goal_id}/replan",
            headers=self.headers,
            json={"reason": "Execution completed and evidence must be checked", "plan": replacement},
        )
        self.assertEqual(replanned.status_code, 200)
        planning = replanned.json()["metadata"]["goal_planning"]
        self.assertEqual(planning["active_plan"]["version"], 2)
        self.assertEqual(planning["active_plan"]["reason"], "Execution completed and evidence must be checked")
        self.assertEqual(planning["plan_history"][0]["version"], 1)
        self.assertEqual(replanned.json()["status"], "active")

    def test_goal_planning_requires_success_criteria_or_completion_condition(self):
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={"objective": "Open ended autonomous goal"},
        )
        self.assertEqual(created.status_code, 201)

        planned = self.client.post(f"/goals/{created.json()['id']}/plan", headers=self.headers, json={})

        self.assertEqual(planned.status_code, 422)
        self.assertIn("success criterion", planned.json()["detail"])

    def test_goal_supervisor_decisions_are_durable_and_listable(self):
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={
                "objective": "Recover blocked autonomous work",
                "successCriteria": [{"id": "fixed", "kind": "artifact", "description": "Fix evidence"}],
            },
        )
        self.assertEqual(created.status_code, 201)
        goal_id = created.json()["id"]

        recorded = self.client.post(
            f"/goals/{goal_id}/supervisor-decisions",
            headers=self.headers,
            json={
                "decision": {
                    "action": "request_replan",
                    "rationale": "The active plan is stale",
                    "risk": "medium",
                    "requires_approval": True,
                    "approval_request_id": "approval-1",
                    "policy": {"mode": "guarded"},
                }
            },
        )
        self.assertEqual(recorded.status_code, 200)
        decisions = recorded.json()["metadata"]["main_agent_monitoring"]["supervisor_decisions"]
        self.assertEqual(decisions[0]["action"], "request_replan")
        self.assertEqual(decisions[0]["actor"], "user-goals")
        self.assertEqual(decisions[0]["approval_request_id"], "approval-1")

        listed = self.client.get(f"/goals/{goal_id}/supervisor-findings", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["decision_count"], 1)
        self.assertEqual(listed.json()["decisions"][0]["rationale"], "The active plan is stale")

    def test_goal_operator_view_and_detail_expose_supervision_state(self):
        workflow = WorkflowDefinition(
            id="workflow-goal-operator-view",
            name="Goal Operator View Workflow",
            entrypoint="task-1",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-task-1",
                    name="Task 1",
                    node_type=NodeType.TASK,
                    task_id="task-1",
                )
            ],
            task_definitions=[
                TaskDefinition(id="task-1", name="Task 1", description="Collect operator view evidence")
            ],
        )
        self._run(self.context.workflow_repo.create(workflow))
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={
                "objective": "Show operator supervision state",
                "priority": "high",
                "status": "active",
                "successCriteria": [{"id": "evidence", "kind": "artifact", "description": "Evidence exists"}],
                "constraints": {"autonomy": "guarded"},
                "metadata": {
                    "goal_planning": {
                        "active_plan": {
                            "version": 2,
                            "steps": [
                                {
                                    "id": "step-evaluate",
                                    "action": "evaluate_evidence",
                                    "status": "pending",
                                }
                            ],
                        }
                    },
                    "main_agent_monitoring": {
                        "findings": [
                            {
                                "id": "finding-stale",
                                "recorded_at": "2026-07-02T00:00:00+00:00",
                                "finding": {
                                    "category": "goal_execution_signal",
                                    "reason": "Linked execution is stale",
                                    "evidence": {
                                        "signal_category": "stale_execution",
                                        "recommended_action": "repair_stale_execution",
                                    },
                                },
                            }
                        ],
                        "approval_requests": [
                            {
                                "approval_request_id": "approval-goal-operator",
                                "status": "pending",
                                "recommended_action": "external_write",
                            }
                        ],
                        "supervisor_actions": [
                            {
                                "action": "repair_stale_execution",
                                "status": "completed",
                                "allowed_by_policy": True,
                                "requires_approval": False,
                            }
                        ],
                    },
                },
            },
        )
        self.assertEqual(created.status_code, 201)
        goal_id = created.json()["id"]
        execution = self._run(
            self.context.execution_store.save_execution(
                Execution(
                    id="execution-goal-operator-view",
                    workflow_id=workflow.id,
                    goal_id=goal_id,
                    runtime_adapter_id="native",
                    status=ExecutionStatus.RUNNING,
                )
            )
        )
        event = self._run(
            self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id=execution.id,
                    workflow_id=workflow.id,
                    event_type=ExecutionEventType.MONITOR_FINDING_CREATED,
                    payload_json={"summary": "Supervisor flagged stale execution"},
                )
            )
        )
        self._run(
            self.context.execution_store.save_artifact(
                ExecutionArtifact(
                    id="artifact-goal-operator-view",
                    execution_id=execution.id,
                    event_id=event.id,
                    artifact_type="diagnostic",
                    name="stale-run-summary",
                    content_json={"summary": "Execution needs repair"},
                )
            )
        )
        patched = self.client.patch(
            f"/goals/{goal_id}",
            headers=self.headers,
            json={
                "executionIds": [execution.id],
                "evidence": [{"id": "artifact-goal-operator-view", "kind": "artifact"}],
                "evaluation": {"status": "missing_evidence", "sufficient": False},
            },
        )
        self.assertEqual(patched.status_code, 200)

        view = self.client.get("/goals/operator-view?active_only=true", headers=self.headers)

        self.assertEqual(view.status_code, 200, view.text)
        body = view.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["summary"]["blocked_count"], 1)
        self.assertEqual(body["summary"]["stale_count"], 1)
        self.assertEqual(body["summary"]["pending_approval_count"], 1)
        self.assertEqual(body["summary"]["automatic_action_count"], 1)
        item = body["items"][0]
        self.assertEqual(item["goal_id"], goal_id)
        self.assertEqual(item["active_plan_version"], 2)
        self.assertEqual(item["active_execution_count"], 1)
        self.assertEqual(item["active_executions"][0]["id"], execution.id)
        self.assertTrue(item["blocked"])
        self.assertTrue(item["flags"]["stale"])
        self.assertTrue(item["flags"]["missing_evidence"])
        self.assertEqual(item["next_supervisor_action"]["type"], "approval")
        self.assertEqual(item["pending_approval_count"], 1)

        detail = self.client.get(f"/goals/{goal_id}/operator-detail", headers=self.headers)

        self.assertEqual(detail.status_code, 200, detail.text)
        detail_body = detail.json()
        self.assertEqual(detail_body["goal"]["id"], goal_id)
        self.assertEqual(detail_body["evidence"][0]["id"], "artifact-goal-operator-view")
        self.assertEqual(detail_body["evaluation"]["status"], "missing_evidence")
        self.assertEqual(detail_body["artifacts"][execution.id][0]["id"], "artifact-goal-operator-view")
        self.assertEqual(detail_body["approvals"][0]["approval_request_id"], "approval-goal-operator")
        self.assertEqual(detail_body["supervisor"]["findings"][0]["id"], "finding-stale")
        self.assertTrue(detail_body["operator_actions"]["pause"])
        self.assertTrue(detail_body["operator_actions"]["adjust_autonomy"])
        self.assertIn(
            "execution_event",
            {entry["type"] for entry in detail_body["timeline"]},
        )

    def test_goal_operator_actions_update_controls_and_record_audit(self):
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={
                "objective": "Operate durable goal controls",
                "status": "active",
                "ownerActor": "operator-old",
                "successCriteria": [{"id": "initial", "kind": "artifact", "description": "Initial evidence"}],
                "constraints": {"autonomy": "advisory"},
            },
        )
        self.assertEqual(created.status_code, 201)
        goal_id = created.json()["id"]

        autonomy = self.client.post(
            f"/goals/{goal_id}/operator-actions",
            headers=self.headers,
            json={"action": "adjust_autonomy", "autonomy": "guarded", "reason": "Allow guarded repairs"},
        )
        self.assertEqual(autonomy.status_code, 200, autonomy.text)
        self.assertEqual(autonomy.json()["constraints"]["autonomy"], "guarded")
        self.assertEqual(
            autonomy.json()["metadata"]["main_agent_monitoring"]["operator_actions"][-1]["action"],
            "adjust_autonomy",
        )

        criteria = self.client.post(
            f"/goals/{goal_id}/operator-actions",
            headers=self.headers,
            json={
                "action": "update_success_criteria",
                "successCriteria": [
                    {"id": "updated", "kind": "artifact", "description": "Updated evidence is attached"}
                ],
            },
        )
        self.assertEqual(criteria.status_code, 200, criteria.text)
        self.assertEqual(criteria.json()["success_criteria"][0]["id"], "updated")

        reassigned = self.client.post(
            f"/goals/{goal_id}/operator-actions",
            headers=self.headers,
            json={"action": "reassign", "ownerActor": "operator-new"},
        )
        self.assertEqual(reassigned.status_code, 200, reassigned.text)
        self.assertEqual(reassigned.json()["owner_actor"], "operator-new")

        paused = self.client.post(
            f"/goals/{goal_id}/operator-actions",
            headers=self.headers,
            json={"action": "pause", "reason": "Pause for operator review"},
        )
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(paused.json()["status"], "paused")

        resumed = self.client.post(
            f"/goals/{goal_id}/operator-actions",
            headers=self.headers,
            json={"action": "resume", "reason": "Continue guarded run"},
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["status"], "active")

        detail = self.client.get(f"/goals/{goal_id}/operator-detail", headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        operator_actions = detail.json()["supervisor"]["operator_actions"]
        self.assertEqual(
            [record["action"] for record in operator_actions],
            ["adjust_autonomy", "update_success_criteria", "reassign", "pause", "resume"],
        )
        self.assertIn("operator_action", {entry["type"] for entry in detail.json()["timeline"]})
        self.assertTrue(detail.json()["operator_actions"]["pause"])
        self.assertTrue(detail.json()["operator_actions"]["adjust_autonomy"])
        self.assertTrue(detail.json()["operator_actions"]["update_success_criteria"])

        cancelled = self.client.post(
            f"/goals/{goal_id}/operator-actions",
            headers=self.headers,
            json={"action": "cancel", "reason": "No longer needed"},
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")

    def test_long_running_goal_is_auditable_governable_and_resumable(self):
        workflow = WorkflowDefinition(
            id="workflow-goal-acceptance-rollup",
            name="Goal Acceptance Rollup Workflow",
            entrypoint="task-1",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-task-1",
                    name="Task 1",
                    node_type=NodeType.TASK,
                    task_id="task-1",
                )
            ],
            task_definitions=[
                TaskDefinition(id="task-1", name="Task 1", description="Maintain long-running goal evidence")
            ],
        )
        self._run(self.context.workflow_repo.create(workflow))
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={
                "objective": "Keep long-running supervised work recoverable",
                "status": "active",
                "successCriteria": [{"id": "evidence", "kind": "artifact", "description": "Evidence exists"}],
                "constraints": {"autonomy": "advisory", "workflow_id": workflow.id},
                "metadata": {
                    "memory_ids": ["memory-rollup-summary"],
                    "goal_planning": {
                        "active_plan": {
                            "version": 1,
                            "steps": [
                                {
                                    "id": "step-maintain",
                                    "action": "start_workflow",
                                    "workflow_id": workflow.id,
                                    "status": "active",
                                }
                            ],
                        }
                    },
                    "main_agent_monitoring": {
                        "findings": [
                            {
                                "id": "finding-rollup-stale",
                                "recorded_at": "2026-07-02T00:00:00+00:00",
                                "finding": {
                                    "category": "goal_execution_signal",
                                    "reason": "Execution heartbeat stalled",
                                    "evidence": {
                                        "signal_category": "stale_execution",
                                        "recommended_action": "repair_stale_execution",
                                    },
                                },
                            }
                        ],
                        "supervisor_decisions": [
                            {
                                "id": "decision-rollup",
                                "action": "request_human_review",
                                "rationale": "Approval is required before external write.",
                                "risk": "high",
                                "approval_request_id": "approval-rollup",
                            }
                        ],
                        "approval_requests": [
                            {
                                "approval_request_id": "approval-rollup",
                                "status": "pending",
                                "recommended_action": "external_write",
                            }
                        ],
                    },
                },
            },
        )
        self.assertEqual(created.status_code, 201)
        goal_id = created.json()["id"]
        execution = self._run(
            self.context.execution_store.save_execution(
                Execution(
                    id="execution-goal-acceptance-rollup",
                    workflow_id=workflow.id,
                    runtime_adapter_id="native",
                    status=ExecutionStatus.RUNNING,
                    input_payload={"goal_id": goal_id},
                )
            )
        )
        event = self._run(
            self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id=execution.id,
                    workflow_id=workflow.id,
                    event_type=ExecutionEventType.MONITOR_FINDING_CREATED,
                    payload_json={"category": "stale_execution", "summary": "Execution needs recovery"},
                )
            )
        )
        self._run(
            self.context.execution_store.save_artifact(
                ExecutionArtifact(
                    id="artifact-goal-acceptance-rollup",
                    execution_id=execution.id,
                    event_id=event.id,
                    artifact_type="diagnostic",
                    name="recovery-diagnostic",
                    content_json={"summary": "Partial output retained for resume"},
                )
            )
        )

        reconciliation = self._run(GoalStartupReconciler(self.context).reconcile_once())

        self.assertEqual(reconciliation.repaired_goal_execution_links, 1)
        self.assertEqual(reconciliation.repaired_execution_goal_links, 1)
        operator_action = self.client.post(
            f"/goals/{goal_id}/operator-actions",
            headers=self.headers,
            json={"action": "adjust_autonomy", "autonomy": "guarded", "reason": "Allow guarded stale-run repair"},
        )
        self.assertEqual(operator_action.status_code, 200, operator_action.text)

        detail = self.client.get(f"/goals/{goal_id}/operator-detail", headers=self.headers)

        self.assertEqual(detail.status_code, 200, detail.text)
        body = detail.json()
        self.assertEqual(body["goal"]["constraints"]["autonomy"], "guarded")
        self.assertEqual(body["active_execution_count"], 1)
        self.assertEqual(body["active_executions"][0]["id"], execution.id)
        self.assertEqual(body["artifacts"][execution.id][0]["id"], "artifact-goal-acceptance-rollup")
        self.assertEqual(body["memory"]["memory_ids"], ["memory-rollup-summary"])
        self.assertEqual(body["approvals"][0]["approval_request_id"], "approval-rollup")
        self.assertEqual(body["supervisor"]["decisions"][0]["id"], "decision-rollup")
        self.assertEqual(body["supervisor"]["operator_actions"][0]["action"], "adjust_autonomy")
        self.assertTrue(body["flags"]["stale"])
        self.assertTrue(body["blocked"])
        timeline_types = {entry["type"] for entry in body["timeline"]}
        self.assertIn("execution_event", timeline_types)
        self.assertIn("supervisor_finding", timeline_types)
        self.assertIn("operator_action", timeline_types)
        events = self._run(self.context.graph_projection_event_repo.list_events(limit=100))
        goal_events = [event for event in events if event.aggregate_type == "goal" and event.aggregate_id == goal_id]
        self.assertIn("goal.operator_action.recorded", [event.event_type for event in goal_events])

    def test_execution_creation_links_to_goal(self):
        workflow = WorkflowDefinition(
            id="workflow-goal-link",
            name="Goal Link Workflow",
            entrypoint="task-1",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-task-1",
                    name="Task 1",
                    node_type=NodeType.TASK,
                    task_id="task-1",
                )
            ],
            task_definitions=[
                TaskDefinition(id="task-1", name="Task 1", description="Do the first goal-linked task")
            ],
        )
        self._run(self.context.workflow_repo.create(workflow))

        created_goal = self.client.post(
            "/goals",
            headers=self.headers,
            json={"objective": "Run workflow under a durable goal"},
        )
        self.assertEqual(created_goal.status_code, 201)
        goal_id = created_goal.json()["id"]

        execution_response = self.client.post(
            "/workflows/workflow-goal-link/executions",
            headers=self.headers,
            json={"goalId": goal_id, "input": {"prompt": "hello"}, "trigger": {"type": "manual"}},
        )
        self.assertEqual(execution_response.status_code, 200)
        execution = execution_response.json()
        self.assertEqual(execution["goal_id"], goal_id)
        self.assertEqual(execution["metadata"]["goal_id"], goal_id)

        goal_response = self.client.get(f"/goals/{goal_id}", headers=self.headers)
        self.assertEqual(goal_response.status_code, 200)
        self.assertEqual(goal_response.json()["execution_ids"], [execution["id"]])

    def test_goal_graph_projection_events_capture_lineage_relationships(self):
        workflow = WorkflowDefinition(
            id="workflow-goal-graph",
            name="Goal Graph Workflow",
            entrypoint="task-1",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-task-1",
                    name="Task 1",
                    node_type=NodeType.TASK,
                    task_id="task-1",
                )
            ],
            task_definitions=[TaskDefinition(id="task-1", name="Task 1", description="Produce evidence")],
        )
        self._run(self.context.workflow_repo.create(workflow))
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={
                "objective": "Build goal graph context",
                "successCriteria": [{"id": "artifact", "kind": "artifact", "description": "Artifact exists"}],
                "metadata": {"memory_ids": ["memory-goal-summary"]},
                "constraints": {"workflow_id": workflow.id},
            },
        )
        self.assertEqual(created.status_code, 201)
        goal_id = created.json()["id"]

        planned = self.client.post(f"/goals/{goal_id}/plan", headers=self.headers, json={})
        self.assertEqual(planned.status_code, 200)
        evidence = self.client.post(
            f"/goals/{goal_id}/evidence",
            headers=self.headers,
            json={
                "evidence": [
                    {"type": "artifact", "id": "artifact-goal-1", "criterion_id": "artifact"},
                    {"type": "memory", "id": "memory-evidence-1"},
                ]
            },
        )
        self.assertEqual(evidence.status_code, 200)
        evaluated = self.client.post(
            f"/goals/{goal_id}/evaluate",
            headers=self.headers,
            json={"persist": True},
        )
        self.assertEqual(evaluated.status_code, 200)
        decision = self.client.post(
            f"/goals/{goal_id}/supervisor-decisions",
            headers=self.headers,
            json={
                "decision": {
                    "id": "decision-graph-1",
                    "action": "request_replan",
                    "risk": "medium",
                    "approval_request_id": "approval-graph-1",
                }
            },
        )
        self.assertEqual(decision.status_code, 200)
        execution_response = self.client.post(
            f"/workflows/{workflow.id}/executions",
            headers=self.headers,
            json={"goalId": goal_id, "input": {}, "trigger": {"type": "manual"}},
        )
        self.assertEqual(execution_response.status_code, 200)
        execution_id = execution_response.json()["id"]

        events = self._run(self.context.graph_projection_event_repo.list_events(limit=100))
        goal_events = [event for event in events if event.aggregate_type == "goal" and event.aggregate_id == goal_id]
        event_types = [event.event_type for event in goal_events]
        self.assertIn("goal.created", event_types)
        self.assertIn("goal.plan_versioned", event_types)
        self.assertIn("goal.evidence_attached", event_types)
        self.assertIn("goal.evaluation_recorded", event_types)
        self.assertIn("goal.supervisor_decision.recorded", event_types)
        self.assertIn("goal.execution_linked", event_types)
        linked = next(event for event in goal_events if event.event_type == "goal.execution_linked")
        self.assertEqual(linked.payload["relationships"]["execution_ids"], [execution_id])
        self.assertEqual(linked.payload["relationships"]["linked_execution_ids"], [execution_id])
        self.assertIn("artifact-goal-1", linked.payload["relationships"]["artifact_ids"])
        self.assertIn("memory-goal-summary", linked.payload["relationships"]["memory_ids"])
        self.assertIn("memory-evidence-1", linked.payload["relationships"]["memory_ids"])
        decision_event = next(event for event in goal_events if event.event_type == "goal.supervisor_decision.recorded")
        self.assertIn("decision-graph-1", decision_event.payload["relationships"]["supervisor_decision_ids"])
        self.assertIn("approval-graph-1", decision_event.payload["relationships"]["approval_request_ids"])
        plan_event = next(event for event in goal_events if event.event_type == "goal.plan_versioned")
        self.assertEqual(plan_event.payload["active_plan"]["steps"][0]["workflow_id"], workflow.id)

    def test_goal_memory_summary_preserves_constraints_approvals_and_blockers(self):
        workflow = WorkflowDefinition(
            id="workflow-goal-memory",
            name="Goal Memory Workflow",
            entrypoint="task-1",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-task-1",
                    name="Task 1",
                    node_type=NodeType.TASK,
                    task_id="task-1",
                )
            ],
            task_definitions=[TaskDefinition(id="task-1", name="Task 1", description="Maintain goal evidence")],
        )
        self._run(self.context.workflow_repo.create(workflow))
        created = self.client.post(
            "/goals",
            headers=self.headers,
            json={
                "objective": "Preserve long running goal context",
                "successCriteria": [{"id": "artifact", "kind": "artifact", "description": "Artifact exists"}],
                "constraints": {
                    "workflow_id": workflow.id,
                    "approval_policy": {"approval_required_actions": ["external_write"]},
                },
            },
        )
        self.assertEqual(created.status_code, 201)
        goal_id = created.json()["id"]

        planned = self.client.post(f"/goals/{goal_id}/plan", headers=self.headers, json={})
        self.assertEqual(planned.status_code, 200)
        patched = self.client.patch(
            f"/goals/{goal_id}",
            headers=self.headers,
            json={
                "metadata": {
                    **planned.json()["metadata"],
                    "main_agent_monitoring": {
                        "approval_requests": [
                            {
                                "approval_request_id": "approval-memory-1",
                                "status": "pending",
                                "recommended_action": "external_write",
                            }
                        ],
                        "findings": [
                            {
                                "dedupe_key": "finding-memory-1",
                                "finding": {"category": "stalled_goal"},
                            }
                        ],
                    },
                }
            },
        )
        self.assertEqual(patched.status_code, 200)

        first = self.client.post(
            f"/goals/{goal_id}/memory-summary",
            headers=self.headers,
            json={"reason": "first_compaction"},
        )
        self.assertEqual(first.status_code, 200)
        first_memory_id = first.json()["metadata"]["goal_memory"]["latest_summary_memory_id"]
        second = self.client.post(
            f"/goals/{goal_id}/memory-summary",
            headers=self.headers,
            json={"reason": "refresh_compaction"},
        )
        self.assertEqual(second.status_code, 200)
        second_memory_id = second.json()["metadata"]["goal_memory"]["latest_summary_memory_id"]
        self.assertNotEqual(first_memory_id, second_memory_id)
        self.assertEqual(second.json()["metadata"]["goal_memory"]["previous_summary_memory_id"], first_memory_id)
        self.assertIn(second_memory_id, second.json()["metadata"]["memory_ids"])
        self.assertNotIn(first_memory_id, second.json()["metadata"]["memory_ids"])

        first_memory = self._run(self.context.memory_repo.get(first_memory_id))
        second_memory = self._run(self.context.memory_repo.get(second_memory_id))
        self.assertEqual(first_memory.scope.value, "workflow")
        self.assertEqual(second_memory.workflow_id, workflow.id)
        self.assertEqual(second_memory.supersedes_memory_id, first_memory_id)
        self.assertEqual(second_memory.metadata["goal_id"], goal_id)
        self.assertEqual(
            second_memory.metadata["preserved_constraints"]["approval_policy"]["approval_required_actions"],
            ["external_write"],
        )
        self.assertEqual(second_memory.metadata["preserved_approvals"][0]["approval_request_id"], "approval-memory-1")
        self.assertEqual(second_memory.metadata["preserved_blockers"][0]["type"], "approval")
        self.assertTrue(second_memory.metadata["next_actions"])
        self.assertIn("Unresolved blockers", second_memory.content)

        events = self._run(self.context.graph_projection_event_repo.list_events(limit=100))
        summary_events = [
            event
            for event in events
            if event.aggregate_type == "goal" and event.aggregate_id == goal_id
            and event.event_type == "goal.memory_summary.stored"
        ]
        self.assertEqual(len(summary_events), 2)
        self.assertEqual(summary_events[-1].payload["memory_id"], second_memory_id)

    def test_goal_system_tools_manage_goal_and_link_workflow_execution(self):
        workflow = WorkflowDefinition(
            id="workflow-goal-tool-link",
            name="Goal Tool Link Workflow",
            entrypoint="task-1",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-task-1",
                    name="Task 1",
                    node_type=NodeType.TASK,
                    task_id="task-1",
                )
            ],
            task_definitions=[
                TaskDefinition(id="task-1", name="Task 1", description="Run from a goal system tool")
            ],
        )
        self._run(self.context.workflow_repo.create(workflow))
        executor = ToolRuntimeExecutor(context=self.context)

        created = self._run(
            executor.run_async(
                "agency.goal.create",
                {
                    "objective": "Keep a supervised workflow moving",
                    "success_criteria": [{"kind": "execution", "description": "Workflow execution is linked"}],
                },
                actor="agent-main",
            )
        )
        self.assertEqual(created.result["status"], "ok")
        goal_id = created.result["goal"]["id"]

        listed = self._run(executor.run_async("agency.goal.list", {"active_only": True}, actor="agent-main"))
        self.assertEqual(listed.result["status"], "ok")
        self.assertEqual(listed.result["count"], 1)

        paused = self._run(executor.run_async("agency.goal.pause", {"goal_id": goal_id}, actor="agent-main"))
        self.assertEqual(paused.result["goal"]["status"], "paused")

        resumed = self._run(executor.run_async("agency.goal.resume", {"goal_id": goal_id}, actor="agent-main"))
        self.assertEqual(resumed.result["goal"]["status"], "active")

        run = self._run(
            executor.run_async(
                "agency.workflow.run",
                {"workflow_id": "workflow-goal-tool-link", "input_payload": {"topic": "goals"}, "goal_id": goal_id},
                actor="agent-main",
            )
        )
        self.assertEqual(run.result["status"], "queued")
        self.assertEqual(run.result["execution"]["goal_id"], goal_id)

        fetched = self._run(executor.run_async("agency.goal.get", {"goal_id": goal_id}, actor="agent-main"))
        self.assertEqual(fetched.result["goal"]["execution_ids"], [run.result["execution_id"]])

        completed = self._run(
            executor.run_async(
                "agency.goal.complete",
                {
                    "goal_id": goal_id,
                    "evidence": [{"type": "execution", "id": run.result["execution_id"]}],
                    "evaluation": {"confidence": "medium"},
                },
                actor="agent-main",
            )
        )
        self.assertEqual(completed.result["goal"]["status"], "completed")
        self.assertEqual(completed.result["goal"]["evaluation"]["status"], "sufficient")

    def test_goal_system_tool_rejects_high_risk_worker_only_completion(self):
        executor = ToolRuntimeExecutor(context=self.context)
        created = self._run(
            executor.run_async(
                "agency.goal.create",
                {
                    "objective": "Ship high-risk production change",
                    "priority": "high",
                    "success_criteria": [{"id": "artifact", "kind": "artifact", "description": "Change proof"}],
                    "constraints": {"risk_level": "high"},
                },
                actor="agent-main",
            )
        )
        goal_id = created.result["goal"]["id"]

        rejected = self._run(
            executor.run_async(
                "agency.goal.complete",
                {
                    "goal_id": goal_id,
                    "evidence": [
                        {
                            "type": "artifact",
                            "id": "artifact-worker-tool",
                            "criterion_id": "artifact",
                            "agent_id": "worker-agent",
                        }
                    ],
                },
                actor="worker-agent",
            )
        )
        self.assertEqual(rejected.result["status"], "error")
        self.assertIn("independent review", rejected.result["error"])

        approved = self._run(
            executor.run_async(
                "agency.goal.complete",
                {
                    "goal_id": goal_id,
                    "evaluation": {
                        "reviewer_role": "human",
                        "reviewer_actor": "operator-1",
                        "confidence": "high",
                    },
                },
                actor="agent-main",
            )
        )
        self.assertEqual(approved.result["goal"]["status"], "completed")
        self.assertEqual(approved.result["goal"]["evaluation"]["status"], "sufficient")

    def test_goal_system_tools_attach_and_evaluate_evidence(self):
        executor = ToolRuntimeExecutor(context=self.context)
        created = self._run(
            executor.run_async(
                "agency.goal.create",
                {
                    "objective": "Gather deployment approval",
                    "success_criteria": [{"id": "approval", "kind": "approval", "description": "Approval exists"}],
                },
                actor="agent-main",
            )
        )
        goal_id = created.result["goal"]["id"]

        insufficient = self._run(
            executor.run_async("agency.goal.evaluate", {"goal_id": goal_id, "persist": True}, actor="agent-main")
        )
        self.assertEqual(insufficient.result["evaluation"]["status"], "missing_evidence")

        attached = self._run(
            executor.run_async(
                "agency.goal.evidence.attach",
                {"goal_id": goal_id, "evidence": [{"type": "approval", "id": "approval-1"}]},
                actor="agent-main",
            )
        )
        self.assertEqual(attached.result["goal"]["evidence"][0]["id"], "approval-1")

        evaluated = self._run(
            executor.run_async("agency.goal.evaluate", {"goal_id": goal_id}, actor="agent-main")
        )
        self.assertEqual(evaluated.result["evaluation"]["status"], "sufficient")

    def test_goal_system_tools_plan_and_replan_goal(self):
        executor = ToolRuntimeExecutor(context=self.context)
        created = self._run(
            executor.run_async(
                "agency.goal.create",
                {
                    "objective": "Supervise a deployment workflow",
                    "success_criteria": [{"id": "deployment", "kind": "artifact", "description": "Deployment notes"}],
                },
                actor="agent-main",
            )
        )
        goal_id = created.result["goal"]["id"]

        planned = self._run(
            executor.run_async(
                "agency.goal.plan",
                {
                    "goal_id": goal_id,
                    "plan": {
                        "steps": [
                            {
                                "action": "start_workflow",
                                "workflow_id": "workflow-deploy",
                                "input_payload": {"environment": "staging"},
                                "assigned_agents": ["agent-deploy"],
                                "expected_evidence": [{"id": "deployment"}],
                            }
                        ]
                    },
                },
                actor="agent-main",
            )
        )
        self.assertEqual(planned.result["goal"]["metadata"]["goal_planning"]["active_plan"]["version"], 1)

        replanned = self._run(
            executor.run_async(
                "agency.goal.replan",
                {
                    "goal_id": goal_id,
                    "reason": "Deployment workflow finished; evaluate the evidence",
                    "plan": {"steps": [{"action": "evaluate_evidence", "expected_evidence": [{"id": "deployment"}]}]},
                },
                actor="agent-main",
            )
        )
        planning = replanned.result["goal"]["metadata"]["goal_planning"]
        self.assertEqual(planning["active_plan"]["version"], 2)
        self.assertEqual(planning["plan_history"][0]["version"], 1)

    def test_goal_system_tools_record_and_list_supervisor_decisions(self):
        executor = ToolRuntimeExecutor(context=self.context)
        created = self._run(
            executor.run_async(
                "agency.goal.create",
                {
                    "objective": "Audit supervisor decisions",
                    "success_criteria": [{"id": "decision", "kind": "artifact", "description": "Decision record"}],
                },
                actor="agent-main",
            )
        )
        goal_id = created.result["goal"]["id"]

        recorded = self._run(
            executor.run_async(
                "agency.goal.supervisor-decision.record",
                {
                    "goal_id": goal_id,
                    "decision": {
                        "action": "request_human_review",
                        "rationale": "High-risk external write requires approval",
                        "requires_approval": True,
                        "risk": "high",
                    },
                },
                actor="agent-main",
            )
        )
        monitoring = recorded.result["goal"]["metadata"]["main_agent_monitoring"]
        self.assertEqual(monitoring["supervisor_decisions"][0]["action"], "request_human_review")

        listed = self._run(
            executor.run_async("agency.goal.supervisor-findings", {"goal_id": goal_id}, actor="agent-main")
        )
        self.assertEqual(listed.result["decision_count"], 1)
        self.assertEqual(listed.result["decisions"][0]["risk"], "high")

    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.run(coro)
