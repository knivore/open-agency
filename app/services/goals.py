"""Service layer for durable goal records and lifecycle transitions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.api.context import ApiContext
from app.core.config import get_settings
from app.core.time import utc_now
from app.domain import Execution, ExecutionEvent, ExecutionEventType, ExecutionStatus, GoalDefinition, GoalStatus, \
    GraphProjectionEvent
from app.domain.goals import TERMINAL_GOAL_STATUSES

logger = logging.getLogger(__name__)

HIGH_RISK_GOAL_LEVELS = {"high", "critical", "unsafe"}
FINAL_APPROVAL_EVIDENCE_KINDS = {"approval", "final_approval", "human_approval", "operator_approval"}
INDEPENDENT_REVIEWER_ROLES = {"human", "operator", "main_agent", "evaluation_agent", "supervisor", "reviewer"}
HIGH_RISK_APPROVAL_ACTIONS = {
    "delete",
    "destructive_action",
    "external_write",
    "physical_world_action",
    "purchase",
    "shell_side_effect",
    "tool_definition_mutation",
    "workflow_definition_mutation",
}

ACTIVE_EXECUTION_STATUSES = {
    ExecutionStatus.CREATED,
    ExecutionStatus.QUEUED,
    ExecutionStatus.RUNNING,
    ExecutionStatus.WAITING_FOR_INPUT,
    ExecutionStatus.WAITING_FOR_APPROVAL,
    ExecutionStatus.WAITING_FOR_EVENT,
    ExecutionStatus.SLEEPING,
    ExecutionStatus.PAUSED,
    ExecutionStatus.CANCELLING,
}


class GoalNotFoundError(ValueError):
    pass


class GoalTransitionError(ValueError):
    pass


class GoalEvaluator:
    """Deterministic evaluator for evidence-gated goal completion."""

    @classmethod
    def evaluate(cls, goal: GoalDefinition, evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        evidence_records = list(evidence if evidence is not None else goal.evidence)
        criteria = list(goal.success_criteria)
        criteria_results: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for index, criterion in enumerate(criteria):
            matches = cls._matching_evidence(criterion, evidence_records)
            criterion_id = cls._criterion_id(criterion, index)
            result = {
                "criterion_id": criterion_id,
                "criterion": criterion,
                "satisfied": bool(matches),
                "evidence_ids": [
                    str(item.get("id") or item.get("artifact_id") or item.get("execution_id") or "")
                    for item in matches
                ],
                "evidence": matches,
            }
            criteria_results.append(result)
            if not matches:
                missing.append(
                    {
                        "criterion_id": criterion_id,
                        "criterion": criterion,
                        "reason": "No evidence matched this success criterion by criterion id or kind/type.",
                    }
                )

        if not evidence_records:
            missing.append(
                {
                    "criterion_id": "completion_evidence",
                    "criterion": {"kind": "evidence", "description": "At least one completion evidence record"},
                    "reason": "Goal completion requires at least one evidence record.",
                }
            )

        sufficient = bool(evidence_records) and not missing
        confidence = "high" if sufficient and criteria else "medium" if sufficient else "low"
        return {
            "status": "sufficient" if sufficient else "missing_evidence",
            "sufficient": sufficient,
            "confidence": confidence,
            "checked_at": utc_now().isoformat(),
            "criteria_count": len(criteria),
            "evidence_count": len(evidence_records),
            "criteria_results": criteria_results,
            "missing_evidence": missing,
            "rationale": (
                "All success criteria have matching evidence."
                if sufficient and criteria
                else "Completion evidence is present and no structured success criteria were defined."
                if sufficient
                else "Goal cannot be completed until missing evidence is supplied."
            ),
        }

    @classmethod
    def _matching_evidence(cls, criterion: dict[str, Any], evidence_records: list[dict[str, Any]]) -> list[
        dict[str, Any]]:
        criterion_ids = {
            str(value)
            for key in ("id", "criterion_id", "key", "name")
            if (value := criterion.get(key)) and isinstance(value, str | int | float | bool)
        }
        criterion_kind = str(criterion.get("kind") or criterion.get("type") or "").strip().lower()
        matches: list[dict[str, Any]] = []
        for evidence in evidence_records:
            if cls._evidence_references_criterion(evidence, criterion_ids):
                matches.append(evidence)
                continue
            evidence_kind = str(evidence.get("kind") or evidence.get("type") or "").strip().lower()
            if criterion_kind and evidence_kind == criterion_kind:
                matches.append(evidence)
        return matches

    @staticmethod
    def _criterion_id(criterion: dict[str, Any], index: int) -> str:
        for key in ("id", "criterion_id", "key", "name"):
            value = criterion.get(key)
            if value:
                return str(value)
        return f"criterion-{index + 1}"

    @staticmethod
    def _evidence_references_criterion(evidence: dict[str, Any], criterion_ids: set[str]) -> bool:
        if not criterion_ids:
            return False
        candidates = {
            evidence.get("criterion_id"),
            evidence.get("success_criterion_id"),
            evidence.get("satisfies_criterion_id"),
        }
        satisfies = evidence.get("satisfies")
        if isinstance(satisfies, list):
            candidates.update(satisfies)
        elif satisfies:
            candidates.add(satisfies)
        criteria_ids = evidence.get("criteria_ids")
        if isinstance(criteria_ids, list):
            candidates.update(criteria_ids)
        return bool({str(item) for item in candidates if item}.intersection(criterion_ids))


@dataclass(slots=True)
class GoalStartupReconciliationReport:
    active_goals_scanned: int
    executions_scanned: int
    repaired_goal_execution_links: int
    repaired_execution_goal_links: int
    orphaned_goal_execution_references: int
    orphaned_execution_goal_references: int
    findings: list[dict[str, Any]]

    def model_dump(self) -> dict[str, Any]:
        return {
            "active_goals_scanned": self.active_goals_scanned,
            "executions_scanned": self.executions_scanned,
            "repaired_goal_execution_links": self.repaired_goal_execution_links,
            "repaired_execution_goal_links": self.repaired_execution_goal_links,
            "orphaned_goal_execution_references": self.orphaned_goal_execution_references,
            "orphaned_execution_goal_references": self.orphaned_execution_goal_references,
            "findings": self.findings,
        }


@dataclass(slots=True)
class GoalStartupReconciler:
    """Repair durable goal/execution links after process restart before supervision resumes."""

    context: ApiContext

    async def reconcile_once(self) -> GoalStartupReconciliationReport:
        now = utc_now()
        goals = await self.context.goal_repo.list()
        active_goals = [goal for goal in goals if goal.status in ACTIVE_GOAL_STATUSES]
        goals_by_id = {goal.id: goal for goal in goals}
        executions = await self.context.execution_store.list_executions()
        executions_by_id = {execution.id: execution for execution in executions}

        repaired_goal_execution_links = 0
        repaired_execution_goal_links = 0
        orphaned_goal_execution_references = 0
        orphaned_execution_goal_references = 0
        findings: list[dict[str, Any]] = []

        for goal in active_goals:
            missing_execution_ids: list[str] = []
            repaired_execution_ids: list[str] = []
            for execution_id in goal.execution_ids:
                execution = executions_by_id.get(execution_id)
                if execution is None:
                    missing_execution_ids.append(execution_id)
                    orphaned_goal_execution_references += 1
                    continue
                if execution.goal_id != goal.id:
                    await self._save_execution_goal_link(execution, goal.id, reason="goal_execution_reference")
                    repaired_execution_ids.append(execution.id)
                    repaired_execution_goal_links += 1
                    await self._record_reconciliation_event(
                        execution,
                        goal_id=goal.id,
                        category="execution_goal_link_repaired",
                        reason="Active goal listed the execution but the execution did not point back to the goal.",
                    )
            if missing_execution_ids or repaired_execution_ids:
                goal = await self._record_goal_reconciliation(
                    goal,
                    now=now,
                    missing_execution_ids=missing_execution_ids,
                    repaired_execution_ids=repaired_execution_ids,
                )
                goals_by_id[goal.id] = goal
                for execution_id in missing_execution_ids:
                    findings.append(
                        {
                            "category": "orphaned_goal_execution_reference",
                            "goal_id": goal.id,
                            "execution_id": execution_id,
                            "reason": "Active goal references an execution that no longer exists.",
                        }
                    )

        for execution in executions:
            goal_id = self._execution_goal_hint(execution)
            if not goal_id:
                continue
            goal = goals_by_id.get(goal_id)
            if goal is None:
                orphaned_execution_goal_references += 1
                findings.append(
                    {
                        "category": "orphaned_execution_goal_reference",
                        "execution_id": execution.id,
                        "goal_id": goal_id,
                        "reason": "Execution references a goal that no longer exists.",
                    }
                )
                await self._flag_orphaned_execution_goal_reference(execution, goal_id=goal_id)
                await self._record_reconciliation_event(
                    execution,
                    goal_id=goal_id,
                    category="orphaned_execution_goal_reference",
                    reason="Execution references a goal that no longer exists.",
                )
                continue
            if execution.id not in goal.execution_ids:
                updated_goal = goal.model_copy(
                    update={
                        "execution_ids": [*goal.execution_ids, execution.id],
                        "updated_at": now,
                    }
                )
                goals_by_id[goal.id] = await self.context.goal_repo.save(updated_goal)
                repaired_goal_execution_links += 1
            if execution.goal_id != goal.id:
                await self._save_execution_goal_link(execution, goal.id, reason="execution_goal_hint")
                repaired_execution_goal_links += 1
                await self._record_reconciliation_event(
                    execution,
                    goal_id=goal.id,
                    category="execution_goal_link_repaired",
                    reason="Execution metadata referenced the goal but the canonical execution goal_id was missing.",
                )

        report = GoalStartupReconciliationReport(
            active_goals_scanned=len(active_goals),
            executions_scanned=len(executions),
            repaired_goal_execution_links=repaired_goal_execution_links,
            repaired_execution_goal_links=repaired_execution_goal_links,
            orphaned_goal_execution_references=orphaned_goal_execution_references,
            orphaned_execution_goal_references=orphaned_execution_goal_references,
            findings=findings,
        )
        self.context.runtime_operations.increment("goal_startup_reconciliation.runs")
        if repaired_goal_execution_links:
            self.context.runtime_operations.increment(
                "goal_startup_reconciliation.repaired_goal_execution_links",
                repaired_goal_execution_links,
            )
        if repaired_execution_goal_links:
            self.context.runtime_operations.increment(
                "goal_startup_reconciliation.repaired_execution_goal_links",
                repaired_execution_goal_links,
            )
        if orphaned_goal_execution_references or orphaned_execution_goal_references:
            self.context.runtime_operations.increment(
                "goal_startup_reconciliation.orphaned_references",
                orphaned_goal_execution_references + orphaned_execution_goal_references,
            )
        self.context.runtime_operations.record_action("goal_startup_reconciliation.completed", **report.model_dump())
        return report

    @staticmethod
    def _execution_goal_hint(execution: Execution) -> str | None:
        for source in (
                execution.goal_id,
                execution.metadata.get("goal_id"),
                execution.trigger_payload.get("goal_id"),
                execution.input_payload.get("goal_id"),
        ):
            if isinstance(source, str) and source.strip():
                return source.strip()
        return None

    async def _save_execution_goal_link(self, execution: Execution, goal_id: str, *, reason: str) -> Execution:
        metadata = dict(execution.metadata)
        metadata["goal_id"] = goal_id
        reconciliation = dict(metadata.get("goal_startup_reconciliation") or {})
        reconciliation.update({"last_repaired_at": utc_now().isoformat(), "reason": reason})
        metadata["goal_startup_reconciliation"] = reconciliation
        trigger_payload = {**execution.trigger_payload, "goal_id": goal_id}
        input_payload = {**execution.input_payload, "goal_id": goal_id}
        return await self.context.execution_store.update_execution(
            execution.model_copy(
                update={
                    "goal_id": goal_id,
                    "trigger_payload": trigger_payload,
                    "input_payload": input_payload,
                    "metadata": metadata,
                    "updated_at": utc_now(),
                }
            )
        )

    async def _flag_orphaned_execution_goal_reference(self, execution: Execution, *, goal_id: str) -> Execution:
        metadata = dict(execution.metadata)
        reconciliation = dict(metadata.get("goal_startup_reconciliation") or {})
        reconciliation.update(
            {
                "last_flagged_at": utc_now().isoformat(),
                "status": "orphaned_execution_goal_reference",
                "goal_id": goal_id,
            }
        )
        metadata["goal_startup_reconciliation"] = reconciliation
        return await self.context.execution_store.update_execution(
            execution.model_copy(update={"metadata": metadata, "updated_at": utc_now()})
        )

    async def _record_goal_reconciliation(
            self,
            goal: GoalDefinition,
            *,
            now,
            missing_execution_ids: list[str],
            repaired_execution_ids: list[str],
    ) -> GoalDefinition:
        metadata = dict(goal.metadata)
        reconciliation = dict(metadata.get("goal_startup_reconciliation") or {})
        reconciliation.update(
            {
                "last_reconciled_at": now.isoformat(),
                "missing_execution_ids": missing_execution_ids,
                "repaired_execution_ids": repaired_execution_ids,
            }
        )
        metadata["goal_startup_reconciliation"] = reconciliation
        return await self.context.goal_repo.save(goal.model_copy(update={"metadata": metadata, "updated_at": now}))

    async def _record_reconciliation_event(
            self,
            execution: Execution,
            *,
            goal_id: str,
            category: str,
            reason: str,
    ) -> None:
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                event_type=ExecutionEventType.MONITOR_FINDING_CREATED,
                actor_type="system",
                actor_id="goal_startup_reconciler",
                payload_json={
                    "category": category,
                    "execution_id": execution.id,
                    "workflow_id": execution.workflow_id,
                    "goal_id": goal_id,
                    "reason": reason,
                    "source": "goal_startup_reconciler",
                },
                metadata={"source": "goal_startup_reconciler", "category": category, "goal_id": goal_id},
            )
        )


ACTIVE_GOAL_STATUSES = {
    GoalStatus.CREATED,
    GoalStatus.PLANNING,
    GoalStatus.ACTIVE,
    GoalStatus.WAITING_FOR_INPUT,
    GoalStatus.WAITING_FOR_APPROVAL,
    GoalStatus.PAUSED,
}

GOAL_PLAN_STEP_ACTIONS = {
    "start_workflow",
    "inspect_execution",
    "retrieve_memory",
    "ask_for_input",
    "request_approval",
    "evaluate_evidence",
}


@dataclass(slots=True)
class GoalService:
    context: ApiContext

    async def create_goal(self, payload: dict[str, Any]) -> GoalDefinition:
        goal = GoalDefinition.model_validate(payload)
        created = await self.context.goal_repo.create(goal)
        await self._append_goal_projection_event("goal.created", created)
        return created

    async def list_goals(
            self,
            *,
            status: str | None = None,
            parent_goal_id: str | None = None,
            active_only: bool = False,
    ) -> dict[str, Any]:
        goals = await self.context.goal_repo.list()
        if status:
            normalized = GoalStatus(status)
            goals = [goal for goal in goals if goal.status == normalized]
        if parent_goal_id:
            goals = [goal for goal in goals if goal.parent_goal_id == parent_goal_id]
        if active_only:
            goals = [goal for goal in goals if goal.status in ACTIVE_GOAL_STATUSES]
        goals = sorted(goals, key=lambda goal: goal.created_at, reverse=True)
        return {
            "items": [goal.model_dump(mode="json") for goal in goals],
            "count": len(goals),
            "filters": {
                "status": status,
                "parent_goal_id": parent_goal_id,
                "active_only": active_only,
            },
        }

    async def operator_goal_view(
            self,
            *,
            status: str | None = None,
            parent_goal_id: str | None = None,
            active_only: bool = False,
    ) -> dict[str, Any]:
        goals = await self._filtered_goals(status=status, parent_goal_id=parent_goal_id, active_only=active_only)
        executions = await self.context.execution_store.list_executions()
        executions_by_id = {execution.id: execution for execution in executions}
        items = [
            await self._operator_goal_card(goal, executions_by_id=executions_by_id)
            for goal in goals
        ]
        return {
            "items": items,
            "count": len(items),
            "filters": {
                "status": status,
                "parent_goal_id": parent_goal_id,
                "active_only": active_only,
            },
            "summary": {
                "blocked_count": sum(1 for item in items if item["blocked"]),
                "stale_count": sum(1 for item in items if item["flags"]["stale"]),
                "failing_count": sum(1 for item in items if item["flags"]["failing"]),
                "pending_approval_count": sum(item["pending_approval_count"] for item in items),
                "automatic_action_count": sum(item["automatic_action_count"] for item in items),
            },
        }

    async def operator_goal_detail(self, goal_id: str) -> dict[str, Any]:
        goal = await self.get_goal(goal_id)
        executions = await self.context.execution_store.list_executions()
        executions_by_id = {execution.id: execution for execution in executions}
        card = await self._operator_goal_card(goal, executions_by_id=executions_by_id)
        linked_executions = self._goal_linked_executions(goal, executions_by_id)
        events_by_execution: dict[str, list[ExecutionEvent]] = {}
        artifacts_by_execution: dict[str, list[dict[str, Any]]] = {}
        timeline: list[dict[str, Any]] = [
            {
                "type": "goal",
                "event": "goal.created",
                "goal_id": goal.id,
                "timestamp": goal.created_at.isoformat(),
                "summary": "Goal created",
            }
        ]
        if goal.completed_at is not None:
            timeline.append(
                {
                    "type": "goal",
                    "event": f"goal.{goal.status.value}",
                    "goal_id": goal.id,
                    "timestamp": goal.completed_at.isoformat(),
                    "summary": f"Goal {goal.status.value}",
                }
            )
        for execution in linked_executions:
            events = await self.context.execution_store.list_events(execution.id)
            events_by_execution[execution.id] = events
            artifacts = await self.context.execution_store.list_artifacts(execution.id)
            artifacts_by_execution[execution.id] = [artifact.model_dump(mode="json") for artifact in artifacts]
            timeline.append(
                {
                    "type": "execution",
                    "event": "execution.status",
                    "goal_id": goal.id,
                    "execution_id": execution.id,
                    "workflow_id": execution.workflow_id,
                    "status": execution.status.value,
                    "timestamp": execution.updated_at.isoformat(),
                    "summary": f"Execution {execution.id} is {execution.status.value}",
                }
            )
            for event in events[-25:]:
                timeline.append(
                    {
                        "type": "execution_event",
                        "event": event.event_type.value,
                        "goal_id": goal.id,
                        "execution_id": execution.id,
                        "workflow_id": event.workflow_id or execution.workflow_id,
                        "timestamp": event.timestamp.isoformat(),
                        "summary": event.payload.get("summary")
                                   or event.payload.get("reason")
                                   or event.payload.get("category")
                                   or event.event_type.value,
                    }
                )
        monitoring = self._goal_monitoring(goal)
        for record in monitoring["findings"][-25:]:
            finding = record.get("finding") if isinstance(record.get("finding"), dict) else record
            timeline.append(
                {
                    "type": "supervisor_finding",
                    "event": "goal.supervisor_finding",
                    "goal_id": goal.id,
                    "timestamp": record.get("recorded_at") or record.get("created_at") or goal.updated_at.isoformat(),
                    "summary": finding.get("reason") or finding.get("category") or "Supervisor finding",
                    "category": finding.get("category"),
                    "finding_id": record.get("execution_event_id") or record.get("id") or record.get("dedupe_key"),
                }
            )
        for action in monitoring["supervisor_actions"][-25:]:
            timeline.append(
                {
                    "type": "supervisor_action",
                    "event": "goal.supervisor_action",
                    "goal_id": goal.id,
                    "timestamp": action.get("recorded_at") or action.get("created_at") or goal.updated_at.isoformat(),
                    "summary": action.get("action") or "Supervisor action",
                    "status": action.get("status"),
                    "approval_request_id": action.get("approval_request_id"),
                }
            )
        for action in monitoring["operator_actions"][-25:]:
            timeline.append(
                {
                    "type": "operator_action",
                    "event": "goal.operator_action",
                    "goal_id": goal.id,
                    "timestamp": action.get("recorded_at") or goal.updated_at.isoformat(),
                    "summary": action.get("action") or "Operator action",
                    "status": action.get("status"),
                    "actor": action.get("actor"),
                }
            )
        timeline = sorted(timeline, key=lambda item: str(item.get("timestamp") or ""), reverse=True)[:100]
        return {
            **card,
            "goal": goal.model_dump(mode="json"),
            "timeline": timeline,
            "evidence": list(goal.evidence),
            "artifacts": artifacts_by_execution,
            "approvals": monitoring["approval_requests"],
            "memory": {
                "memory_ids": self._memory_ids_for_goal(goal, goal.evidence),
                "goal_memory": goal.metadata.get("goal_memory") if isinstance(goal.metadata, dict) else {},
            },
            "evaluation": goal.evaluation,
            "supervisor": monitoring,
            "executions": {
                execution.id: execution.model_dump(mode="json")
                for execution in linked_executions
            },
            "events": {
                execution_id: [event.model_dump(mode="json") for event in events]
                for execution_id, events in events_by_execution.items()
            },
            "operator_actions": self._operator_goal_actions(goal),
        }

    async def apply_operator_action(
            self,
            goal_id: str,
            payload: dict[str, Any],
            *,
            actor: str | None = None,
    ) -> GoalDefinition:
        action = str(payload.get("action") or "").strip().lower()
        if not action:
            raise ValueError("Operator action is required")
        if action == "pause":
            updated = await self.pause_goal(goal_id)
        elif action == "resume":
            updated = await self.resume_goal(goal_id)
        elif action == "cancel":
            updated = await self.cancel_goal(goal_id, reason=payload.get("reason"))
        elif action == "adjust_autonomy":
            updated = await self._adjust_goal_autonomy(goal_id, payload)
        elif action == "update_success_criteria":
            updated = await self._update_goal_success_criteria(goal_id, payload)
        elif action == "reassign":
            updated = await self._reassign_goal(goal_id, payload)
        else:
            raise ValueError(
                "Unsupported operator action. Expected pause, resume, cancel, adjust_autonomy, "
                "update_success_criteria, or reassign."
            )
        if updated.status in TERMINAL_GOAL_STATUSES and action in {"cancel"}:
            current = updated
        else:
            current = await self.get_goal(goal_id)
        return await self._record_operator_action(current, action=action, payload=payload, actor=actor)

    async def _adjust_goal_autonomy(self, goal_id: str, payload: dict[str, Any]) -> GoalDefinition:
        autonomy = str(payload.get("autonomy") or "").strip().lower()
        if autonomy not in {"off", "advisory", "guarded", "high_autonomy"}:
            raise ValueError("autonomy must be one of off, advisory, guarded, or high_autonomy")
        goal = await self.get_goal(goal_id)
        self._ensure_not_terminal(goal)
        constraints = dict(goal.constraints)
        constraints["autonomy"] = autonomy
        return await self.update_goal(goal_id, {"constraints": constraints})

    async def _update_goal_success_criteria(self, goal_id: str, payload: dict[str, Any]) -> GoalDefinition:
        criteria = payload.get("success_criteria")
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("success_criteria must contain at least one criterion")
        if not all(isinstance(item, dict) for item in criteria):
            raise ValueError("success_criteria entries must be objects")
        goal = await self.get_goal(goal_id)
        self._ensure_not_terminal(goal)
        return await self.update_goal(goal_id, {"success_criteria": criteria})

    async def _reassign_goal(self, goal_id: str, payload: dict[str, Any]) -> GoalDefinition:
        owner_actor = str(payload.get("owner_actor") or "").strip()
        if not owner_actor:
            raise ValueError("owner_actor is required when reassigning a goal")
        goal = await self.get_goal(goal_id)
        self._ensure_not_terminal(goal)
        return await self.update_goal(goal_id, {"owner_actor": owner_actor})

    async def _record_operator_action(
            self,
            goal: GoalDefinition,
            *,
            action: str,
            payload: dict[str, Any],
            actor: str | None,
    ) -> GoalDefinition:
        metadata = dict(goal.metadata)
        monitoring = dict(metadata.get("main_agent_monitoring") or {})
        operator_actions = [item for item in monitoring.get("operator_actions", []) if isinstance(item, dict)]
        record = {
            "id": f"operator-action-{len(operator_actions) + 1}",
            "goal_id": goal.id,
            "action": action,
            "actor": actor,
            "reason": payload.get("reason"),
            "recorded_at": utc_now().isoformat(),
            "status": "completed",
            "fields": sorted(key for key in payload if key != "action"),
        }
        operator_actions.append(record)
        monitoring["operator_actions"] = operator_actions[-50:]
        monitoring["last_operator_action"] = record
        metadata["main_agent_monitoring"] = monitoring
        updated = await self.update_goal(goal.id, {"metadata": metadata})
        await self._append_goal_projection_event(
            "goal.operator_action.recorded",
            updated,
            extra={"operator_action": record},
        )
        return updated

    async def _filtered_goals(
            self,
            *,
            status: str | None = None,
            parent_goal_id: str | None = None,
            active_only: bool = False,
    ) -> list[GoalDefinition]:
        goals = await self.context.goal_repo.list()
        if status:
            normalized = GoalStatus(status)
            goals = [goal for goal in goals if goal.status == normalized]
        if parent_goal_id:
            goals = [goal for goal in goals if goal.parent_goal_id == parent_goal_id]
        if active_only:
            goals = [goal for goal in goals if goal.status in ACTIVE_GOAL_STATUSES]
        return sorted(goals, key=lambda goal: goal.created_at, reverse=True)

    async def _operator_goal_card(
            self,
            goal: GoalDefinition,
            *,
            executions_by_id: dict[str, Execution],
    ) -> dict[str, Any]:
        linked_executions = self._goal_linked_executions(goal, executions_by_id)
        active_executions = [
            execution for execution in linked_executions if execution.status in ACTIVE_EXECUTION_STATUSES
        ]
        monitoring = self._goal_monitoring(goal)
        active_plan = self._goal_active_plan(goal)
        findings = monitoring["findings"]
        approvals = monitoring["approval_requests"]
        actions = monitoring["supervisor_actions"]
        blockers = self._goal_summary_blockers(goal, findings=findings, approvals=approvals)
        pending_approvals = [
            approval for approval in approvals
            if str(approval.get("status") or "").lower() in {"pending", "waiting", "pending_approval"}
        ]
        automatic_actions = [
            action for action in actions
            if action.get("allowed_by_policy") is True and action.get("requires_approval") is not True
        ]
        flags = self._operator_goal_flags(
            goal,
            linked_executions=linked_executions,
            findings=findings,
            blockers=blockers,
        )
        return {
            "goal_id": goal.id,
            "objective": goal.objective,
            "status": goal.status.value,
            "priority": goal.priority,
            "deadline_at": goal.deadline_at.isoformat() if goal.deadline_at else None,
            "owner_actor": goal.owner_actor,
            "parent_goal_id": goal.parent_goal_id,
            "current_plan": active_plan,
            "active_plan_version": active_plan.get("version") if isinstance(active_plan, dict) else None,
            "active_executions": [execution.model_dump(mode="json") for execution in active_executions],
            "active_execution_count": len(active_executions),
            "linked_execution_count": len(linked_executions),
            "next_supervisor_action": self._next_operator_supervisor_action(
                active_plan=active_plan,
                findings=findings,
                actions=actions,
                pending_approvals=pending_approvals,
            ),
            "blocked": bool(blockers),
            "blocked_reason": blockers[0] if blockers else None,
            "blockers": blockers,
            "pending_approvals": pending_approvals,
            "pending_approval_count": len(pending_approvals),
            "automatic_actions": automatic_actions,
            "automatic_action_count": len(automatic_actions),
            "flags": flags,
            "success_criteria_count": len(goal.success_criteria),
            "evidence_count": len(goal.evidence),
            "evaluation_status": goal.evaluation.get("status") if isinstance(goal.evaluation, dict) else None,
            "updated_at": goal.updated_at.isoformat(),
            "created_at": goal.created_at.isoformat(),
        }

    @staticmethod
    def _goal_linked_executions(
            goal: GoalDefinition,
            executions_by_id: dict[str, Execution],
    ) -> list[Execution]:
        return [
            executions_by_id[execution_id]
            for execution_id in goal.execution_ids
            if execution_id in executions_by_id
        ]

    @staticmethod
    def _goal_monitoring(goal: GoalDefinition) -> dict[str, list[dict[str, Any]]]:
        monitoring = goal.metadata.get("main_agent_monitoring") if isinstance(goal.metadata, dict) else {}
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        return {
            "findings": [item for item in monitoring.get("findings", []) if isinstance(item, dict)],
            "decisions": [item for item in monitoring.get("supervisor_decisions", []) if isinstance(item, dict)],
            "supervisor_actions": [
                item for item in monitoring.get("supervisor_actions", []) if isinstance(item, dict)
            ],
            "operator_actions": [
                item for item in monitoring.get("operator_actions", []) if isinstance(item, dict)
            ],
            "approval_requests": [
                item for item in monitoring.get("approval_requests", []) if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _goal_active_plan(goal: GoalDefinition) -> dict[str, Any]:
        planning = goal.metadata.get("goal_planning") if isinstance(goal.metadata, dict) else {}
        planning = planning if isinstance(planning, dict) else {}
        active_plan = planning.get("active_plan")
        return active_plan if isinstance(active_plan, dict) else {}

    @staticmethod
    def _operator_goal_flags(
            goal: GoalDefinition,
            *,
            linked_executions: list[Execution],
            findings: list[dict[str, Any]],
            blockers: list[dict[str, Any]],
    ) -> dict[str, bool]:
        finding_categories: set[str] = set()
        signal_categories: set[str] = set()
        for record in findings:
            finding = record.get("finding") if isinstance(record.get("finding"), dict) else record
            category = finding.get("category")
            if isinstance(category, str):
                finding_categories.add(category)
            evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
            signal_category = evidence.get("signal_category")
            if isinstance(signal_category, str):
                signal_categories.add(signal_category)
        return {
            "blocked": bool(blockers),
            "stale": "stale_execution" in signal_categories or "stale_execution" in finding_categories,
            "failing": (
                    "goal_repeated_failure" in finding_categories
                    or any(execution.status == ExecutionStatus.FAILED for execution in linked_executions)
            ),
            "missing_evidence": (
                    "goal_missing_evidence" in finding_categories
                    or (isinstance(goal.evaluation, dict) and goal.evaluation.get("sufficient") is False)
            ),
            "waiting_for_approval": goal.status == GoalStatus.WAITING_FOR_APPROVAL,
            "waiting_for_input": goal.status == GoalStatus.WAITING_FOR_INPUT,
        }

    @staticmethod
    def _next_operator_supervisor_action(
            *,
            active_plan: dict[str, Any],
            findings: list[dict[str, Any]],
            actions: list[dict[str, Any]],
            pending_approvals: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if pending_approvals:
            approval = pending_approvals[-1]
            return {
                "type": "approval",
                "action": approval.get("recommended_action") or approval.get("action"),
                "status": approval.get("status") or "pending",
                "approval_request_id": approval.get("approval_request_id") or approval.get("id"),
            }
        for action in reversed(actions):
            if action.get("status") in {"pending", "pending_approval", "blocked"}:
                return {
                    "type": "supervisor_action",
                    "action": action.get("action"),
                    "status": action.get("status"),
                    "approval_request_id": action.get("approval_request_id"),
                }
        if isinstance(active_plan, dict):
            next_action = active_plan.get("next_action")
            if isinstance(next_action, dict) and next_action:
                return {"type": "plan_next_action", **next_action}
            if isinstance(next_action, str) and next_action.strip():
                return {"type": "plan_next_action", "action": next_action}
            for step in active_plan.get("steps", []):
                if not isinstance(step, dict):
                    continue
                status = str(step.get("status") or "").lower()
                if status in {"pending", "ready", "active", "in_progress"}:
                    return {"type": "plan_step", **step}
        for record in reversed(findings):
            finding = record.get("finding") if isinstance(record.get("finding"), dict) else record
            evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
            recommended = evidence.get("recommended_action")
            if recommended:
                return {
                    "type": "supervisor_recommendation",
                    "action": recommended,
                    "finding_category": finding.get("category"),
                }
        return None

    @staticmethod
    def _operator_goal_actions(goal: GoalDefinition) -> dict[str, bool]:
        terminal = goal.status in TERMINAL_GOAL_STATUSES
        return {
            "pause": not terminal and goal.status != GoalStatus.PAUSED,
            "resume": goal.status == GoalStatus.PAUSED,
            "cancel": not terminal,
            "update_success_criteria": not terminal,
            "adjust_autonomy": not terminal,
            "attach_evidence": not terminal,
            "complete": not terminal,
        }

    async def get_goal(self, goal_id: str) -> GoalDefinition:
        goal = await self.context.goal_repo.get(goal_id)
        if goal is None:
            raise GoalNotFoundError(f"Goal '{goal_id}' was not found")
        return goal

    async def update_goal(self, goal_id: str, patch: dict[str, Any]) -> GoalDefinition:
        current = await self.get_goal(goal_id)
        merged = current.model_dump(mode="json")
        merged.update(patch)
        merged["id"] = goal_id
        merged["updated_at"] = utc_now()
        updated = await self.context.goal_repo.save(GoalDefinition.model_validate(merged))
        await self._append_goal_projection_event("goal.updated", updated, previous_goal=current)
        return updated

    async def attach_evidence(self, goal_id: str, evidence: list[dict[str, Any]]) -> GoalDefinition:
        if not evidence:
            raise ValueError("At least one evidence record is required")
        goal = await self.get_goal(goal_id)
        merged_evidence = [*goal.evidence, *evidence]
        updated = await self.update_goal(goal_id, {"evidence": merged_evidence})
        await self._append_goal_projection_event(
            "goal.evidence_attached",
            updated,
            extra={"evidence": evidence},
        )
        return updated

    async def evaluate_goal(
            self,
            goal_id: str,
            *,
            evidence: list[dict[str, Any]] | None = None,
            persist: bool = True,
    ) -> dict[str, Any]:
        goal = await self.get_goal(goal_id)
        evidence_records = [*goal.evidence, *(evidence or [])]
        evaluation = GoalEvaluator.evaluate(goal, evidence_records)
        if persist:
            updated = await self.update_goal(goal_id, {"evaluation": evaluation})
            await self._append_goal_projection_event(
                "goal.evaluation_recorded",
                updated,
                extra={"evaluation": evaluation},
            )
        return evaluation

    async def plan_goal(
            self,
            goal_id: str,
            *,
            plan: dict[str, Any] | None = None,
            reason: str = "initial_plan",
            actor: str | None = None,
    ) -> GoalDefinition:
        goal = await self.get_goal(goal_id)
        self._ensure_not_terminal(goal)
        if not goal.success_criteria and not self._explicit_completion_condition(goal):
            raise ValueError("Active goals require at least one success criterion or explicit completion condition")
        normalized_plan = self._normalize_plan(goal, plan, reason=reason, actor=actor, previous_version=0)
        metadata = self._metadata_with_plan(goal.metadata, normalized_plan, reason=reason, actor=actor)
        status = GoalStatus.PLANNING.value if goal.status == GoalStatus.CREATED else goal.status.value
        updated = await self.update_goal(goal_id, {"status": status, "metadata": metadata})
        await self._append_goal_projection_event(
            "goal.plan_versioned",
            updated,
            extra={"plan": normalized_plan, "reason": reason, "actor": actor},
        )
        return updated

    async def replan_goal(
            self,
            goal_id: str,
            *,
            plan: dict[str, Any] | None = None,
            reason: str,
            actor: str | None = None,
    ) -> GoalDefinition:
        if not reason.strip():
            raise ValueError("Replan reason is required")
        goal = await self.get_goal(goal_id)
        self._ensure_not_terminal(goal)
        planning = goal.metadata.get("goal_planning") if isinstance(goal.metadata, dict) else {}
        active_plan = planning.get("active_plan") if isinstance(planning, dict) else None
        previous_version = int(active_plan.get("version") or 0) if isinstance(active_plan, dict) else 0
        normalized_plan = self._normalize_plan(goal, plan, reason=reason, actor=actor,
                                               previous_version=previous_version)
        metadata = self._metadata_with_plan(goal.metadata, normalized_plan, reason=reason, actor=actor)
        updated = await self.update_goal(goal_id, {"status": GoalStatus.ACTIVE.value, "metadata": metadata})
        await self._append_goal_projection_event(
            "goal.plan_versioned",
            updated,
            extra={"plan": normalized_plan, "reason": reason, "actor": actor},
        )
        return updated

    async def list_supervisor_findings(self, goal_id: str) -> dict[str, Any]:
        goal = await self.get_goal(goal_id)
        monitoring = goal.metadata.get("main_agent_monitoring") if isinstance(goal.metadata, dict) else {}
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        findings = [item for item in monitoring.get("findings", []) if isinstance(item, dict)]
        decisions = [item for item in monitoring.get("supervisor_decisions", []) if isinstance(item, dict)]
        actions = [item for item in monitoring.get("supervisor_actions", []) if isinstance(item, dict)]
        approval_requests = [item for item in monitoring.get("approval_requests", []) if isinstance(item, dict)]
        return {
            "goal_id": goal.id,
            "findings": findings,
            "finding_count": len(findings),
            "decisions": decisions,
            "decision_count": len(decisions),
            "actions": actions,
            "action_count": len(actions),
            "approval_requests": approval_requests,
            "approval_request_count": len(approval_requests),
        }

    async def record_supervisor_decision(
            self,
            goal_id: str,
            decision: dict[str, Any],
            *,
            actor: str | None = None,
    ) -> GoalDefinition:
        if not isinstance(decision, dict) or not decision:
            raise ValueError("Supervisor decision payload is required")
        goal = await self.get_goal(goal_id)
        monitoring = dict(goal.metadata.get("main_agent_monitoring") or {})
        decisions = [item for item in monitoring.get("supervisor_decisions", []) if isinstance(item, dict)]
        record = {
            "id": str(decision.get("id") or f"decision-{len(decisions) + 1}"),
            "goal_id": goal.id,
            "source": "main_agent_supervisor",
            "recorded_at": utc_now().isoformat(),
            "actor": actor,
            "decision": decision,
            "risk": decision.get("risk") or "low",
            "policy": decision.get("policy") if isinstance(decision.get("policy"), dict) else {},
            "requires_approval": decision.get("requires_approval") is True,
            "approval_request_id": decision.get("approval_request_id"),
            "finding_id": decision.get("finding_id") or decision.get("finding_event_id"),
            "action": decision.get("action") or decision.get("recommended_action"),
            "rationale": decision.get("rationale") or decision.get("reason"),
        }
        decisions.append(record)
        monitoring["supervisor_decisions"] = decisions[-50:]
        monitoring["last_supervisor_decision"] = record
        metadata = dict(goal.metadata)
        metadata["main_agent_monitoring"] = monitoring
        updated = await self.update_goal(goal_id, {"metadata": metadata})
        await self._append_goal_projection_event(
            "goal.supervisor_decision.recorded",
            updated,
            extra={"supervisor_decision": record},
        )
        return updated

    async def store_goal_summary_memory(
            self,
            goal_id: str,
            *,
            actor: str | None = None,
            reason: str = "goal_summary",
    ) -> GoalDefinition:
        from app.services.memory import MemoryService

        goal = await self.get_goal(goal_id)
        metadata = dict(goal.metadata)
        goal_memory = dict(metadata.get("goal_memory") or {})
        previous_memory_id = goal_memory.get("latest_summary_memory_id")
        summary_payload = self._goal_summary_memory_payload(goal, actor=actor, reason=reason)
        if previous_memory_id:
            summary_payload["supersedes_memory_id"] = previous_memory_id
        memory = await MemoryService(self.context).create_memory(
            summary_payload,
            confirmed=True,
            trusted_actor=True,
        )
        memory_ids = self._memory_ids_for_goal(goal, goal.evidence)
        if previous_memory_id in memory_ids:
            memory_ids.remove(previous_memory_id)
        if memory.id not in memory_ids:
            memory_ids.append(memory.id)
        goal_memory.update(
            {
                "latest_summary_memory_id": memory.id,
                "previous_summary_memory_id": previous_memory_id,
                "updated_at": utc_now().isoformat(),
                "reason": reason,
            }
        )
        metadata["memory_ids"] = sorted(set(memory_ids))
        metadata["goal_memory"] = goal_memory
        updated = await self.update_goal(goal_id, {"metadata": metadata})
        await self._append_goal_projection_event(
            "goal.memory_summary.stored",
            updated,
            extra={"memory_id": memory.id, "supersedes_memory_id": previous_memory_id},
        )
        return updated

    async def pause_goal(self, goal_id: str) -> GoalDefinition:
        goal = await self.get_goal(goal_id)
        self._ensure_not_terminal(goal)
        return await self.update_goal(goal_id, {"status": GoalStatus.PAUSED.value})

    async def resume_goal(self, goal_id: str) -> GoalDefinition:
        goal = await self.get_goal(goal_id)
        if goal.status != GoalStatus.PAUSED:
            raise GoalTransitionError("Only paused goals can be resumed")
        return await self.update_goal(goal_id, {"status": GoalStatus.ACTIVE.value})

    async def cancel_goal(self, goal_id: str, *, reason: str | None = None) -> GoalDefinition:
        goal = await self.get_goal(goal_id)
        self._ensure_not_terminal(goal)
        metadata = dict(goal.metadata)
        if reason:
            metadata["cancellation_reason"] = reason
        return await self.update_goal(
            goal_id,
            {
                "status": GoalStatus.CANCELLED.value,
                "completed_at": utc_now(),
                "metadata": metadata,
            },
        )

    async def complete_goal(
            self,
            goal_id: str,
            *,
            evidence: list[dict[str, Any]] | None = None,
            evaluation: dict[str, Any] | None = None,
    ) -> GoalDefinition:
        goal = await self.get_goal(goal_id)
        self._ensure_not_terminal(goal)
        merged_evidence = list(goal.evidence)
        merged_evidence.extend(evidence or [])
        evaluator_result = GoalEvaluator.evaluate(goal, merged_evidence)
        if evaluation is not None:
            evaluator_result["reviewer_evaluation"] = evaluation
        review_requirement = self._completion_review_requirement(goal)
        if review_requirement["required"] and not self._has_independent_completion_authority(
                goal,
                merged_evidence,
                evaluation=evaluation,
        ):
            missing_review = {
                "criterion_id": "independent_completion_review",
                "criterion": {
                    "kind": "independent_review",
                    "description": review_requirement["description"],
                },
                "reason": review_requirement["reason"],
            }
            evaluator_result["status"] = "completion_review_required"
            evaluator_result["sufficient"] = False
            evaluator_result["confidence"] = "low"
            evaluator_result["completion_review"] = {
                **review_requirement,
                "satisfied": False,
            }
            evaluator_result["missing_evidence"] = [
                *evaluator_result.get("missing_evidence", []),
                missing_review,
            ]
        elif review_requirement["required"]:
            evaluator_result["completion_review"] = {
                **review_requirement,
                "satisfied": True,
            }
        if not evaluator_result["sufficient"]:
            updated = await self.update_goal(
                goal_id,
                {
                    "evidence": merged_evidence,
                    "evaluation": evaluator_result,
                },
            )
            await self._append_goal_projection_event(
                "goal.evaluation_recorded",
                updated,
                extra={"evaluation": evaluator_result},
            )
            if evaluator_result["status"] == "completion_review_required":
                raise GoalTransitionError("High-risk goal completion requires independent review or final approval")
            raise GoalTransitionError("Goal completion requires sufficient evidence")
        updated = await self.update_goal(
            goal_id,
            {
                "status": GoalStatus.COMPLETED.value,
                "completed_at": utc_now(),
                "evidence": merged_evidence,
                "evaluation": evaluator_result,
            },
        )
        await self._append_goal_projection_event(
            "goal.evaluation_recorded",
            updated,
            extra={"evaluation": evaluator_result},
        )
        return updated

    async def link_execution(self, goal_id: str, execution_id: str) -> GoalDefinition:
        goal = await self.get_goal(goal_id)
        execution_ids = list(goal.execution_ids)
        if execution_id not in execution_ids:
            execution_ids.append(execution_id)
        updated = await self.update_goal(goal_id, {"execution_ids": execution_ids})
        await self._append_goal_projection_event(
            "goal.execution_linked",
            updated,
            extra={"execution_id": execution_id},
        )
        return updated

    def _completion_review_requirement(self, goal: GoalDefinition) -> dict[str, Any]:
        constraints = goal.constraints if isinstance(goal.constraints, dict) else {}
        approval_policy = constraints.get("approval_policy") if isinstance(constraints.get("approval_policy"),
                                                                           dict) else {}
        approval_required_actions = {
            str(item)
            for source in (
                constraints.get("approval_required_actions"),
                approval_policy.get("approval_required_actions"),
            )
            if isinstance(source, list)
            for item in source
            if item
        }
        risk_values = {
            str(value).strip().lower()
            for value in (
                constraints.get("risk"),
                constraints.get("risk_level"),
                constraints.get("riskLevel"),
                goal.priority,
            )
            if value
        }
        risky_criteria = [
            criterion
            for criterion in goal.success_criteria
            if isinstance(criterion, dict)
               and (
                       str(criterion.get("risk") or criterion.get("risk_level") or "").strip().lower()
                       in HIGH_RISK_GOAL_LEVELS
                       or criterion.get("high_risk") is True
               )
        ]
        final_approval_required = any(
            source is True
            for source in (
                constraints.get("final_approval_required"),
                constraints.get("human_final_approval_required"),
                constraints.get("require_human_final_approval"),
                approval_policy.get("final_approval_required"),
                approval_policy.get("human_final_approval_required"),
                approval_policy.get("require_human_final_approval"),
            )
        )
        high_risk = (
                bool(risk_values.intersection(HIGH_RISK_GOAL_LEVELS))
                or bool(approval_required_actions.intersection(HIGH_RISK_APPROVAL_ACTIONS))
                or bool(risky_criteria)
                or final_approval_required
        )
        return {
            "required": high_risk,
            "final_approval_required": final_approval_required,
            "risk_values": sorted(risk_values),
            "approval_required_actions": sorted(approval_required_actions),
            "high_risk_success_criteria_ids": [
                str(item.get("id") or item.get("criterion_id") or item.get("kind") or "criterion")
                for item in risky_criteria
            ],
            "description": (
                "High-risk goals require independent completion review or explicit final approval before completion."
            ),
            "reason": (
                "Completion evidence is sufficient, but the goal is high risk and does not include independent "
                "review or final approval evidence."
            ),
        }

    def _has_independent_completion_authority(
            self,
            goal: GoalDefinition,
            evidence: list[dict[str, Any]],
            *,
            evaluation: dict[str, Any] | None,
    ) -> bool:
        candidates = [item for item in evidence if isinstance(item, dict)]
        if isinstance(evaluation, dict):
            candidates.append(evaluation)
        goal_evaluation = goal.evaluation if isinstance(goal.evaluation, dict) else {}
        if goal_evaluation:
            candidates.append(goal_evaluation)

        worker_authorities = self._worker_completion_authorities(evidence)
        for item in candidates:
            if self._is_final_approval_record(item):
                approver = self._completion_authority_actor(item)
                if not approver or approver not in worker_authorities:
                    return True
            reviewer_role = str(
                item.get("reviewer_role")
                or item.get("evaluator_role")
                or item.get("authority_role")
                or item.get("approved_by_role")
                or ""
            ).strip().lower()
            if reviewer_role in INDEPENDENT_REVIEWER_ROLES:
                reviewer_actor = self._completion_authority_actor(item)
                if not reviewer_actor or reviewer_actor not in worker_authorities:
                    return True
            if item.get("independent_review") is True or item.get("independent_completion_review") is True:
                reviewer_actor = self._completion_authority_actor(item)
                if not reviewer_actor or reviewer_actor not in worker_authorities:
                    return True
        return False

    @staticmethod
    def _worker_completion_authorities(evidence: list[dict[str, Any]]) -> set[str]:
        authorities: set[str] = set()
        for item in evidence:
            if not isinstance(item, dict):
                continue
            for key in ("agent_id", "worker_agent_id", "produced_by", "created_by", "declared_by", "source_agent_id"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    authorities.add(value.strip())
        return authorities

    @staticmethod
    def _is_final_approval_record(item: dict[str, Any]) -> bool:
        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        status = str(item.get("status") or item.get("approval_status") or "").strip().lower()
        return (
                kind in FINAL_APPROVAL_EVIDENCE_KINDS
                and status not in {"", "rejected", "denied", "cancelled", "pending"}
        ) or item.get("final_approval") is True or item.get("human_final_approval") is True

    @staticmethod
    def _completion_authority_actor(item: dict[str, Any]) -> str:
        return str(
            item.get("reviewer_actor")
            or item.get("evaluator_actor")
            or item.get("approved_by")
            or item.get("reviewed_by")
            or item.get("actor")
            or ""
        ).strip()

    async def _append_goal_projection_event(
            self,
            event_type: str,
            goal: GoalDefinition,
            *,
            previous_goal: GoalDefinition | None = None,
            extra: dict[str, Any] | None = None,
    ) -> None:
        if not get_settings().graph_projection_enabled:
            return
        repo = getattr(self.context, "graph_projection_event_repo", None)
        if repo is None:
            return
        try:
            await repo.append(
                GraphProjectionEvent(
                    event_type=event_type,
                    aggregate_type="goal",
                    aggregate_id=goal.id,
                    user_id=goal.owner_actor,
                    payload=self._goal_projection_payload(goal, previous_goal=previous_goal, extra=extra),
                    source="goal_service",
                )
            )
        except Exception:
            logger.exception("Failed to append goal graph projection event")

    def _goal_projection_payload(
            self,
            goal: GoalDefinition,
            *,
            previous_goal: GoalDefinition | None = None,
            extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        planning = goal.metadata.get("goal_planning") if isinstance(goal.metadata, dict) else {}
        active_plan = planning.get("active_plan") if isinstance(planning, dict) else None
        monitoring = goal.metadata.get("main_agent_monitoring") if isinstance(goal.metadata, dict) else {}
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        approval_requests = [item for item in monitoring.get("approval_requests", []) if isinstance(item, dict)]
        decisions = [item for item in monitoring.get("supervisor_decisions", []) if isinstance(item, dict)]
        findings = [item for item in monitoring.get("findings", []) if isinstance(item, dict)]
        evidence = list(goal.evidence)
        previous_execution_ids = set(previous_goal.execution_ids if previous_goal is not None else [])
        linked_execution_ids = [
            execution_id for execution_id in goal.execution_ids if execution_id not in previous_execution_ids
        ]
        payload = {
            "goal": {
                "id": goal.id,
                "objective": goal.objective,
                "status": goal.status.value,
                "priority": goal.priority,
                "owner_actor": goal.owner_actor,
                "parent_goal_id": goal.parent_goal_id,
                "created_at": goal.created_at.isoformat(),
                "updated_at": goal.updated_at.isoformat(),
                "completed_at": goal.completed_at.isoformat() if goal.completed_at else None,
                "deadline_at": goal.deadline_at.isoformat() if goal.deadline_at else None,
            },
            "success_criteria": goal.success_criteria,
            "constraints": goal.constraints,
            "relationships": {
                "parent_goal_id": goal.parent_goal_id,
                "execution_ids": list(goal.execution_ids),
                "linked_execution_ids": linked_execution_ids,
                "artifact_ids": self._artifact_ids_from_evidence(evidence),
                "memory_ids": self._memory_ids_for_goal(goal, evidence),
                "approval_request_ids": [
                    str(item.get("approval_request_id"))
                    for item in [*approval_requests, *decisions]
                    if item.get("approval_request_id")
                ],
                "evaluation_ids": self._evaluation_ids_for_goal(goal),
                "supervisor_decision_ids": [str(item.get("id")) for item in decisions if item.get("id")],
                "supervisor_finding_ids": [
                    str(item.get("execution_event_id") or item.get("dedupe_key"))
                    for item in findings
                    if item.get("execution_event_id") or item.get("dedupe_key")
                ],
            },
            "active_plan": active_plan,
            "evaluation": goal.evaluation,
        }
        if extra:
            payload.update(extra)
        return payload

    @staticmethod
    def _artifact_ids_from_evidence(evidence: list[dict[str, Any]]) -> list[str]:
        artifact_ids: list[str] = []
        for item in evidence:
            for key in ("artifact_id", "artifactId"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    artifact_ids.append(value)
            if str(item.get("type") or item.get("kind") or "") == "artifact":
                value = item.get("id")
                if isinstance(value, str) and value:
                    artifact_ids.append(value)
        return sorted(set(artifact_ids))

    @staticmethod
    def _evaluation_ids_for_goal(goal: GoalDefinition) -> list[str]:
        evaluation = goal.evaluation if isinstance(goal.evaluation, dict) else {}
        values = []
        for key in ("id", "evaluation_id", "evaluationId"):
            value = evaluation.get(key)
            if isinstance(value, str) and value:
                values.append(value)
        return sorted(set(values))

    @staticmethod
    def _memory_ids_for_goal(goal: GoalDefinition, evidence: list[dict[str, Any]]) -> list[str]:
        memory_ids: list[str] = []
        metadata = goal.metadata if isinstance(goal.metadata, dict) else {}
        for source in (
                metadata.get("memory_ids"),
                metadata.get("attached_memory_ids"),
                metadata.get("context_memory_ids"),
        ):
            if isinstance(source, list):
                memory_ids.extend(str(item) for item in source if item)
        for item in evidence:
            for key in ("memory_id", "memoryId"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    memory_ids.append(value)
            if str(item.get("type") or item.get("kind") or "") == "memory":
                value = item.get("id")
                if isinstance(value, str) and value:
                    memory_ids.append(value)
        return sorted(set(memory_ids))

    def _goal_summary_memory_payload(
            self,
            goal: GoalDefinition,
            *,
            actor: str | None,
            reason: str,
    ) -> dict[str, Any]:
        metadata = goal.metadata if isinstance(goal.metadata, dict) else {}
        planning = metadata.get("goal_planning") if isinstance(metadata.get("goal_planning"), dict) else {}
        active_plan = planning.get("active_plan") if isinstance(planning.get("active_plan"), dict) else None
        monitoring = metadata.get("main_agent_monitoring") if isinstance(metadata.get("main_agent_monitoring"),
                                                                         dict) else {}
        findings = [item for item in monitoring.get("findings", []) if isinstance(item, dict)][-10:]
        approvals = [item for item in monitoring.get("approval_requests", []) if isinstance(item, dict)][-10:]
        decisions = [item for item in monitoring.get("supervisor_decisions", []) if isinstance(item, dict)][-10:]
        actions = [item for item in monitoring.get("supervisor_actions", []) if isinstance(item, dict)][-10:]
        blockers = self._goal_summary_blockers(goal, findings=findings, approvals=approvals)
        next_actions = self._goal_summary_next_actions(active_plan=active_plan, actions=actions)
        content = "\n".join(
            [
                f"Goal: {goal.objective}",
                f"Status: {goal.status.value}",
                f"Priority: {goal.priority}",
                f"Success criteria: {goal.success_criteria}",
                f"Constraints: {goal.constraints}",
                f"Active plan: {active_plan or {} }",
                f"Evidence count: {len(goal.evidence)}",
                f"Evaluation: {goal.evaluation or {} }",
                f"Pending approvals: {approvals}",
                f"Supervisor decisions: {decisions}",
                f"Unresolved blockers: {blockers}",
                f"Next actions: {next_actions}",
            ]
        )
        workflow_id = self._goal_summary_workflow_id(goal)
        payload: dict[str, Any] = {
            "scope": "workflow" if workflow_id else "global",
            "workflow_id": workflow_id,
            "content": content,
            "summary": f"{goal.status.value} goal summary: {goal.objective}",
            "tags": ["goal_summary", f"goal:{goal.id}", f"status:{goal.status.value}"],
            "source": "goal_summary",
            "memory_type": "archive",
            "importance": 85,
            "metadata": {
                "goal_id": goal.id,
                "goal_status": goal.status.value,
                "summary_reason": reason,
                "created_by": actor,
                "preserved_constraints": goal.constraints,
                "preserved_approvals": approvals,
                "preserved_blockers": blockers,
                "next_actions": next_actions,
                "active_plan_version": active_plan.get("version") if active_plan else None,
                "execution_ids": list(goal.execution_ids),
                "evidence_count": len(goal.evidence),
            },
        }
        if not workflow_id:
            payload.pop("workflow_id", None)
        return payload

    @staticmethod
    def _goal_summary_blockers(
            goal: GoalDefinition,
            *,
            findings: list[dict[str, Any]],
            approvals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if goal.status in {GoalStatus.WAITING_FOR_INPUT, GoalStatus.WAITING_FOR_APPROVAL, GoalStatus.PAUSED}:
            blockers.append({"type": "goal_status", "status": goal.status.value})
        for approval in approvals:
            if str(approval.get("status") or "").lower() in {"pending", "waiting", "pending_approval"}:
                blockers.append(
                    {
                        "type": "approval",
                        "approval_request_id": approval.get("approval_request_id"),
                        "recommended_action": approval.get("recommended_action"),
                    }
                )
        for record in findings:
            finding = record.get("finding") if isinstance(record.get("finding"), dict) else {}
            category = finding.get("category") or record.get("category")
            if category in {"stalled_goal", "goal_execution_signal", "goal_repeated_failure", "goal_missing_evidence"}:
                blockers.append(
                    {
                        "type": "finding",
                        "category": category,
                        "finding_id": record.get("execution_event_id") or record.get("dedupe_key"),
                    }
                )
        return blockers

    @staticmethod
    def _goal_summary_next_actions(
            *,
            active_plan: dict[str, Any] | None,
            actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        next_actions: list[dict[str, Any]] = []
        if isinstance(active_plan, dict):
            steps = [step for step in active_plan.get("steps", []) if isinstance(step, dict)]
            next_actions.extend(steps[:3])
        for action in actions:
            if action.get("status") in {"pending", "pending_approval", "blocked"}:
                next_actions.append(
                    {
                        "action": action.get("action"),
                        "status": action.get("status"),
                        "approval_request_id": action.get("approval_request_id"),
                    }
                )
        return next_actions[:10]

    @staticmethod
    def _goal_summary_workflow_id(goal: GoalDefinition) -> str | None:
        constraints = goal.constraints if isinstance(goal.constraints, dict) else {}
        workflow_id = constraints.get("workflow_id")
        if isinstance(workflow_id, str) and workflow_id:
            return workflow_id
        metadata = goal.metadata if isinstance(goal.metadata, dict) else {}
        workflow_id = metadata.get("workflow_id")
        return workflow_id if isinstance(workflow_id, str) and workflow_id else None

    def _normalize_plan(
            self,
            goal: GoalDefinition,
            plan: dict[str, Any] | None,
            *,
            reason: str,
            actor: str | None,
            previous_version: int,
    ) -> dict[str, Any]:
        source_plan = plan if isinstance(plan, dict) and plan else self._default_plan_for_goal(goal)
        steps = source_plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("Goal plan requires at least one step")
        normalized_steps = [self._normalize_plan_step(step, index) for index, step in enumerate(steps)]
        return {
            "version": previous_version + 1,
            "goal_id": goal.id,
            "objective": goal.objective,
            "status": "active",
            "summary": str(source_plan.get("summary") or f"Plan for goal: {goal.objective}"),
            "steps": normalized_steps,
            "created_at": utc_now().isoformat(),
            "created_by": actor,
            "reason": reason,
            "expected_evidence": source_plan.get("expected_evidence") if isinstance(
                source_plan.get("expected_evidence"), list) else goal.success_criteria,
        }

    def _default_plan_for_goal(self, goal: GoalDefinition) -> dict[str, Any]:
        workflow_id = goal.constraints.get("workflow_id") if isinstance(goal.constraints, dict) else None
        workflow_input = goal.constraints.get("workflow_input") if isinstance(goal.constraints, dict) else None
        steps: list[dict[str, Any]] = []
        if workflow_id:
            steps.append(
                {
                    "action": "start_workflow",
                    "workflow_id": workflow_id,
                    "input_payload": workflow_input if isinstance(workflow_input, dict) else {},
                    "expected_evidence": goal.success_criteria,
                }
            )
        steps.append({"action": "evaluate_evidence", "expected_evidence": goal.success_criteria})
        return {"summary": f"Initial plan for {goal.objective}", "steps": steps,
                "expected_evidence": goal.success_criteria}

    def _normalize_plan_step(self, step: Any, index: int) -> dict[str, Any]:
        if not isinstance(step, dict):
            raise ValueError("Goal plan steps must be objects")
        action = str(step.get("action") or "").strip()
        if action not in GOAL_PLAN_STEP_ACTIONS:
            raise ValueError(f"Unsupported goal plan step action '{action}'")
        normalized = dict(step)
        normalized.setdefault("id", f"step-{index + 1}")
        normalized.setdefault("status", "pending")
        normalized["action"] = action
        if action == "start_workflow":
            if not normalized.get("workflow_id"):
                raise ValueError("start_workflow plan steps require workflow_id")
            normalized["input_payload"] = normalized.get("input_payload") if isinstance(normalized.get("input_payload"),
                                                                                        dict) else {}
            normalized["assigned_agents"] = normalized.get("assigned_agents") if isinstance(
                normalized.get("assigned_agents"), list) else []
            normalized["expected_evidence"] = normalized.get("expected_evidence") if isinstance(
                normalized.get("expected_evidence"), list) else []
        return normalized

    def _metadata_with_plan(
            self,
            metadata: dict[str, Any],
            plan: dict[str, Any],
            *,
            reason: str,
            actor: str | None,
    ) -> dict[str, Any]:
        updated = dict(metadata)
        planning = dict(updated.get("goal_planning") or {})
        history = [item for item in planning.get("plan_history", []) if isinstance(item, dict)]
        active_plan = planning.get("active_plan")
        if isinstance(active_plan, dict):
            history.append(active_plan)
        planning["active_plan"] = plan
        planning["plan_history"] = history[-20:]
        planning["last_replan_reason"] = reason
        planning["last_planned_by"] = actor
        planning["updated_at"] = utc_now().isoformat()
        # The monitor checks this compact alias when deciding whether a goal has a next action.
        updated["active_plan"] = plan
        updated["goal_planning"] = planning
        return updated

    @staticmethod
    def _explicit_completion_condition(goal: GoalDefinition) -> bool:
        constraints = goal.constraints if isinstance(goal.constraints, dict) else {}
        return bool(constraints.get("completion_condition") or constraints.get("human_defined_completion_condition"))

    @staticmethod
    def _ensure_not_terminal(goal: GoalDefinition) -> None:
        if goal.status in TERMINAL_GOAL_STATUSES:
            raise GoalTransitionError(f"Goal '{goal.id}' is already terminal with status '{goal.status.value}'")
