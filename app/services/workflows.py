from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.api.context import ApiContext
from app.core.config import get_settings
from app.core.time import utc_now
from app.domain import Execution, ExecutionEvent, ExecutionEventType, WorkflowDefinition
from app.runtime.native.errors import WorkflowNotFoundError
from app.services.conversations.policy import MainAgentPolicyService
from app.services.execution_classification import classify_execution_staleness
from app.services.workflow_validation import WorkflowValidationService


MONITOR_EVENT_TYPES = {
    ExecutionEventType.MONITOR_FINDING_CREATED,
    ExecutionEventType.MONITOR_EVALUATION_RECORDED,
    ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
    ExecutionEventType.MONITOR_IMPROVEMENT_COMPARED,
}


@dataclass(slots=True)
class WorkflowService:
    context: ApiContext

    async def publish_workflow(self, workflow_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        revision = workflow.versioning.revision + 1
        version = payload.get("version") or workflow.versioning.version
        patch = {
            "versioning": {
                "version": version,
                "revision": revision,
                "parent_version": workflow.versioning.version,
                "is_published": True,
                "labels": workflow.versioning.labels,
            },
            "metadata": {
                **workflow.metadata,
                "published_at": payload.get("published_at") or utc_now().isoformat(),
            },
        }
        published = await self.context.workflow_repo.update(workflow_id, patch)
        if published is not None:
            await self.maybe_replace_active_executions_for_revision_change(
                before=workflow,
                after=published,
                restart_requested=bool(payload.get("restart_active_executions")),
                source="workflow_publish",
            )
        return published.model_dump(mode="json")

    async def unpublish_workflow(self, workflow_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        revision = workflow.versioning.revision + 1
        version = payload.get("version") or workflow.versioning.version
        patch = {
            "versioning": {
                "version": version,
                "revision": revision,
                "parent_version": workflow.versioning.version,
                "is_published": False,
                "labels": workflow.versioning.labels,
            },
            "metadata": {
                **workflow.metadata,
                "published_at": None,
                "unpublished_at": payload.get("unpublished_at") or utc_now().isoformat(),
            },
        }
        unpublished = await self.context.workflow_repo.update(workflow_id, patch)
        if unpublished is not None:
            await self.maybe_replace_active_executions_for_revision_change(
                before=workflow,
                after=unpublished,
                restart_requested=bool(payload.get("restart_active_executions")),
                source="workflow_unpublish",
            )
        return unpublished.model_dump(mode="json")

    async def maybe_replace_active_executions_for_revision_change(
            self,
            *,
            before: WorkflowDefinition,
            after: WorkflowDefinition,
            restart_requested: bool = False,
            source: str = "workflow_update",
    ) -> list[str]:
        if before.versioning.revision == after.versioning.revision:
            return []
        settings = get_settings()
        if not restart_requested and not settings.workflow_restart_active_executions_on_revision_change:
            return []
        replace = getattr(self.context.control_plane, "replace_active_executions_for_workflow_revision", None)
        if replace is None:
            return []
        return await replace(
            workflow_id=after.id,
            previous_revision=before.versioning.revision,
            replacement_revision=after.versioning.revision,
            source=source,
        )

    async def validate_workflow(self, payload: WorkflowDefinition) -> dict[str, Any]:
        result = await WorkflowValidationService(self.context).validate(payload)
        return result.__dict__

    async def list_workflow_versions(self, workflow_id: str) -> dict[str, list[dict[str, Any]]]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        list_versions = getattr(self.context.workflow_repo, "list_versions", None)
        if list_versions is None:
            return {"items": [self._current_version_payload(workflow)]}
        items = await list_versions(workflow_id)
        if not items:
            items = [self._current_version_payload(workflow)]
        return {"items": items}

    async def get_workflow_version(self, workflow_id: str, revision: int) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        get_version = getattr(self.context.workflow_repo, "get_version", None)
        item = await get_version(workflow_id, revision) if get_version is not None else None
        if item is None and revision == workflow.versioning.revision:
            item = self._current_version_payload(workflow)
        if item is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' version '{revision}' not found")
        return item

    async def list_workflow_executions(self, workflow_id: str) -> dict[str, Any]:
        items = await self.context.execution_store.list_executions_by_workflow(workflow_id)
        workflow = await self.context.workflow_repo.get(workflow_id)
        return {
            "items": [await self._workflow_execution_payload(item) for item in items],
            "monitoring": (
                self.monitoring_operator_payload(
                    workflow,
                    main_agent_default_workflow_id=await self.main_agent_default_workflow_id(),
                )
                if workflow is not None
                else None
            ),
        }

    async def update_monitoring_controls(self, workflow_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        main_agent_default_workflow_id = await self.main_agent_default_workflow_id()
        if (
                "allow_self_monitoring" in patch
                and workflow.id != main_agent_default_workflow_id
        ):
            raise ValueError(
                "allow_self_monitoring can only be changed on the active main-agent default workflow"
            )
        metadata = dict(workflow.metadata)
        monitoring = dict(metadata.get("main_agent_monitoring") or {})
        allowed_keys = {
            "enabled",
            "level",
            "reason",
            "store_run_summaries",
            "store_failure_summaries",
            "allow_improvement_proposals",
            "allow_evaluation_agent_review",
            "allow_self_monitoring",
            "route_improvement_proposals_to_approval",
            "approval_conversation_id",
            "safe_to_summarize",
            "strong_review_required",
        }
        for key, value in patch.items():
            if key not in allowed_keys:
                continue
            if key == "level" and value is not None:
                value = str(value).strip().lower()
                if value not in {"off", "minimal", "standard", "strict"}:
                    raise ValueError("Monitoring level must be off, minimal, standard, or strict")
            monitoring[key] = value
        metadata["main_agent_monitoring"] = monitoring
        updated = await self.context.workflow_repo.update(workflow_id, {"metadata": metadata})
        assert updated is not None
        return {
            "workflow": updated.model_dump(mode="json"),
            "monitoring": self.monitoring_operator_payload(
                updated,
                main_agent_default_workflow_id=main_agent_default_workflow_id,
            ),
        }

    async def main_agent_default_workflow_id(self) -> str | None:
        profiles = await self.context.main_agent_profile_repo.list()
        enabled = [profile for profile in profiles if getattr(profile, "enabled", True)]
        profile = enabled[0] if enabled else (profiles[0] if profiles else None)
        return getattr(profile, "default_workflow_id", None)

    def monitoring_operator_payload(
            self,
            workflow: WorkflowDefinition,
            *,
            main_agent_default_workflow_id: str | None = None,
    ) -> dict[str, Any]:
        policy = MainAgentPolicyService(self.context)
        summary = policy.workflow_monitoring_summary(workflow)
        monitoring = workflow.metadata.get("main_agent_monitoring")
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        is_main_agent_default_workflow = workflow.id == main_agent_default_workflow_id
        controls = {
            "enabled": summary["enabled"],
            "level": summary["level"],
            "store_run_summaries": bool(monitoring.get("store_run_summaries")),
            "store_failure_summaries": bool(monitoring.get("store_failure_summaries")),
            "allow_improvement_proposals": bool(monitoring.get("allow_improvement_proposals")),
            "allow_evaluation_agent_review": bool(monitoring.get("allow_evaluation_agent_review")),
            "allow_self_monitoring": bool(monitoring.get("allow_self_monitoring")),
            "safe_to_summarize": bool(monitoring.get("safe_to_summarize")),
            "route_improvement_proposals_to_approval": bool(monitoring.get("route_improvement_proposals_to_approval")),
            "approval_conversation_id": monitoring.get("approval_conversation_id"),
        }
        return {
            **summary,
            "is_main_agent_default_workflow": is_main_agent_default_workflow,
            "status_label": self._monitoring_status_label(summary),
            "controls": controls,
            "exemption": {
                "enabled": summary["exempted"],
                "reason": summary.get("reason"),
                "toggle_field": "main_agent_monitoring.enabled",
            },
            "operator_actions": {
                "update_controls": f"/workflows/{workflow.id}/monitoring",
                "list_events": f"/workflows/{workflow.id}/monitoring/events",
                "repair_stale_executions": f"/workflows/{workflow.id}/stale-executions/repair",
            },
        }

    async def workflow_monitoring_events(self, workflow_id: str) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        executions = await self.context.execution_store.list_executions_by_workflow(workflow_id)
        events: list[ExecutionEvent] = []
        for execution in executions:
            for event in await self.context.execution_store.list_events(execution.id):
                if event.event_type in MONITOR_EVENT_TYPES:
                    events.append(event)
        events.sort(key=lambda item: (item.timestamp, item.sequence, item.id))
        approvals = [
            item
            for item in await self.context.conversation_approval_repo.list()
            if item.target_id == workflow_id and item.metadata.get("source") == "main_agent_monitor"
        ]
        return {
            "workflow_id": workflow_id,
            "monitoring": self.monitoring_operator_payload(
                workflow,
                main_agent_default_workflow_id=await self.main_agent_default_workflow_id(),
            ),
            "findings": [
                event.model_dump(mode="json")
                for event in events
                if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED
            ],
            "proposals": [
                {
                    **event.model_dump(mode="json"),
                    "approval_requests": [
                        item.model_dump(mode="json")
                        for item in approvals
                        if item.metadata.get("monitor_proposal_event_id") == event.id
                    ],
                }
                for event in events
                if event.event_type == ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED
            ],
            "evaluations": [
                event.model_dump(mode="json")
                for event in events
                if event.event_type == ExecutionEventType.MONITOR_EVALUATION_RECORDED
            ],
            "comparisons": [
                event.model_dump(mode="json")
                for event in events
                if event.event_type == ExecutionEventType.MONITOR_IMPROVEMENT_COMPARED
            ],
            "approval_controls": [item.model_dump(mode="json") for item in approvals],
        }

    async def repair_stale_workflow_executions(self, workflow_id: str) -> dict[str, Any]:
        if await self.context.workflow_repo.get(workflow_id) is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        repaired = await self.context.control_plane.repair_stale_executions(workflow_id=workflow_id)
        return {
            "workflow_id": workflow_id,
            "items": repaired,
            "repaired_count": len(repaired),
        }

    async def _workflow_execution_payload(self, execution: Execution) -> dict[str, Any]:
        payload = execution.model_dump(mode="json")
        settings = get_settings()
        stale_classification = classify_execution_staleness(
            execution,
            stale_after_seconds=settings.main_agent_workflow_monitor_stale_after_seconds,
        )
        payload["stale_classification"] = stale_classification
        payload["stale_repair_action"] = (
            {
                "available": True,
                "method": "POST",
                "url": f"/workflows/{execution.workflow_id}/stale-executions/repair",
                "reason": stale_classification["reason"],
            }
            if stale_classification["is_stale"]
            else {"available": False}
        )
        monitor_events = [
            event.model_dump(mode="json")
            for event in await self.context.execution_store.list_events(execution.id)
            if event.event_type in MONITOR_EVENT_TYPES
        ]
        payload["monitor_events"] = monitor_events
        return payload

    def _monitoring_status_label(self, summary: dict[str, Any]) -> str:
        if summary["exempted"]:
            return "exempt"
        if not summary["enabled"]:
            return "off"
        return f"{summary['level']}_monitoring"

    def _current_version_payload(self, workflow: WorkflowDefinition) -> dict[str, Any]:
        definition = workflow.model_dump(mode="json")
        metadata = workflow.metadata if isinstance(workflow.metadata, dict) else {}
        return {
            "id": f"{workflow.id}:v{workflow.versioning.revision}",
            "workflow_id": workflow.id,
            "revision": workflow.versioning.revision,
            "version": workflow.versioning.version,
            "status": "published" if workflow.versioning.is_published else "draft",
            "labels": workflow.versioning.labels,
            "parent_version": workflow.versioning.parent_version,
            "is_published": workflow.versioning.is_published,
            "is_current": True,
            "definition": definition,
            "created_at": None,
            "published_at": metadata.get("published_at"),
            "provenance": metadata.get("provenance"),
        }
