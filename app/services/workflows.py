"""Workflow service operations beyond basic catalog CRUD.

This module owns publication state, validation, version/revision lookups,
execution replacement on revision changes, and monitoring payload shaping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.api.context import ApiContext
from app.core.config import get_settings
from app.core.time import utc_now
from app.domain import (
    AgentDefinition,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalTargetType,
    ApprovalType,
    Conversation,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    Execution,
    ExecutionEvent,
    ExecutionEventType,
    PersonaDefinition,
    PersonaVersion,
    TokenBudgetPolicy,
    WorkflowDefinition,
)
from app.runtime.native.errors import WorkflowNotFoundError
from app.services.conversations.channel_registry import chat_channel_types
from app.services.execution_classification import classify_execution_staleness
from app.services.memory import MemoryService
from app.services.workflow_validation import WorkflowValidationService

MONITOR_EVENT_TYPES = {
    ExecutionEventType.MONITOR_FINDING_CREATED,
    ExecutionEventType.MONITOR_EVALUATION_RECORDED,
    ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
    ExecutionEventType.MONITOR_IMPROVEMENT_COMPARED,
    ExecutionEventType.SUPERVISOR_STEERING_REQUESTED,
    ExecutionEventType.SUPERVISOR_STEERING_APPLIED,
}

WORKFLOW_DOCUMENT_LINK_METADATA_KEY = "document_links"
WORKFLOW_SHARED_MEMORY_METADATA_KEY = "shared_memory"

PERSONA_VERSION_PIN_ACCEPTED_FOR_KEY = "persona_version_pin_accepted_for"
PERSONA_VERSION_PIN_DECISION_KEY = "persona_version_pin_decision"
PERSONA_VERSION_PIN_ACCEPTED_AT_KEY = "persona_version_pin_accepted_at"
PERSONA_VERSION_PIN_ACCEPTED_BY_KEY = "persona_version_pin_accepted_by"
PERSONA_AGENT_WORKFLOW_METADATA_KEYS = {
    "workflow_graph_position",
    "workflowGraphPosition",
}


class WorkflowAgentPromotionConflictError(ValueError):
    """Raised when a workflow agent promotion would overwrite an unrelated global agent."""


@dataclass(slots=True)
class WorkflowService:
    """Coordinate workflow lifecycle actions with execution and monitoring services."""

    context: ApiContext

    async def workflow_persona_version_notices(self, workflow_id: str) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        notices = await self._persona_version_notices_for_workflow(workflow, include_current=False)
        return {
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "items": notices,
            "count": len(notices),
            "has_updates": bool(notices),
        }

    async def persona_workflow_usages(self, persona_id: str) -> dict[str, Any]:
        persona = await self.context.persona_repo.get(persona_id, include_deleted=True)
        if persona is None:
            raise WorkflowNotFoundError(f"Persona '{persona_id}' not found")
        workflows = await self.context.workflow_repo.list()
        items: list[dict[str, Any]] = []
        for workflow in workflows:
            items.extend(
                await self._persona_version_notices_for_workflow(
                    workflow,
                    persona=persona,
                    include_current=True,
                )
            )
        return {
            "persona_id": persona.id,
            "persona_slug": persona.slug,
            "current_persona_version_id": persona.current_version_id,
            "published_agent_id": persona.published_agent_id,
            "items": items,
            "count": len(items),
            "outdated_count": len([item for item in items if item["status"] == "outdated"]),
        }

    async def use_latest_persona_agent(
            self,
            workflow_id: str,
            agent_id: str,
            *,
            updated_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        embedded_agent = self._workflow_agent_by_id(workflow, agent_id)
        persona_id = self._persona_id_for_agent(embedded_agent)
        if persona_id is None:
            raise ValueError(f"Agent '{agent_id}' is not a persona-backed workflow agent.")
        persona = await self.context.persona_repo.get(persona_id)
        if persona is None or not persona.current_version_id or not persona.published_agent_id:
            raise WorkflowNotFoundError(f"Persona '{persona_id}' does not have a published agent.")
        latest_agent = await self.context.agent_repo.get(persona.published_agent_id)
        if latest_agent is None:
            raise WorkflowNotFoundError(f"Published persona agent '{persona.published_agent_id}' not found.")

        replacement = self._workflow_embedded_latest_persona_agent(
            latest_agent=latest_agent,
            embedded_agent=embedded_agent,
            persona=persona,
            updated_by_user_id=updated_by_user_id,
        )
        updated_workflow = await self._replace_workflow_agent(workflow, agent_id, replacement)
        notices = await self._persona_version_notices_for_workflow(updated_workflow, persona=persona,
                                                                   include_current=True)
        return {
            "workflow": updated_workflow.model_dump(mode="json"),
            "agent": replacement.model_dump(mode="json"),
            "persona": persona.model_dump(mode="json"),
            "usage": next((item for item in notices if item["agent_id"] == replacement.id), None),
            "persona_version_notices": [
                item for item in notices if item["status"] == "outdated"
            ],
        }

    async def keep_persona_agent_version(
            self,
            workflow_id: str,
            agent_id: str,
            *,
            updated_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        embedded_agent = self._workflow_agent_by_id(workflow, agent_id)
        persona_id = self._persona_id_for_agent(embedded_agent)
        if persona_id is None:
            raise ValueError(f"Agent '{agent_id}' is not a persona-backed workflow agent.")
        persona = await self.context.persona_repo.get(persona_id)
        if persona is None or not persona.current_version_id:
            raise WorkflowNotFoundError(f"Persona '{persona_id}' does not have a current version.")

        metadata = dict(embedded_agent.metadata)
        metadata.update(
            {
                PERSONA_VERSION_PIN_ACCEPTED_FOR_KEY: persona.current_version_id,
                PERSONA_VERSION_PIN_DECISION_KEY: "keep_workflow_snapshot",
                PERSONA_VERSION_PIN_ACCEPTED_AT_KEY: utc_now().isoformat(),
            }
        )
        if updated_by_user_id:
            metadata[PERSONA_VERSION_PIN_ACCEPTED_BY_KEY] = updated_by_user_id
        replacement = embedded_agent.model_copy(update={"metadata": metadata})
        updated_workflow = await self._replace_workflow_agent(workflow, agent_id, replacement)
        notices = await self._persona_version_notices_for_workflow(updated_workflow, persona=persona,
                                                                   include_current=True)
        return {
            "workflow": updated_workflow.model_dump(mode="json"),
            "agent": replacement.model_dump(mode="json"),
            "persona": persona.model_dump(mode="json"),
            "usage": next((item for item in notices if item["agent_id"] == replacement.id), None),
            "persona_version_notices": [
                item for item in notices if item["status"] == "outdated"
            ],
        }

    async def promote_workflow_agent(
            self,
            workflow_id: str,
            agent_id: str,
            *,
            global_agent_id: str | None = None,
            replace_workflow_agent: bool = False,
            promoted_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        embedded_agent = self._workflow_agent_by_id(workflow, agent_id)
        target_agent_id = (
            global_agent_id.strip()
            if isinstance(global_agent_id, str) and global_agent_id.strip()
            else embedded_agent.id
        )
        existing_global_agent = await self.context.agent_repo.get(target_agent_id)
        if existing_global_agent is not None and not self._is_same_promoted_workflow_agent(
                existing_global_agent=existing_global_agent,
                workflow=workflow,
                embedded_agent=embedded_agent,
        ):
            raise WorkflowAgentPromotionConflictError(
                f"Agent '{target_agent_id}' already exists in the global catalog."
            )
        if existing_global_agent is None:
            deleted_global_agent = await self.context.agent_repo.get(target_agent_id, include_deleted=True)
            if deleted_global_agent is not None:
                raise WorkflowAgentPromotionConflictError(
                    f"Agent '{target_agent_id}' already exists in the global catalog history and cannot be reused."
                )

        promoted_agent = self._promoted_global_agent(
            workflow=workflow,
            embedded_agent=embedded_agent,
            target_agent_id=target_agent_id,
            promoted_by_user_id=promoted_by_user_id,
        )
        saved_agent = await self.context.agent_repo.save(promoted_agent)
        updated_workflow: WorkflowDefinition | None = None
        if replace_workflow_agent:
            updated_workflow = await self._replace_workflow_agent(workflow, agent_id, saved_agent)
        return {
            "agent": saved_agent.model_dump(mode="json"),
            "workflow": (
                updated_workflow.model_dump(mode="json")
                if updated_workflow is not None
                else workflow.model_dump(mode="json")
            ),
            "workflow_updated": updated_workflow is not None,
            "promotion": {
                "source_workflow_id": workflow.id,
                "source_workflow_name": workflow.name,
                "source_agent_id": embedded_agent.id,
                "global_agent_id": saved_agent.id,
                "replaced_workflow_agent": replace_workflow_agent,
            },
        }

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
            "delegate_hitl_to_main_agent",
            "route_improvement_proposals_to_approval",
            "route_steering_requests_to_approval",
            "approval_conversation_id",
            "safe_to_summarize",
            "strong_review_required",
            "supervise_token_usage",
            "supervise_context_health",
            "supervise_subagents",
            "supervise_tool_failures",
            "excluded_subagent_ids",
            "excluded_task_ids",
            "allowed_steering_actions",
            "auto_apply_steering_actions",
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
        from app.services.conversations.policy import MainAgentPolicyService

        policy = MainAgentPolicyService(self.context)
        summary = policy.workflow_monitoring_summary(workflow)
        monitoring = workflow.metadata.get("main_agent_monitoring")
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        is_main_agent_default_workflow = workflow.id == main_agent_default_workflow_id

        def explicit_bool(key: str) -> bool | None:
            value = monitoring.get(key) if key in monitoring else None
            return value if isinstance(value, bool) else None

        control_defaults = {
            "store_run_summaries": False,
            "store_failure_summaries": False,
            "allow_improvement_proposals": False,
            # The monitor may request Evaluation-agent review by policy for failed,
            # stale, strict, sensitive, or recently changed workflows unless disabled.
            "allow_evaluation_agent_review": True,
            "allow_self_monitoring": False,
            "delegate_hitl_to_main_agent": False,
            "safe_to_summarize": False,
            "route_improvement_proposals_to_approval": False,
            "route_steering_requests_to_approval": False,
            "supervise_token_usage": True,
            "supervise_context_health": True,
            "supervise_subagents": True,
            "supervise_tool_failures": True,
        }

        def effective_bool(key: str) -> bool:
            explicit = explicit_bool(key)
            return bool(control_defaults[key] if explicit is None else explicit)

        controls = {
            "enabled": summary["enabled"],
            "level": summary["level"],
            "store_run_summaries": effective_bool("store_run_summaries"),
            "store_failure_summaries": effective_bool("store_failure_summaries"),
            "allow_improvement_proposals": effective_bool("allow_improvement_proposals"),
            "allow_evaluation_agent_review": effective_bool("allow_evaluation_agent_review"),
            "allow_self_monitoring": effective_bool("allow_self_monitoring"),
            "delegate_hitl_to_main_agent": effective_bool("delegate_hitl_to_main_agent"),
            "safe_to_summarize": effective_bool("safe_to_summarize"),
            "route_improvement_proposals_to_approval": effective_bool("route_improvement_proposals_to_approval"),
            "route_steering_requests_to_approval": effective_bool("route_steering_requests_to_approval"),
            "approval_conversation_id": monitoring.get("approval_conversation_id"),
            "supervise_token_usage": effective_bool("supervise_token_usage"),
            "supervise_context_health": effective_bool("supervise_context_health"),
            "supervise_subagents": effective_bool("supervise_subagents"),
            "supervise_tool_failures": effective_bool("supervise_tool_failures"),
            "excluded_subagent_ids": monitoring.get("excluded_subagent_ids") or [],
            "excluded_task_ids": monitoring.get("excluded_task_ids") or [],
            "allowed_steering_actions": monitoring.get("allowed_steering_actions"),
            "auto_apply_steering_actions": monitoring.get("auto_apply_steering_actions") or [],
        }
        explicit_controls = {key: explicit_bool(key) for key in control_defaults}
        control_sources = {
            key: "explicit" if explicit_controls[key] is not None else "policy_default"
            for key in control_defaults
        }
        return {
            **summary,
            "is_main_agent_default_workflow": is_main_agent_default_workflow,
            "status_label": self._monitoring_status_label(summary),
            "controls": controls,
            "explicit_controls": explicit_controls,
            "control_sources": control_sources,
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

    async def update_runtime_governance_controls(self, workflow_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        metadata = dict(workflow.metadata)
        runtime_governance = dict(metadata.get("runtime_governance") or {})
        if "token_budget" in patch:
            token_budget = self._normalize_token_budget_patch(
                runtime_governance.get("token_budget"),
                patch.get("token_budget"),
            )
            if token_budget:
                runtime_governance["token_budget"] = token_budget
            else:
                runtime_governance.pop("token_budget", None)
        if "context_compaction" in patch:
            context_compaction = self._normalize_context_compaction_patch(
                runtime_governance.get("context_compaction"),
                patch.get("context_compaction"),
            )
            if context_compaction:
                runtime_governance["context_compaction"] = context_compaction
            else:
                runtime_governance.pop("context_compaction", None)
        metadata["runtime_governance"] = runtime_governance
        workflow_patch: dict[str, Any] = {"metadata": metadata}
        if "execution_policy" in patch:
            workflow_patch.update(self._normalize_execution_policy_patch(patch.get("execution_policy")))
        updated = await self.context.workflow_repo.update(workflow_id, workflow_patch)
        assert updated is not None
        return {
            "workflow": updated.model_dump(mode="json"),
            "runtime_governance": self.runtime_governance_operator_payload(updated),
        }

    def runtime_governance_operator_payload(self, workflow: WorkflowDefinition) -> dict[str, Any]:
        metadata = workflow.metadata if isinstance(workflow.metadata, dict) else {}
        runtime_governance = metadata.get("runtime_governance")
        runtime_governance = runtime_governance if isinstance(runtime_governance, dict) else {}
        token_budget = runtime_governance.get("token_budget")
        token_budget = token_budget if isinstance(token_budget, dict) else {}
        context_compaction = runtime_governance.get("context_compaction")
        context_compaction = context_compaction if isinstance(context_compaction, dict) else {}
        settings = get_settings()
        persist_explicit = "persist_context_pack" in context_compaction
        return {
            "workflow_id": workflow.id,
            "token_budget": {
                "configured": bool(token_budget),
                "run_total_tokens": token_budget.get("run_total_tokens"),
                "workflow_total_tokens": token_budget.get("workflow_total_tokens"),
                "agent_total_tokens": token_budget.get("agent_total_tokens"),
                "warn_ratio": token_budget.get("warn_ratio", settings.agent_token_budget_warn_ratio),
                "hard_ratio": token_budget.get("hard_ratio", settings.agent_token_budget_hard_ratio),
                "action": token_budget.get("action", settings.agent_token_budget_action),
            },
            "context_compaction": {
                "enabled": context_compaction.get("enabled", True) is not False,
                "persist_context_pack": bool(
                    context_compaction.get(
                        "persist_context_pack",
                        settings.agent_context_compaction_persist_context_pack_default,
                    )
                ),
                "persist_context_pack_source": "workflow" if persist_explicit else "global_default",
                "preserve_recent_messages": context_compaction.get("preserve_recent_messages", 1),
                "oversized_message_tokens": context_compaction.get("oversized_message_tokens", 600),
                "min_estimated_tokens_saved": context_compaction.get("min_estimated_tokens_saved", 50),
                "max_summary_chars": context_compaction.get("max_summary_chars", 5000),
            },
            "execution_policy": {
                "configured": any(
                    value is not None
                    for value in (
                        workflow.max_runtime_seconds,
                        workflow.max_retries,
                        workflow.concurrency_limit,
                        workflow.approval_mode,
                    )
                ),
                "max_runtime_seconds": workflow.max_runtime_seconds,
                "max_retries": workflow.max_retries,
                "concurrency_limit": workflow.concurrency_limit,
                "approval_mode": workflow.approval_mode or "task_policy",
                "effective_concurrency_limit": 1,
            },
            "operator_actions": {
                "update_controls": f"/workflows/{workflow.id}/runtime-governance",
            },
        }

    def _normalize_execution_policy_patch(self, patch: Any) -> dict[str, Any]:
        if patch is None:
            return {
                "max_runtime_seconds": None,
                "max_retries": None,
                "concurrency_limit": None,
                "approval_mode": None,
            }
        if not isinstance(patch, dict):
            raise ValueError("execution_policy must be an object")
        allowed_keys = {
            "max_runtime_seconds",
            "max_retries",
            "concurrency_limit",
            "approval_mode",
        }
        unknown = sorted(set(patch) - allowed_keys)
        if unknown:
            raise ValueError(f"Unsupported execution_policy fields: {', '.join(unknown)}")

        normalized: dict[str, Any] = {}
        for key in ("max_runtime_seconds", "concurrency_limit"):
            if key not in patch:
                continue
            value = patch[key]
            if value is None:
                normalized[key] = None
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be a positive integer") from exc
            if parsed <= 0:
                raise ValueError(f"{key} must be a positive integer")
            normalized[key] = parsed

        if "max_retries" in patch:
            value = patch["max_retries"]
            if value is None:
                normalized["max_retries"] = None
            else:
                try:
                    parsed = int(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("max_retries must be a non-negative integer") from exc
                if parsed < 0:
                    raise ValueError("max_retries must be a non-negative integer")
                normalized["max_retries"] = parsed

        if "approval_mode" in patch:
            value = patch["approval_mode"]
            if value is None:
                normalized["approval_mode"] = None
            elif value in {"task_policy", "before_run", "all_tasks"}:
                normalized["approval_mode"] = value
            else:
                raise ValueError("approval_mode must be task_policy, before_run, or all_tasks")
        return normalized

    def _normalize_token_budget_patch(self, current: Any, patch: Any) -> dict[str, Any]:
        if patch is None:
            return {}
        if not isinstance(patch, dict):
            raise ValueError("token_budget must be an object")
        allowed_keys = {
            "run_total_tokens",
            "workflow_total_tokens",
            "agent_total_tokens",
            "warn_ratio",
            "hard_ratio",
            "action",
        }
        unknown = sorted(set(patch) - allowed_keys)
        if unknown:
            raise ValueError(f"Unsupported token_budget fields: {', '.join(unknown)}")
        merged = dict(current or {}) if isinstance(current, dict) else {}
        for key, value in patch.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        if not merged:
            return {}
        policy = TokenBudgetPolicy.model_validate(merged)
        return {
            key: value
            for key, value in policy.model_dump(mode="json").items()
            if value is not None
        }

    def _normalize_context_compaction_patch(self, current: Any, patch: Any) -> dict[str, Any]:
        if patch is None:
            return {}
        if not isinstance(patch, dict):
            raise ValueError("context_compaction must be an object")
        allowed_keys = {
            "enabled",
            "persist_context_pack",
            "preserve_recent_messages",
            "oversized_message_tokens",
            "min_estimated_tokens_saved",
            "max_summary_chars",
        }
        unknown = sorted(set(patch) - allowed_keys)
        if unknown:
            raise ValueError(f"Unsupported context_compaction fields: {', '.join(unknown)}")
        merged = dict(current or {}) if isinstance(current, dict) else {}
        for key, value in patch.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = self._normalize_context_compaction_value(key, value)
        return merged

    def _normalize_context_compaction_value(self, key: str, value: Any) -> Any:
        if key in {"enabled", "persist_context_pack"}:
            return bool(value)
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if key == "preserve_recent_messages":
            return min(max(parsed, 0), 10)
        if key == "oversized_message_tokens":
            return max(parsed, 50)
        if key == "min_estimated_tokens_saved":
            return max(parsed, 0)
        if key == "max_summary_chars":
            return min(max(parsed, 1200), 20000)
        return parsed

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
        dispatches_by_event_id = await self._monitor_proposal_dispatches(workflow_id)
        proposal_history = self._workflow_improvement_proposal_history(workflow)
        steering_approval_history = self._workflow_steering_approval_history(workflow)
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
                    "dispatches": dispatches_by_event_id.get(event.id, []),
                }
                for event in events
                if event.event_type == ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED
            ],
            "proposal_history": proposal_history,
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
            "steering_requests": [
                event.model_dump(mode="json")
                for event in events
                if event.event_type == ExecutionEventType.SUPERVISOR_STEERING_REQUESTED
            ],
            "steering_applied": [
                event.model_dump(mode="json")
                for event in events
                if event.event_type == ExecutionEventType.SUPERVISOR_STEERING_APPLIED
            ],
            "steering_approval_history": steering_approval_history,
            "approval_controls": [item.model_dump(mode="json") for item in approvals],
        }

    async def workflow_improvement_proposals(
            self,
            workflow_id: str,
            *,
            proposal_id: str | None = None,
            status: str | None = None,
            limit: int | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        items = self._workflow_improvement_proposal_history(workflow)
        filtered = self._filter_governance_items(items, item_id=proposal_id, status=status, limit=limit)
        return {
            "workflow_id": workflow.id,
            "items": filtered,
            "count": len(filtered),
            "total_count": len(items),
        }

    async def create_workflow_improvement_proposal(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        metadata, monitoring = self._workflow_monitoring_metadata(workflow)
        # Reuse the monitor-owned metadata history so operator-created and monitor-created
        # governance proposals stay visible in the same review surface.
        history = self._raw_history_list(monitoring.get("improvement_proposals"))
        entry = self._new_improvement_proposal_entry(workflow, payload)
        history.append(entry)
        monitoring["improvement_proposals"] = history[-50:]
        monitoring["last_improvement_proposal_id"] = entry["id"]
        metadata["main_agent_monitoring"] = monitoring
        updated = await self.context.workflow_repo.update(workflow.id, {"metadata": metadata})
        assert updated is not None
        return {
            "workflow": updated.model_dump(mode="json"),
            "proposal": self._workflow_improvement_proposal_by_id(updated, entry["id"]),
        }

    async def update_workflow_improvement_proposal(
            self,
            workflow_id: str,
            proposal_id: str,
            patch: dict[str, Any],
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        metadata, monitoring = self._workflow_monitoring_metadata(workflow)
        history = self._raw_history_list(monitoring.get("improvement_proposals"))
        target_index = self._find_governance_item_index(history, proposal_id, id_keys=("id", "proposal_event_id"))
        if target_index is None:
            raise ValueError(f"Improvement proposal '{proposal_id}' was not found for workflow '{workflow_id}'.")
        history[target_index] = self._apply_improvement_proposal_patch(history[target_index], patch)
        monitoring["improvement_proposals"] = history[-50:]
        metadata["main_agent_monitoring"] = monitoring
        updated = await self.context.workflow_repo.update(workflow.id, {"metadata": metadata})
        assert updated is not None
        return {
            "workflow": updated.model_dump(mode="json"),
            "proposal": self._workflow_improvement_proposal_by_id(updated, proposal_id),
        }

    async def workflow_steering_approvals(
            self,
            workflow_id: str,
            *,
            approval_id: str | None = None,
            status: str | None = None,
            limit: int | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        items = self._workflow_steering_approval_history(workflow)
        filtered = self._filter_governance_items(items, item_id=approval_id, status=status, limit=limit)
        return {
            "workflow_id": workflow.id,
            "items": filtered,
            "count": len(filtered),
            "total_count": len(items),
        }

    async def workflow_governance_audit(self, workflow_id: str) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        proposal_items = self._workflow_improvement_proposal_history(workflow)
        steering_items = self._workflow_steering_approval_history(workflow)
        approvals = [
            item for item in await self.context.conversation_approval_repo.list()
            if item.target_id == workflow_id
        ]
        approvals_by_id = {item.id: item for item in approvals}
        referenced_approval_ids = {
            approval_id
            for approval_id in [
                *[self._normalized_optional_string(item.get("approval_request_id")) for item in proposal_items],
                *[self._normalized_optional_string(item.get("approval_request_id")) for item in steering_items],
            ]
            if approval_id
        }
        proposal_checks = [self._governance_record_audit("improvement_proposal", item, approvals_by_id) for item in
                           proposal_items]
        steering_checks = [self._governance_record_audit("steering_approval", item, approvals_by_id) for item in
                           steering_items]
        orphaned_approvals = [
            self._approval_audit_payload(item)
            for item in approvals
            if item.id not in referenced_approval_ids
               and item.metadata.get("source") in {"workflow_service", "main_agent_monitor"}
        ]
        mismatches = [
            item
            for item in [*proposal_checks, *steering_checks]
            if item["audit"]["status"] != "ok"
        ]
        return {
            "workflow_id": workflow.id,
            "summary": {
                "proposal_count": len(proposal_items),
                "steering_approval_count": len(steering_items),
                "approval_request_count": len(approvals),
                "mismatch_count": len(mismatches),
                "orphaned_approval_count": len(orphaned_approvals),
            },
            "proposals": proposal_checks,
            "steering_approvals": steering_checks,
            "orphaned_approvals": orphaned_approvals,
        }

    async def repair_workflow_governance_record(
            self,
            workflow_id: str,
            *,
            record_kind: str,
            record_id: str,
            action: str,
            approval_request_id: str | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        normalized_kind = self._normalize_governance_record_kind(record_kind)
        normalized_record_id = self._required_string(record_id, field="record_id")
        normalized_action = self._normalize_governance_repair_action(action)

        if normalized_action == "link_approval_request":
            linked_approval_id = self._required_string(approval_request_id, field="approval_request_id")
            approval = await self.context.conversation_approval_repo.get(linked_approval_id)
            if approval is None:
                raise ValueError(f"Approval request '{linked_approval_id}' was not found.")
            if approval.target_id != workflow.id:
                raise ValueError(
                    f"Approval request '{linked_approval_id}' targets workflow '{approval.target_id}', not '{workflow.id}'."
                )
            updated_approval = await self._sync_governance_approval_metadata(
                approval,
                workflow_id=workflow.id,
                record_kind=normalized_kind,
                record_id=normalized_record_id,
            )
            decision_patch = self._governance_patch_from_approval(updated_approval)
            updated_record = await self._update_governance_record_by_kind(
                workflow.id,
                normalized_kind,
                normalized_record_id,
                decision_patch,
            )
            return await self._governance_repair_result(
                workflow.id,
                normalized_kind,
                normalized_action,
                updated_record,
                approval_request=updated_approval,
            )

        if normalized_action == "unlink_approval_request":
            updated_record = await self._update_governance_record_by_kind(
                workflow.id,
                normalized_kind,
                normalized_record_id,
                {"approval_request_id": None},
            )
            return await self._governance_repair_result(
                workflow.id,
                normalized_kind,
                normalized_action,
                updated_record,
                approval_request=None,
            )

        current_record = self._workflow_governance_record_by_kind(workflow, normalized_kind, normalized_record_id)
        linked_approval_id = self._normalized_optional_string(current_record.get("approval_request_id"))
        if not linked_approval_id:
            raise ValueError(
                f"{normalized_kind.replace('_', ' ').title()} '{normalized_record_id}' has no linked approval request."
            )
        approval = await self.context.conversation_approval_repo.get(linked_approval_id)
        if approval is None:
            raise ValueError(f"Approval request '{linked_approval_id}' was not found.")
        if approval.target_id != workflow.id:
            raise ValueError(
                f"Approval request '{linked_approval_id}' targets workflow '{approval.target_id}', not '{workflow.id}'."
            )
        updated_record = await self._update_governance_record_by_kind(
            workflow.id,
            normalized_kind,
            normalized_record_id,
            self._governance_patch_from_approval(approval),
        )
        return await self._governance_repair_result(
            workflow.id,
            normalized_kind,
            normalized_action,
            updated_record,
            approval_request=approval,
        )

    async def remediate_workflow_governance(
            self,
            workflow_id: str,
            *,
            dry_run: bool = False,
            sync_status_mismatches: Any = None,
            clear_orphaned_references: Any = None,
            adopt_orphaned_approvals: Any = None,
    ) -> dict[str, Any]:
        audit = await self.workflow_governance_audit(workflow_id)
        sync_enabled = self._effective_bool(sync_status_mismatches, default=True)
        clear_enabled = self._effective_bool(clear_orphaned_references, default=True)
        adopt_enabled = self._effective_bool(adopt_orphaned_approvals, default=True)
        planned_actions: list[dict[str, Any]] = []

        governance_items = list(audit.get("proposals") or []) + list(audit.get("steering_approvals") or [])
        for item in governance_items:
            record = item.get("record") if isinstance(item, dict) else None
            audit_payload = item.get("audit") if isinstance(item, dict) else None
            if not isinstance(record, dict) or not isinstance(audit_payload, dict):
                continue
            record_id = self._normalized_optional_string(record.get("id"))
            record_kind = self._normalized_optional_string(item.get("kind"))
            status = self._normalized_optional_string(audit_payload.get("status"))
            if not record_id or not record_kind or not status:
                continue
            if status == "status_mismatch" and sync_enabled:
                planned_actions.append(
                    self._governance_remediation_action(
                        record_kind=record_kind,
                        record_id=record_id,
                        action="sync_status_from_approval",
                        reason=self._normalized_optional_string(audit_payload.get("reason")),
                    )
                )
            elif status == "orphaned_reference" and clear_enabled:
                planned_actions.append(
                    self._governance_remediation_action(
                        record_kind=record_kind,
                        record_id=record_id,
                        action="unlink_approval_request",
                        approval_request_id=self._normalized_optional_string(record.get("approval_request_id")),
                        reason=self._normalized_optional_string(audit_payload.get("reason")),
                    )
                )

        if adopt_enabled:
            adopt_actions = await self._adoptable_orphaned_approval_actions(workflow_id, audit)
            planned_actions.extend(adopt_actions)

        applied_actions: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if not dry_run:
            for action in planned_actions:
                try:
                    result = await self.repair_workflow_governance_record(
                        workflow_id,
                        record_kind=str(action["record_kind"]),
                        record_id=str(action["record_id"]),
                        action=str(action["action"]),
                        approval_request_id=self._normalized_optional_string(action.get("approval_request_id")),
                    )
                except ValueError as exc:
                    errors.append(
                        {
                            "record_kind": action["record_kind"],
                            "record_id": action["record_id"],
                            "action": action["action"],
                            "error": str(exc),
                        }
                    )
                    continue
                applied_actions.append(
                    {
                        **action,
                        "result_audit": result.get("audit"),
                    }
                )

        final_audit = audit if dry_run else await self.workflow_governance_audit(workflow_id)
        return {
            "workflow_id": workflow_id,
            "dry_run": dry_run,
            "options": {
                "sync_status_mismatches": sync_enabled,
                "clear_orphaned_references": clear_enabled,
                "adopt_orphaned_approvals": adopt_enabled,
            },
            "summary": {
                "planned_action_count": len(planned_actions),
                "applied_action_count": 0 if dry_run else len(applied_actions),
                "error_count": len(errors),
            },
            "planned_actions": planned_actions,
            "applied_actions": applied_actions,
            "errors": errors,
            "audit_before": audit,
            "audit_after": final_audit,
        }

    async def workflow_governance_review_queue(
            self,
            workflow_id: str,
            *,
            limit: int | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        bounded_limit = self._bounded_limit(limit, default=20, maximum=100)
        proposals = self._workflow_improvement_proposal_history(workflow)
        steering = self._workflow_steering_approval_history(workflow)
        document_links = self._workflow_document_link_history(workflow)
        audit = await self.workflow_governance_audit(workflow_id)
        remediation_preview = await self.remediate_workflow_governance(workflow_id, dry_run=True)
        evidence_links_by_target = self._document_links_by_target(document_links)

        proposal_queue = [
            self._governance_review_queue_item(
                record_kind="improvement_proposal",
                record=item,
                audit=self._audit_for_record(audit.get("proposals") or [], record_id=str(item["id"])),
                evidence_links=evidence_links_by_target.get(("improvement_proposal", str(item["id"])), []),
            )
            for item in proposals
        ]
        steering_queue = [
            self._governance_review_queue_item(
                record_kind="steering_approval",
                record=item,
                audit=self._audit_for_record(audit.get("steering_approvals") or [], record_id=str(item["id"])),
                evidence_links=evidence_links_by_target.get(("steering_approval", str(item["id"])), []),
            )
            for item in steering
        ]
        actionable_items = [
            item for item in [*proposal_queue, *steering_queue]
            if item["priority"] != "resolved" or "reopen" in (item.get("next_actions") or [])
        ]
        actionable_items.sort(key=lambda item: self._governance_queue_sort_key(item))

        recommendations = self._governance_review_recommendations(
            actionable_items=actionable_items,
            orphaned_approvals=audit.get("orphaned_approvals") or [],
            remediation_preview=remediation_preview,
        )
        return {
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "summary": {
                "proposal_count": len(proposals),
                "steering_approval_count": len(steering),
                "actionable_count": len(actionable_items),
                "orphaned_approval_count": len(audit.get("orphaned_approvals") or []),
                "remediation_candidate_count": remediation_preview["summary"]["planned_action_count"],
            },
            "items": actionable_items[:bounded_limit],
            "proposals": proposal_queue[:bounded_limit],
            "steering_approvals": steering_queue[:bounded_limit],
            "orphaned_approvals": list(audit.get("orphaned_approvals") or [])[:bounded_limit],
            "recommendations": recommendations,
            "remediation_preview": remediation_preview,
            "operator_actions": {
                "audit": "agency.workflow.governance.audit",
                "repair": "agency.workflow.governance.repair",
                "remediate": "agency.workflow.governance.remediate",
                "act": "agency.workflow.governance.act",
                "document_suggest": "agency.workflow.governance.document-suggest",
                "document_links": "agency.workflow.document-links",
            },
        }

    async def execute_workflow_governance_action(
            self,
            workflow_id: str,
            *,
            action: str,
            actor_user_id: str,
            record_kind: str | None = None,
            record_id: str | None = None,
            document_id: str | None = None,
            label: str | None = None,
            summary: str | None = None,
            linked_by: str | None = None,
            metadata: dict[str, Any] | None = None,
            sync_status_mismatches: Any = None,
            clear_orphaned_references: Any = None,
            adopt_orphaned_approvals: Any = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        normalized_action = self._normalize_governance_queue_action(action)
        normalized_kind = self._normalize_governance_record_kind(record_kind) if record_kind is not None else None
        normalized_record_id = self._normalized_optional_string(record_id)

        if normalized_action == "apply_remediation":
            remediation = await self.remediate_workflow_governance(
                workflow_id,
                dry_run=False,
                sync_status_mismatches=sync_status_mismatches,
                clear_orphaned_references=clear_orphaned_references,
                adopt_orphaned_approvals=adopt_orphaned_approvals,
            )
            return {
                "workflow_id": workflow_id,
                "action": normalized_action,
                "result": remediation,
            }

        if not normalized_kind or not normalized_record_id:
            raise ValueError("record_kind and record_id are required for this governance action.")

        # Validate the record exists up front so action routing stays deterministic.
        self._workflow_governance_record_by_kind(workflow, normalized_kind, normalized_record_id)

        if normalized_action == "request_approval":
            if normalized_kind == "improvement_proposal":
                result = await self.request_workflow_improvement_proposal_approval(
                    workflow_id,
                    normalized_record_id,
                    actor_user_id=actor_user_id,
                )
            else:
                result = await self.request_workflow_steering_approval(
                    workflow_id,
                    normalized_record_id,
                    actor_user_id=actor_user_id,
                )
            return {
                "workflow_id": workflow_id,
                "action": normalized_action,
                "record_kind": normalized_kind,
                "record_id": normalized_record_id,
                "result": result,
            }

        if normalized_action in {"resolve", "dismiss", "reopen"}:
            result = await self.transition_workflow_governance_record(
                workflow_id,
                record_kind=normalized_kind,
                record_id=normalized_record_id,
                action=normalized_action,
                actor_user_id=actor_user_id,
            )
            return {
                "workflow_id": workflow_id,
                "action": normalized_action,
                "record_kind": normalized_kind,
                "record_id": normalized_record_id,
                "result": result,
            }

        linked_document_id = self._required_string(document_id, field="document_id")
        evidence_link = await self.add_workflow_document_link(
            workflow_id,
            {
                "document_id": linked_document_id,
                "target_type": normalized_kind,
                "target_id": normalized_record_id,
                "label": label,
                "summary": summary,
                "linked_by": linked_by or actor_user_id,
                "metadata": metadata,
            },
        )
        return {
            "workflow_id": workflow_id,
            "action": normalized_action,
            "record_kind": normalized_kind,
            "record_id": normalized_record_id,
            "document_id": linked_document_id,
            "result": evidence_link,
        }

    async def transition_workflow_governance_record(
            self,
            workflow_id: str,
            *,
            record_kind: str,
            record_id: str,
            action: str,
            actor_user_id: str,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        record = self._workflow_governance_record_by_kind(workflow, record_kind, record_id)
        status = self._normalized_optional_string(record.get("status")) or "unknown"
        linked_approval_request_id = self._normalized_optional_string(record.get("approval_request_id"))
        if linked_approval_request_id:
            raise ValueError(
                "Governance lifecycle actions are blocked while an approval request is linked. "
                "Resolve the approval or repair the governance link first."
            )
        patch = self._governance_record_transition_patch(
            record_kind=record_kind,
            action=action,
            status=status,
            actor_user_id=actor_user_id,
        )
        updated_record = await self._update_governance_record_by_kind(workflow_id, record_kind, record_id, patch)
        return {
            "record": updated_record,
            "transition": action,
            "previous_status": status,
            "status": self._normalized_optional_string(updated_record.get("status")) or status,
        }

    async def suggest_workflow_governance_documents(
            self,
            workflow_id: str,
            *,
            actor_user_id: str,
            record_kind: str,
            record_id: str,
            limit: int | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        normalized_kind = self._normalize_governance_record_kind(record_kind)
        normalized_record_id = self._required_string(record_id, field="record_id")
        record = self._workflow_governance_record_by_kind(workflow, normalized_kind, normalized_record_id)
        repo = getattr(self.context, "uploaded_document_repo", None)
        if repo is None or not hasattr(repo, "query"):
            documents = []
        else:
            documents = await repo.query(
                workflow_id=workflow_id,
                user_id=actor_user_id,
                limit=self._bounded_limit(limit, default=20, maximum=50),
            )
        document_links = self._workflow_document_link_history(workflow)
        suggestions = [
            self._governance_document_suggestion(
                record_kind=normalized_kind,
                record=record,
                document=document,
                links=document_links,
            )
            for document in documents
        ]
        suggestions = [item for item in suggestions if item["score"] > 0]
        suggestions.sort(
            key=lambda item: (-int(item["score"]), str(item["document"]["filename"]), str(item["document"]["id"]))
        )
        bounded_limit = self._bounded_limit(limit, default=5, maximum=20)
        return {
            "workflow_id": workflow.id,
            "record_kind": normalized_kind,
            "record_id": normalized_record_id,
            "record": record,
            "items": suggestions[:bounded_limit],
            "count": min(len(suggestions), bounded_limit),
            "total_count": len(suggestions),
        }

    async def execute_workflow_governance_bundle(
            self,
            workflow_id: str,
            *,
            actor_user_id: str,
            record_kind: str,
            record_id: str,
            attach_top_suggestion: Any = None,
            request_approval: Any = None,
            document_limit: int | None = None,
            evidence_label: str | None = None,
            evidence_summary: str | None = None,
            metadata: dict[str, Any] | None = None,
            dry_run: bool = False,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        normalized_kind = self._normalize_governance_record_kind(record_kind)
        normalized_record_id = self._required_string(record_id, field="record_id")
        record = self._workflow_governance_record_by_kind(workflow, normalized_kind, normalized_record_id)
        attach_enabled = self._effective_bool(attach_top_suggestion, default=True)
        approval_enabled = self._effective_bool(request_approval, default=True)
        suggestions = await self.suggest_workflow_governance_documents(
            workflow_id,
            actor_user_id=actor_user_id,
            record_kind=normalized_kind,
            record_id=normalized_record_id,
            limit=document_limit,
        )
        top_suggestion = suggestions["items"][0] if suggestions["items"] else None
        planned_steps: list[dict[str, Any]] = []
        applied_steps: list[dict[str, Any]] = []

        if attach_enabled:
            if top_suggestion is None:
                planned_steps.append(
                    {
                        "action": "attach_top_suggestion",
                        "status": "skipped",
                        "reason": "No matching document suggestion was available.",
                    }
                )
            else:
                planned_steps.append(
                    {
                        "action": "attach_top_suggestion",
                        "status": "planned",
                        "document_id": top_suggestion["document"]["id"],
                        "reason": top_suggestion.get("reason"),
                    }
                )
        if approval_enabled:
            planned_steps.append({"action": "request_approval", "status": "planned"})

        if not dry_run and attach_enabled and top_suggestion is not None:
            attach_result = await self.execute_workflow_governance_action(
                workflow_id,
                action="attach_evidence",
                actor_user_id=actor_user_id,
                record_kind=normalized_kind,
                record_id=normalized_record_id,
                document_id=str(top_suggestion["document"]["id"]),
                label=evidence_label,
                summary=evidence_summary or top_suggestion.get("reason"),
                metadata=metadata,
            )
            applied_steps.append(
                {
                    "action": "attach_top_suggestion",
                    "status": "applied",
                    "document_id": top_suggestion["document"]["id"],
                    "result": attach_result,
                }
            )
        if not dry_run and approval_enabled:
            approval_result = await self.execute_workflow_governance_action(
                workflow_id,
                action="request_approval",
                actor_user_id=actor_user_id,
                record_kind=normalized_kind,
                record_id=normalized_record_id,
            )
            applied_steps.append(
                {
                    "action": "request_approval",
                    "status": "applied",
                    "result": approval_result,
                }
            )

        return {
            "workflow_id": workflow_id,
            "record_kind": normalized_kind,
            "record_id": normalized_record_id,
            "record": record,
            "dry_run": dry_run,
            "options": {
                "attach_top_suggestion": attach_enabled,
                "request_approval": approval_enabled,
            },
            "suggestions": suggestions,
            "planned_steps": planned_steps,
            "applied_steps": applied_steps,
        }

    async def create_workflow_steering_approval(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        metadata, monitoring = self._workflow_monitoring_metadata(workflow)
        history = self._raw_history_list(monitoring.get("steering_approvals"))
        entry = self._new_steering_approval_entry(workflow, payload)
        history.append(entry)
        monitoring["steering_approvals"] = history[-50:]
        monitoring["last_steering_approval_id"] = entry["id"]
        metadata["main_agent_monitoring"] = monitoring
        updated = await self.context.workflow_repo.update(workflow.id, {"metadata": metadata})
        assert updated is not None
        return {
            "workflow": updated.model_dump(mode="json"),
            "approval": self._workflow_steering_approval_by_id(updated, entry["id"]),
        }

    async def update_workflow_steering_approval(
            self,
            workflow_id: str,
            approval_id: str,
            patch: dict[str, Any],
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        metadata, monitoring = self._workflow_monitoring_metadata(workflow)
        history = self._raw_history_list(monitoring.get("steering_approvals"))
        target_index = self._find_governance_item_index(history, approval_id, id_keys=("id", "approval_request_id"))
        if target_index is None:
            raise ValueError(f"Steering approval '{approval_id}' was not found for workflow '{workflow_id}'.")
        history[target_index] = self._apply_steering_approval_patch(history[target_index], patch)
        monitoring["steering_approvals"] = history[-50:]
        metadata["main_agent_monitoring"] = monitoring
        updated = await self.context.workflow_repo.update(workflow.id, {"metadata": metadata})
        assert updated is not None
        return {
            "workflow": updated.model_dump(mode="json"),
            "approval": self._workflow_steering_approval_by_id(updated, approval_id),
        }

    async def workflow_document_links(
            self,
            workflow_id: str,
            *,
            link_id: str | None = None,
            target_type: str | None = None,
            target_id: str | None = None,
            document_id: str | None = None,
            limit: int | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        items = self._workflow_document_link_history(workflow)
        filtered = items
        if link_id:
            filtered = [item for item in filtered if item["id"] == link_id]
        normalized_target_type = self._normalized_optional_string(target_type)
        if normalized_target_type:
            filtered = [item for item in filtered if item["target_type"] == normalized_target_type]
        normalized_target_id = self._normalized_optional_string(target_id)
        if normalized_target_id:
            filtered = [item for item in filtered if item.get("target_id") == normalized_target_id]
        normalized_document_id = self._normalized_optional_string(document_id)
        if normalized_document_id:
            filtered = [item for item in filtered if item["document_id"] == normalized_document_id]
        filtered = filtered[-self._bounded_limit(limit, default=20, maximum=100):]
        return {
            "workflow_id": workflow.id,
            "items": filtered,
            "count": len(filtered),
            "total_count": len(items),
        }

    async def add_workflow_document_link(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        document_id = self._required_string(payload.get("document_id"), field="document_id")
        document = await self.context.uploaded_document_repo.get(document_id)
        if document is None:
            raise ValueError(f"Uploaded document '{document_id}' was not found.")
        target_type = self._normalize_document_link_target_type(payload.get("target_type"))
        target_id = self._normalize_document_link_target_id(
            workflow,
            target_type=target_type,
            target_id=payload.get("target_id"),
        )
        metadata = dict(workflow.metadata)
        links = self._raw_history_list(metadata.get(WORKFLOW_DOCUMENT_LINK_METADATA_KEY))
        link = {
            "id": f"workflow-document-link-{uuid4().hex[:12]}",
            "workflow_id": workflow.id,
            "document_id": document.id,
            "target_type": target_type,
            "target_id": target_id,
            "label": self._normalized_optional_string(payload.get("label")) or document.filename,
            "summary": self._normalized_optional_string(payload.get("summary")),
            "linked_at": utc_now().isoformat(),
            "linked_by": self._normalized_optional_string(payload.get("linked_by")),
            "metadata": self._dict_payload(payload.get("metadata")),
        }
        links.append(link)
        metadata[WORKFLOW_DOCUMENT_LINK_METADATA_KEY] = links[-100:]
        updated = await self.context.workflow_repo.update(workflow.id, {"metadata": metadata})
        assert updated is not None
        return {
            "workflow": updated.model_dump(mode="json"),
            "link": self._workflow_document_link_by_id(updated, link["id"]),
            "document": document.model_dump(mode="json"),
        }

    async def delete_workflow_document_link(self, workflow_id: str, link_id: str) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        metadata = dict(workflow.metadata)
        links = self._raw_history_list(metadata.get(WORKFLOW_DOCUMENT_LINK_METADATA_KEY))
        remaining = [item for item in links if self._normalized_optional_string(item.get("id")) != link_id]
        deleted = len(remaining) != len(links)
        if deleted:
            metadata[WORKFLOW_DOCUMENT_LINK_METADATA_KEY] = remaining
            updated = await self.context.workflow_repo.update(workflow.id, {"metadata": metadata})
            assert updated is not None
        else:
            updated = workflow
        return {
            "workflow": updated.model_dump(mode="json"),
            "deleted": deleted,
            "link_id": link_id,
            "items": self._workflow_document_link_history(updated),
        }

    async def summarize_workflow_document(self, workflow_id: str, document_id: str) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        document = await self.context.uploaded_document_repo.get(document_id)
        if document is None:
            raise ValueError(f"Uploaded document '{document_id}' was not found.")
        text = (document.extracted_text or "").strip()
        metadata = document.metadata if isinstance(document.metadata, dict) else {}
        upload_intelligence = (
            metadata.get("upload_intelligence")
            if isinstance(metadata.get("upload_intelligence"), dict)
            else {}
        )
        preview = text[:1200] if text else None
        headline = (
                self._normalized_optional_string(upload_intelligence.get("summary"))
                or self._first_nonempty_line(text)
                or document.filename
        )
        return {
            "workflow_id": workflow.id,
            "document_id": document.id,
            "document": document.model_dump(mode="json"),
            "summary": {
                "headline": headline,
                "document_kind": self._normalized_optional_string(upload_intelligence.get("document_kind")),
                "recommended_scope": self._nested_optional_string(upload_intelligence, "recommended", "scope"),
                "tags": self._nested_string_list(upload_intelligence, "recommended", "tags"),
                "text_characters": document.text_characters,
                "estimated_tokens": document.estimated_tokens,
                "has_extracted_text": bool(text),
                "preview": preview,
                "linked_targets": [
                    item
                    for item in self._workflow_document_link_history(workflow)
                    if item["document_id"] == document.id
                ],
            },
        }

    async def workflow_shared_memory_namespaces(
            self,
            workflow_id: str,
            *,
            namespace_id: str | None = None,
            limit: int | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        items = self._workflow_shared_memory_namespaces(workflow)
        if namespace_id:
            items = [item for item in items if item["id"] == namespace_id]
        items = items[-self._bounded_limit(limit, default=20, maximum=100):]
        return {
            "workflow_id": workflow.id,
            "shared_memory": self._workflow_shared_memory_config_payload(workflow),
            "items": items,
            "count": len(items),
        }

    async def create_workflow_shared_memory_namespace(self, workflow_id: str, payload: dict[str, Any]) -> dict[
        str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        metadata = dict(workflow.metadata)
        shared_memory = dict(metadata.get(WORKFLOW_SHARED_MEMORY_METADATA_KEY) or {})
        namespaces = self._raw_history_list(shared_memory.get("namespaces"))
        entry = self._new_shared_memory_namespace_entry(workflow, payload)
        namespaces.append(entry)
        shared_memory["enabled"] = shared_memory.get("enabled", True) is not False
        shared_memory["namespaces"] = namespaces[-50:]
        shared_memory["last_namespace_id"] = entry["id"]
        metadata[WORKFLOW_SHARED_MEMORY_METADATA_KEY] = shared_memory
        updated = await self.context.workflow_repo.update(workflow.id, {"metadata": metadata})
        assert updated is not None
        return {
            "workflow": updated.model_dump(mode="json"),
            "namespace": self._workflow_shared_memory_namespace_by_id(updated, entry["id"]),
        }

    async def update_workflow_shared_memory_namespace(
            self,
            workflow_id: str,
            namespace_id: str,
            patch: dict[str, Any],
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        metadata = dict(workflow.metadata)
        shared_memory = dict(metadata.get(WORKFLOW_SHARED_MEMORY_METADATA_KEY) or {})
        namespaces = self._raw_history_list(shared_memory.get("namespaces"))
        target_index = self._find_governance_item_index(namespaces, namespace_id, id_keys=("id",))
        if target_index is None:
            raise ValueError(f"Shared memory namespace '{namespace_id}' was not found for workflow '{workflow_id}'.")
        namespaces[target_index] = self._apply_shared_memory_namespace_patch(namespaces[target_index], patch)
        shared_memory["namespaces"] = namespaces[-50:]
        metadata[WORKFLOW_SHARED_MEMORY_METADATA_KEY] = shared_memory
        updated = await self.context.workflow_repo.update(workflow.id, {"metadata": metadata})
        assert updated is not None
        return {
            "workflow": updated.model_dump(mode="json"),
            "namespace": self._workflow_shared_memory_namespace_by_id(updated, namespace_id),
        }

    async def delete_workflow_shared_memory_namespace(self, workflow_id: str, namespace_id: str) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        metadata = dict(workflow.metadata)
        shared_memory = dict(metadata.get(WORKFLOW_SHARED_MEMORY_METADATA_KEY) or {})
        namespaces = self._raw_history_list(shared_memory.get("namespaces"))
        remaining = [item for item in namespaces if self._normalized_optional_string(item.get("id")) != namespace_id]
        deleted = len(remaining) != len(namespaces)
        if deleted:
            shared_memory["namespaces"] = remaining
            metadata[WORKFLOW_SHARED_MEMORY_METADATA_KEY] = shared_memory
            updated = await self.context.workflow_repo.update(workflow.id, {"metadata": metadata})
            assert updated is not None
        else:
            updated = workflow
        return {
            "workflow": updated.model_dump(mode="json"),
            "deleted": deleted,
            "namespace_id": namespace_id,
            "items": self._workflow_shared_memory_namespaces(updated),
        }

    async def workflow_shared_memory_namespace_memories(
            self,
            workflow_id: str,
            namespace_id: str,
            *,
            current_user,
            limit: int | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        namespace = self._workflow_shared_memory_namespace_by_id(workflow, namespace_id)
        memory_ids = namespace.get("memory_ids") if isinstance(namespace.get("memory_ids"), list) else []
        service = MemoryService(self.context)
        items: list[dict[str, Any]] = []
        for memory_id in memory_ids:
            memory = await self.context.memory_repo.get(memory_id)
            if memory is None or not await service.can_read(memory, current_user=current_user):
                continue
            items.append(memory.model_dump(mode="json"))
        bounded_limit = self._bounded_limit(limit, default=50, maximum=200)
        return {
            "workflow_id": workflow.id,
            "namespace": namespace,
            "items": items[:bounded_limit],
            "count": min(len(items), bounded_limit),
            "total_count": len(items),
        }

    async def add_workflow_shared_memory_namespace_memory(
            self,
            workflow_id: str,
            namespace_id: str,
            memory_id: str,
            *,
            current_user,
            trusted_actor: bool = False,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        metadata = dict(workflow.metadata)
        shared_memory = dict(metadata.get(WORKFLOW_SHARED_MEMORY_METADATA_KEY) or {})
        namespaces = self._raw_history_list(shared_memory.get("namespaces"))
        target_index = self._find_governance_item_index(namespaces, namespace_id, id_keys=("id",))
        if target_index is None:
            raise ValueError(f"Shared memory namespace '{namespace_id}' was not found for workflow '{workflow_id}'.")
        service = MemoryService(self.context)
        memory = await self.context.memory_repo.get(memory_id)
        if memory is None or not await service.can_read(memory, current_user=current_user):
            raise ValueError(f"Memory '{memory_id}' was not found.")
        memory_metadata = dict(memory.metadata)
        namespace_memberships = self._string_list(memory_metadata.get("shared_memory_namespaces"))
        if namespace_id not in namespace_memberships:
            namespace_memberships.append(namespace_id)
        memory_metadata["shared_memory_namespaces"] = sorted(set(namespace_memberships))
        updated_memory = await service.update_memory(
            memory.id,
            {"metadata": memory_metadata},
            confirmed=True,
            current_user=current_user,
            trusted_actor=trusted_actor,
        )
        if updated_memory is None:
            raise ValueError(f"Memory '{memory_id}' was not found.")
        namespace = dict(namespaces[target_index])
        namespace_memory_ids = namespace.get("memory_ids") if isinstance(namespace.get("memory_ids"), list) else []
        namespace["memory_ids"] = [*namespace_memory_ids,
                                   memory.id] if memory.id not in namespace_memory_ids else namespace_memory_ids
        namespace["updated_at"] = utc_now().isoformat()
        namespace["updated_by"] = getattr(current_user, "id", None)
        namespaces[target_index] = namespace
        shared_memory["namespaces"] = namespaces
        metadata[WORKFLOW_SHARED_MEMORY_METADATA_KEY] = shared_memory
        updated_workflow = await self.context.workflow_repo.update(workflow.id, {"metadata": metadata})
        assert updated_workflow is not None
        return {
            "workflow": updated_workflow.model_dump(mode="json"),
            "namespace": self._workflow_shared_memory_namespace_by_id(updated_workflow, namespace_id),
            "memory": updated_memory.model_dump(mode="json"),
        }

    async def remove_workflow_shared_memory_namespace_memory(
            self,
            workflow_id: str,
            namespace_id: str,
            memory_id: str,
            *,
            current_user,
            trusted_actor: bool = False,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        metadata = dict(workflow.metadata)
        shared_memory = dict(metadata.get(WORKFLOW_SHARED_MEMORY_METADATA_KEY) or {})
        namespaces = self._raw_history_list(shared_memory.get("namespaces"))
        target_index = self._find_governance_item_index(namespaces, namespace_id, id_keys=("id",))
        if target_index is None:
            raise ValueError(f"Shared memory namespace '{namespace_id}' was not found for workflow '{workflow_id}'.")
        service = MemoryService(self.context)
        memory = await self.context.memory_repo.get(memory_id)
        updated_memory_payload = None
        if memory is not None and await service.can_read(memory, current_user=current_user):
            memory_metadata = dict(memory.metadata)
            namespace_memberships = self._string_list(memory_metadata.get("shared_memory_namespaces"))
            if namespace_id in namespace_memberships:
                memory_metadata["shared_memory_namespaces"] = [item for item in namespace_memberships if
                                                               item != namespace_id]
                updated_memory = await service.update_memory(
                    memory.id,
                    {"metadata": memory_metadata},
                    confirmed=True,
                    current_user=current_user,
                    trusted_actor=trusted_actor,
                )
                updated_memory_payload = updated_memory.model_dump(mode="json") if updated_memory is not None else None
        namespace = dict(namespaces[target_index])
        namespace_memory_ids = namespace.get("memory_ids") if isinstance(namespace.get("memory_ids"), list) else []
        deleted = memory_id in namespace_memory_ids
        namespace["memory_ids"] = [item for item in namespace_memory_ids if item != memory_id]
        namespace["updated_at"] = utc_now().isoformat()
        namespace["updated_by"] = getattr(current_user, "id", None)
        namespaces[target_index] = namespace
        shared_memory["namespaces"] = namespaces
        metadata[WORKFLOW_SHARED_MEMORY_METADATA_KEY] = shared_memory
        updated_workflow = await self.context.workflow_repo.update(workflow.id, {"metadata": metadata})
        assert updated_workflow is not None
        return {
            "workflow": updated_workflow.model_dump(mode="json"),
            "namespace": self._workflow_shared_memory_namespace_by_id(updated_workflow, namespace_id),
            "deleted": deleted,
            "memory_id": memory_id,
            "memory": updated_memory_payload,
        }

    async def request_workflow_improvement_proposal_approval(
            self,
            workflow_id: str,
            proposal_id: str,
            *,
            actor_user_id: str,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        proposal = self._workflow_improvement_proposal_by_id(workflow, proposal_id)
        existing_id = self._normalized_optional_string(proposal.get("approval_request_id"))
        if existing_id:
            existing = await self.context.conversation_approval_repo.get(existing_id)
            if existing is not None:
                return {
                    "workflow_id": workflow.id,
                    "proposal": proposal,
                    "approval_request": existing.model_dump(mode="json"),
                    "created": False,
                }
        conversation = await self._ensure_monitor_dispatch_conversation(workflow, actor_user_id=actor_user_id)
        origin = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation.id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.SYSTEM_NOTE,
                plain_text=f"Approval requested for workflow improvement proposal '{proposal.get('title') or proposal_id}'.",
                content={"source": "workflow_service", "proposal_id": proposal_id, "workflow_id": workflow.id},
                metadata={"source": "workflow_service", "proposal_kind": "workflow_improvement",
                          "proposal_id": proposal_id},
            )
        )
        approval = ApprovalRequest(
            approval_type=ApprovalType.WORKFLOW_UPDATE,
            target_type=ApprovalTargetType.WORKFLOW,
            target_id=workflow.id,
            requested_by_agent_id="main_agent",
            conversation_id=conversation.id,
            origin_message_id=origin.id,
            summary=str(proposal.get("title") or f"Workflow improvement proposal {proposal_id}"),
            diff_summary=str(proposal.get("summary") or "Workflow improvement proposal."),
            proposed_payload={
                "workflow_id": workflow.id,
                "proposal_id": proposal_id,
                "proposed_change": proposal.get("proposed_change") if isinstance(proposal.get("proposed_change"),
                                                                                 dict) else {},
                "diagnosis": proposal.get("diagnosis") if isinstance(proposal.get("diagnosis"), dict) else {},
                "risk": proposal.get("risk"),
                "validation_plan": proposal.get("validation_plan"),
                "rollback_plan": proposal.get("rollback_plan"),
            },
            metadata={
                "action": "workflow_update",
                "source": "workflow_service",
                "proposal_kind": "workflow_improvement",
                "workflow_id": workflow.id,
                "proposal_id": proposal_id,
            },
        )
        created = await self.context.conversation_approval_repo.create(approval)
        message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation.id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.WORKFLOW_UPDATE_PROPOSAL,
                plain_text=approval.summary,
                approval_request_id=created.id,
                content={
                    "approval_request_id": created.id,
                    "approval_type": created.approval_type.value,
                    "summary": created.summary,
                    "diff_summary": created.diff_summary,
                    "status": created.status.value,
                    "workflow": {"id": workflow.id, "name": workflow.name},
                    "source": "workflow_service",
                },
                metadata={"source": "workflow_service", "proposal_id": proposal_id},
            )
        )
        from app.services.conversations.core import ConversationService

        await ConversationService(self.context).publish_approval_requested(conversation.id,
                                                                           created.model_dump(mode="json"))
        updated = await self.update_workflow_improvement_proposal(
            workflow.id,
            proposal_id,
            {"approval_request_id": created.id, "status": "approval_requested"},
        )
        return {
            "workflow_id": workflow.id,
            "conversation_id": conversation.id,
            "origin_message": origin.model_dump(mode="json"),
            "message": message.model_dump(mode="json"),
            "approval_request": created.model_dump(mode="json"),
            "proposal": updated["proposal"],
            "created": True,
        }

    async def request_workflow_steering_approval(
            self,
            workflow_id: str,
            approval_id: str,
            *,
            actor_user_id: str,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        steering = self._workflow_steering_approval_by_id(workflow, approval_id)
        existing_id = self._normalized_optional_string(steering.get("approval_request_id"))
        if existing_id:
            existing = await self.context.conversation_approval_repo.get(existing_id)
            if existing is not None:
                return {
                    "workflow_id": workflow.id,
                    "approval": steering,
                    "approval_request": existing.model_dump(mode="json"),
                    "created": False,
                }
        conversation = await self._ensure_monitor_dispatch_conversation(workflow, actor_user_id=actor_user_id)
        origin = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation.id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.SYSTEM_NOTE,
                plain_text=f"Approval requested for supervisor steering '{steering.get('recommended_action') or approval_id}'.",
                content={"source": "workflow_service", "approval_id": approval_id, "workflow_id": workflow.id},
                metadata={"source": "workflow_service", "proposal_kind": "supervisor_steering",
                          "approval_id": approval_id},
            )
        )
        requested_action = str(steering.get("recommended_action") or "review")
        approval = ApprovalRequest(
            approval_type=ApprovalType.OTHER,
            target_type=ApprovalTargetType.WORKFLOW,
            target_id=workflow.id,
            requested_by_agent_id="main_agent",
            conversation_id=conversation.id,
            origin_message_id=origin.id,
            summary=str(steering.get("title") or f"Supervisor steering {requested_action}"),
            diff_summary=str(steering.get("reason") or "Supervisor steering approval request."),
            proposed_payload={
                "workflow_id": workflow.id,
                "approval_id": approval_id,
                "execution_id": steering.get("execution_id"),
                "recommended_action": requested_action,
                "reason": steering.get("reason"),
                "evidence": steering.get("evidence") if isinstance(steering.get("evidence"), dict) else {},
                "policy": steering.get("policy") if isinstance(steering.get("policy"), dict) else {},
                "operator_steering_parameters": steering.get("operator_parameters") if isinstance(
                    steering.get("operator_parameters"), dict) else {},
            },
            metadata={
                "action": "supervisor_steering",
                "source": "workflow_service",
                "proposal_kind": "supervisor_steering",
                "workflow_id": workflow.id,
                "approval_id": approval_id,
                "recommended_action": requested_action,
                "reason": steering.get("reason"),
            },
        )
        created = await self.context.conversation_approval_repo.create(approval)
        message = await self.context.conversation_message_repo.create(
            ConversationMessage(
                conversation_id=conversation.id,
                role=ConversationRole.ASSISTANT,
                message_type=ConversationMessageType.APPROVAL_REQUEST,
                plain_text=approval.summary,
                approval_request_id=created.id,
                content={
                    "approval_request_id": created.id,
                    "approval_type": created.approval_type.value,
                    "summary": created.summary,
                    "diff_summary": created.diff_summary,
                    "status": created.status.value,
                    "workflow": {"id": workflow.id, "name": workflow.name},
                    "recommended_action": requested_action,
                    "source": "workflow_service",
                },
                metadata={"source": "workflow_service", "approval_id": approval_id},
            )
        )
        from app.services.conversations.core import ConversationService

        await ConversationService(self.context).publish_approval_requested(conversation.id,
                                                                           created.model_dump(mode="json"))
        updated = await self.update_workflow_steering_approval(
            workflow.id,
            approval_id,
            {"approval_request_id": created.id, "status": "approval_requested"},
        )
        return {
            "workflow_id": workflow.id,
            "conversation_id": conversation.id,
            "workflow": updated["workflow"],
            "origin_message": origin.model_dump(mode="json"),
            "message": message.model_dump(mode="json"),
            "approval_request": created.model_dump(mode="json"),
            "approval": updated["approval"],
            "created": True,
        }

    async def sync_governance_record_from_approval(self, approval: ApprovalRequest) -> dict[str, Any] | None:
        workflow_id = self._normalized_optional_string(approval.metadata.get("workflow_id")) or approval.target_id
        if not workflow_id:
            return None
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            return None
        proposal_id = self._normalized_optional_string(approval.metadata.get("proposal_id"))
        steering_approval_id = self._normalized_optional_string(approval.metadata.get("approval_id"))
        decision_patch = self._governance_patch_from_approval(approval)

        if proposal_id:
            updated = await self.update_workflow_improvement_proposal(workflow_id, proposal_id, decision_patch)
            return {"kind": "improvement_proposal", "workflow_id": workflow_id, "record": updated["proposal"]}
        if steering_approval_id:
            updated = await self.update_workflow_steering_approval(workflow_id, steering_approval_id, decision_patch)
            return {"kind": "steering_approval", "workflow_id": workflow_id, "record": updated["approval"]}
        return None

    async def dispatch_monitor_proposal_to_main_agent(
            self,
            workflow_id: str,
            proposal_event_id: str,
            *,
            actor_user_id: str,
            operator_note: str | None = None,
    ) -> dict[str, Any]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")

        proposal_event = await self._monitor_proposal_event(workflow_id, proposal_event_id)
        if proposal_event is None:
            raise ValueError(f"Monitor proposal '{proposal_event_id}' was not found for workflow '{workflow_id}'.")

        conversation = await self._ensure_monitor_dispatch_conversation(
            workflow,
            actor_user_id=actor_user_id,
        )
        conversation_id = conversation.id

        payload = proposal_event.payload if isinstance(proposal_event.payload, dict) else {}
        cleaned_operator_note = operator_note.strip() if isinstance(operator_note,
                                                                    str) and operator_note.strip() else None
        message = ConversationMessage(
            conversation_id=conversation_id,
            role=ConversationRole.USER,
            message_type=ConversationMessageType.USER_TEXT,
            plain_text=self._monitor_proposal_dispatch_text(
                workflow,
                proposal_event,
                operator_note=cleaned_operator_note,
            ),
            content={
                "text": self._monitor_proposal_dispatch_text(
                    workflow,
                    proposal_event,
                    operator_note=cleaned_operator_note,
                )
            },
            metadata={
                # Keep the workflow selection explicit so the main-agent workflow tools resolve the right target.
                "source": "main_agent_monitor",
                "monitor_action": "dispatch_proposal_to_main_agent",
                "monitor_proposal_event_id": proposal_event.id,
                "requested_by_user_id": actor_user_id,
                "operator_note": cleaned_operator_note,
                "page_context": {
                    "surface": "workflow.detail",
                    "route": f"/workflows/{workflow.id}",
                    "selection": {"workflowId": workflow.id},
                    "entities": [
                        {
                            "type": "workflow",
                            "id": workflow.id,
                            "label": workflow.name or workflow.id,
                        }
                    ],
                    "allowedActions": [
                        "workflow.inspect",
                        "workflow.propose_update",
                        "workflow.apply_update",
                    ],
                },
                "assistant_providers": {
                    "version": "2026-05-27",
                    "providers": [
                        {
                            "id": "workflow.provider",
                            "label": "Workflow provider",
                            "systemToolIds": [
                                "agency.workflow.get",
                                "agency.workflow.propose-update",
                            ],
                            "selection": {"workflowId": workflow.id},
                        }
                    ],
                },
                # Store the advisory payload on the message so operators can trace what was dispatched.
                "monitor_proposal": payload,
            },
        )

        from app.services.conversations.core import ConversationService

        result = await ConversationService(self.context).post_message(
            conversation_id,
            {
                "message": message.model_dump(mode="json"),
                "response_mode": "async",
            },
        )
        return {
            "workflow_id": workflow.id,
            "proposal_event_id": proposal_event.id,
            "conversation_id": conversation_id,
            "conversation": conversation.model_dump(mode="json"),
            **result,
        }

    async def main_agent_monitor_command_center(self) -> dict[str, Any]:
        settings = get_settings()
        workflows = await self.context.workflow_repo.list()
        main_agent_default_workflow_id = await self.main_agent_default_workflow_id()
        workflow_summaries = [
            {
                "workflow": {
                    "id": workflow.id,
                    "name": workflow.name,
                    "description": workflow.description,
                    "versioning": workflow.versioning.model_dump(mode="json"),
                },
                "monitoring": self.monitoring_operator_payload(
                    workflow,
                    main_agent_default_workflow_id=main_agent_default_workflow_id,
                ),
            }
            for workflow in workflows
        ]
        monitored_workflows = [item for item in workflow_summaries if item["monitoring"]["enabled"]]
        exempt_workflows = [item for item in workflow_summaries if item["monitoring"]["exempted"]]
        strict_workflows = [item for item in workflow_summaries if item["monitoring"]["level"] == "strict"]

        approvals = [
            item
            for item in await self.context.conversation_approval_repo.list()
            if item.metadata.get("source") == "main_agent_monitor"
        ]
        pending_approvals = [item for item in approvals if item.status == ApprovalStatus.PENDING]
        repo_write_requests = [
            item
            for item in pending_approvals
            if self._approval_repo_write_permission(item) is not None
        ]
        active_profile = await self._active_main_agent_profile()
        profile_monitoring = (
            active_profile.metadata.get("main_agent_monitoring")
            if active_profile is not None and isinstance(active_profile.metadata, dict)
            else None
        )
        profile_monitoring = profile_monitoring if isinstance(profile_monitoring, dict) else {}
        approval_conversation_id = profile_monitoring.get("approval_conversation_id")
        approval_conversation = (
            await self.context.conversation_repo.get(approval_conversation_id)
            if isinstance(approval_conversation_id, str) and approval_conversation_id
            else None
        )
        recent_events = await self._recent_main_agent_monitor_events(workflows)
        operations = self.context.runtime_operations.snapshot_dict()
        monitor_actions = [
            item
            for item in operations.get("recent_actions", [])
            if str(item.get("action", "")).startswith("main_agent_monitor.")
        ]
        last_tick = next(
            (
                item
                for item in reversed(monitor_actions)
                if item.get("action") in {
                "main_agent_monitor.tick",
                "main_agent_monitor.tick_failed",
            }
            ),
            None,
        )
        return {
            "settings": {
                "enabled": settings.main_agent_workflow_monitor_enabled,
                "default_enabled": settings.main_agent_workflow_monitor_default_enabled,
                "interval_seconds": settings.main_agent_workflow_monitor_interval_seconds,
                "stale_after_seconds": settings.main_agent_workflow_monitor_stale_after_seconds,
                "terminal_lookback_seconds": settings.main_agent_workflow_monitor_terminal_lookback_seconds,
                "finding_retention_days": settings.main_agent_workflow_monitor_finding_retention_days,
            },
            "runtime": {
                "configured": settings.main_agent_workflow_monitor_enabled,
                "last_tick": last_tick,
                "counters": {
                    key: value
                    for key, value in operations.get("counters", {}).items()
                    if str(key).startswith("main_agent_monitor.")
                       or str(key).startswith("action.main_agent_monitor.")
                },
                "recent_actions": monitor_actions[-20:],
            },
            "active_profile": active_profile.model_dump(mode="json") if active_profile is not None else None,
            "notification_route": {
                "approval_conversation_id": approval_conversation_id,
                "notification_routes": profile_monitoring.get("notification_routes") or ["conversation"],
                "route_improvement_proposals_to_approval": (
                        profile_monitoring.get("route_improvement_proposals_to_approval") is not False
                ),
                "route_steering_requests_to_approval": (
                        profile_monitoring.get("route_steering_requests_to_approval") is not False
                ),
                "monitor_delivery": (
                    approval_conversation.metadata.get("monitor_delivery")
                    if approval_conversation is not None and isinstance(approval_conversation.metadata, dict)
                    else None
                ),
                "conversation": approval_conversation.model_dump(
                    mode="json") if approval_conversation is not None else None,
            },
            "summary": {
                "workflow_count": len(workflows),
                "monitored_workflow_count": len(monitored_workflows),
                "exempt_workflow_count": len(exempt_workflows),
                "strict_workflow_count": len(strict_workflows),
                "pending_approval_count": len(pending_approvals),
                "pending_repo_write_request_count": len(repo_write_requests),
                "recent_finding_count": len(recent_events["findings"]),
                "recent_proposal_count": len(recent_events["proposals"]),
                "recent_steering_request_count": len(recent_events["steering_requests"]),
            },
            "workflows": workflow_summaries,
            "pending_approvals": [item.model_dump(mode="json") for item in pending_approvals],
            "repo_write_requests": [
                {
                    **item.model_dump(mode="json"),
                    "repo_write_permission": self._approval_repo_write_permission(item),
                }
                for item in repo_write_requests
            ],
            **recent_events,
            "operator_actions": {
                "update_routes": "/main-agent/monitor/routes",
                "workflow_monitoring_events": "/workflows/{workflow_id}/monitoring/events",
            },
        }

    async def update_main_agent_monitor_routes(self, patch: dict[str, Any]) -> dict[str, Any]:
        profile = await self._active_main_agent_profile()
        if profile is None:
            raise ValueError("Main-agent setup has not been completed.")
        metadata = dict(profile.metadata)
        monitoring = dict(metadata.get("main_agent_monitoring") or {})
        conversation_id = patch.get("approval_conversation_id")
        if conversation_id is not None:
            if not isinstance(conversation_id, str) or not conversation_id.strip():
                raise ValueError("approval_conversation_id must be a non-empty string")
            conversation_id = conversation_id.strip()
            if await self.context.conversation_repo.get(conversation_id) is None:
                raise ValueError(f"Conversation '{conversation_id}' was not found")
            monitoring["approval_conversation_id"] = conversation_id

        for key in (
                "route_improvement_proposals_to_approval",
                "route_steering_requests_to_approval",
        ):
            if key in patch:
                monitoring[key] = bool(patch[key])
        if "notification_routes" in patch:
            routes = patch["notification_routes"]
            if not isinstance(routes, list) or not all(isinstance(item, str) and item for item in routes):
                raise ValueError("notification_routes must be a list of strings")
            monitoring["notification_routes"] = sorted(set(routes))
        metadata["main_agent_monitoring"] = monitoring
        updated = await self.context.main_agent_profile_repo.update(profile.id, {"metadata": metadata})
        if updated is None:
            raise ValueError("Main-agent profile could not be updated")

        delivery_patch = patch.get("monitor_delivery")
        target_conversation_id = monitoring.get("approval_conversation_id")
        if delivery_patch is not None:
            if not isinstance(delivery_patch, dict):
                raise ValueError("monitor_delivery must be an object")
            if not isinstance(target_conversation_id, str) or not target_conversation_id:
                raise ValueError("Set approval_conversation_id before configuring monitor_delivery")
            conversation = await self.context.conversation_repo.get(target_conversation_id)
            if conversation is None:
                raise ValueError(f"Conversation '{target_conversation_id}' was not found")
            provider = str(delivery_patch.get("provider") or conversation.channel_type.value).strip()
            if provider not in chat_channel_types():
                raise ValueError("monitor_delivery.provider must be one of the supported chat channels")
            credential_id = delivery_patch.get("credential_id")
            if not isinstance(credential_id, str) or not credential_id.strip():
                raise ValueError("monitor_delivery.credential_id must be a non-empty string")
            conversation_metadata = dict(conversation.metadata)
            conversation_metadata["monitor_delivery"] = {
                "provider": provider,
                "credential_id": credential_id.strip(),
            }
            await self.context.conversation_repo.update(conversation.id, {"metadata": conversation_metadata})
        return await self.main_agent_monitor_command_center()

    async def _active_main_agent_profile(self):
        profiles = await self.context.main_agent_profile_repo.list()
        enabled = [profile for profile in profiles if getattr(profile, "enabled", True)]
        return enabled[0] if enabled else (profiles[0] if profiles else None)

    def _workflow_monitoring_metadata(self, workflow: WorkflowDefinition) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata = dict(workflow.metadata)
        monitoring = dict(metadata.get("main_agent_monitoring") or {})
        return metadata, monitoring

    def _workflow_improvement_proposal_history(self, workflow: WorkflowDefinition) -> list[dict[str, Any]]:
        _, monitoring = self._workflow_monitoring_metadata(workflow)
        history = self._raw_history_list(monitoring.get("improvement_proposals"))
        return [
            self._normalize_improvement_proposal_record(workflow, item, index=index)
            for index, item in enumerate(history)
        ]

    def _workflow_improvement_proposal_by_id(
            self,
            workflow: WorkflowDefinition,
            proposal_id: str,
    ) -> dict[str, Any]:
        for item in self._workflow_improvement_proposal_history(workflow):
            if item["id"] == proposal_id:
                return item
        raise ValueError(f"Improvement proposal '{proposal_id}' was not found for workflow '{workflow.id}'.")

    def _workflow_steering_approval_history(self, workflow: WorkflowDefinition) -> list[dict[str, Any]]:
        _, monitoring = self._workflow_monitoring_metadata(workflow)
        history = self._raw_history_list(monitoring.get("steering_approvals"))
        return [
            self._normalize_steering_approval_record(workflow, item, index=index)
            for index, item in enumerate(history)
        ]

    def _workflow_steering_approval_by_id(
            self,
            workflow: WorkflowDefinition,
            approval_id: str,
    ) -> dict[str, Any]:
        for item in self._workflow_steering_approval_history(workflow):
            if item["id"] == approval_id:
                return item
        raise ValueError(f"Steering approval '{approval_id}' was not found for workflow '{workflow.id}'.")

    def _workflow_document_link_history(self, workflow: WorkflowDefinition) -> list[dict[str, Any]]:
        metadata = dict(workflow.metadata)
        history = self._raw_history_list(metadata.get(WORKFLOW_DOCUMENT_LINK_METADATA_KEY))
        return [
            self._normalize_document_link_record(workflow, item, index=index)
            for index, item in enumerate(history)
        ]

    def _workflow_document_link_by_id(self, workflow: WorkflowDefinition, link_id: str) -> dict[str, Any]:
        for item in self._workflow_document_link_history(workflow):
            if item["id"] == link_id:
                return item
        raise ValueError(f"Workflow document link '{link_id}' was not found for workflow '{workflow.id}'.")

    def _workflow_shared_memory_namespaces(self, workflow: WorkflowDefinition) -> list[dict[str, Any]]:
        metadata = dict(workflow.metadata)
        shared_memory = dict(metadata.get(WORKFLOW_SHARED_MEMORY_METADATA_KEY) or {})
        history = self._raw_history_list(shared_memory.get("namespaces"))
        return [
            self._normalize_shared_memory_namespace_record(workflow, item, index=index)
            for index, item in enumerate(history)
        ]

    def _workflow_shared_memory_namespace_by_id(self, workflow: WorkflowDefinition, namespace_id: str) -> dict[
        str, Any]:
        for item in self._workflow_shared_memory_namespaces(workflow):
            if item["id"] == namespace_id:
                return item
        raise ValueError(f"Shared memory namespace '{namespace_id}' was not found for workflow '{workflow.id}'.")

    def _governance_record_audit(
            self,
            record_kind: str,
            item: dict[str, Any],
            approvals_by_id: dict[str, ApprovalRequest],
    ) -> dict[str, Any]:
        approval_request_id = self._normalized_optional_string(item.get("approval_request_id"))
        if not approval_request_id:
            return {
                "kind": record_kind,
                "record": item,
                "audit": {"status": "missing_approval_request", "reason": "Record has no linked approval request."},
            }
        approval = approvals_by_id.get(approval_request_id)
        if approval is None:
            return {
                "kind": record_kind,
                "record": item,
                "audit": {
                    "status": "orphaned_reference",
                    "reason": f"Approval request '{approval_request_id}' was not found.",
                    "approval_request_id": approval_request_id,
                },
            }
        record_status = str(item.get("status") or "").lower()
        approval_status = approval.status.value.lower()
        if record_status != approval_status and not (
                record_status == "approval_requested" and approval_status == "pending"):
            return {
                "kind": record_kind,
                "record": item,
                "audit": {
                    "status": "status_mismatch",
                    "reason": f"Record status '{record_status}' does not match approval status '{approval_status}'.",
                    "approval_request": self._approval_audit_payload(approval),
                },
            }
        return {
            "kind": record_kind,
            "record": item,
            "audit": {
                "status": "ok",
                "approval_request": self._approval_audit_payload(approval),
            },
        }

    async def _governance_repair_result(
            self,
            workflow_id: str,
            record_kind: str,
            action: str,
            record: dict[str, Any],
            *,
            approval_request: ApprovalRequest | None,
    ) -> dict[str, Any]:
        approvals = [
            item for item in await self.context.conversation_approval_repo.list()
            if item.target_id == workflow_id
        ]
        approvals_by_id = {item.id: item for item in approvals}
        return {
            "workflow_id": workflow_id,
            "record_kind": record_kind,
            "action": action,
            "record": record,
            "approval_request": self._approval_audit_payload(
                approval_request) if approval_request is not None else None,
            "audit": self._governance_record_audit(record_kind, record, approvals_by_id)["audit"],
        }

    async def _adoptable_orphaned_approval_actions(
            self,
            workflow_id: str,
            audit: dict[str, Any],
    ) -> list[dict[str, Any]]:
        workflow = await self.context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        actions: list[dict[str, Any]] = []
        orphaned_reference_records = {
            (
                self._normalized_optional_string(item.get("kind")) or "",
                self._normalized_optional_string((item.get("record") or {}).get("id")) or "",
            )
            for item in (list(audit.get("proposals") or []) + list(audit.get("steering_approvals") or []))
            if isinstance(item, dict)
               and isinstance(item.get("audit"), dict)
               and self._normalized_optional_string(item["audit"].get("status")) == "orphaned_reference"
        }
        already_targeted: set[tuple[str, str, str, str | None]] = set()
        for approval_payload in audit.get("orphaned_approvals") or []:
            if not isinstance(approval_payload, dict):
                continue
            approval_id = self._normalized_optional_string(approval_payload.get("id"))
            metadata = approval_payload.get("metadata")
            if not approval_id or not isinstance(metadata, dict):
                continue
            candidate = self._adoptable_governance_record_from_approval_metadata(
                workflow,
                metadata,
                orphaned_reference_records=orphaned_reference_records,
            )
            if candidate is None:
                continue
            record_kind, record_id = candidate
            key = (record_kind, record_id, "link_approval_request", approval_id)
            if key in already_targeted:
                continue
            actions.append(
                self._governance_remediation_action(
                    record_kind=record_kind,
                    record_id=record_id,
                    action="link_approval_request",
                    approval_request_id=approval_id,
                    reason="Orphaned approval metadata already identifies this workflow governance record.",
                )
            )
            already_targeted.add(key)
        return actions

    def _adoptable_governance_record_from_approval_metadata(
            self,
            workflow: WorkflowDefinition,
            metadata: dict[str, Any],
            *,
            orphaned_reference_records: set[tuple[str, str]],
    ) -> tuple[str, str] | None:
        proposal_id = self._normalized_optional_string(metadata.get("proposal_id"))
        approval_id = self._normalized_optional_string(metadata.get("approval_id"))
        if proposal_id:
            try:
                record = self._workflow_improvement_proposal_by_id(workflow, proposal_id)
            except ValueError:
                record = None
            if record is not None:
                current_link = self._normalized_optional_string(record.get("approval_request_id"))
                if not current_link or ("improvement_proposal", proposal_id) in orphaned_reference_records:
                    return "improvement_proposal", proposal_id
        if approval_id:
            try:
                record = self._workflow_steering_approval_by_id(workflow, approval_id)
            except ValueError:
                record = None
            if record is not None:
                current_link = self._normalized_optional_string(record.get("approval_request_id"))
                if not current_link or ("steering_approval", approval_id) in orphaned_reference_records:
                    return "steering_approval", approval_id
        return None

    def _governance_remediation_action(
            self,
            *,
            record_kind: str,
            record_id: str,
            action: str,
            approval_request_id: str | None = None,
            reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "record_kind": record_kind,
            "record_id": record_id,
            "action": action,
            "approval_request_id": approval_request_id,
            "reason": reason,
        }

    def _document_links_by_target(
            self,
            links: list[dict[str, Any]],
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in links:
            target_type = self._normalized_optional_string(item.get("target_type"))
            target_id = self._normalized_optional_string(item.get("target_id"))
            if not target_type:
                continue
            key = (target_type, target_id or "")
            grouped.setdefault(key, []).append(item)
        return grouped

    def _audit_for_record(
            self,
            audit_items: list[dict[str, Any]],
            *,
            record_id: str,
    ) -> dict[str, Any] | None:
        for item in audit_items:
            if self._normalized_optional_string((item.get("record") or {}).get("id")) == record_id:
                audit_payload = item.get("audit")
                if isinstance(audit_payload, dict):
                    return audit_payload
        return None

    def _governance_review_queue_item(
            self,
            *,
            record_kind: str,
            record: dict[str, Any],
            audit: dict[str, Any] | None,
            evidence_links: list[dict[str, Any]],
    ) -> dict[str, Any]:
        status = self._normalized_optional_string(record.get("status")) or "unknown"
        audit_status = self._normalized_optional_string((audit or {}).get("status")) or "ok"
        needs_approval = status in {"draft", "pending", "requested"}
        needs_evidence = len(evidence_links) == 0 and status not in self._closed_governance_statuses()
        priority = self._governance_review_priority(
            record_kind=record_kind,
            audit_status=audit_status,
            needs_approval=needs_approval,
            needs_evidence=needs_evidence,
            status=status,
        )
        next_actions = self._governance_review_next_actions(
            record_kind=record_kind,
            record=record,
            audit_status=audit_status,
            needs_approval=needs_approval,
            needs_evidence=needs_evidence,
        )
        return {
            "record_kind": record_kind,
            "record_id": record["id"],
            "title": record.get("title"),
            "status": status,
            "priority": priority,
            "audit_status": audit_status,
            "audit_reason": self._normalized_optional_string((audit or {}).get("reason")),
            "approval_request_id": self._normalized_optional_string(record.get("approval_request_id")),
            "approval_request": self._dict_payload((audit or {}).get("approval_request")),
            "evidence_link_count": len(evidence_links),
            "evidence_links": evidence_links,
            "activity": self._governance_record_activity(
                record_kind=record_kind,
                record=record,
                evidence_links=evidence_links,
                approval_request=self._dict_payload((audit or {}).get("approval_request")),
            ),
            "record": record,
            "next_actions": next_actions,
        }

    def _governance_record_activity(
            self,
            *,
            record_kind: str,
            record: dict[str, Any],
            evidence_links: list[dict[str, Any]],
            approval_request: dict[str, Any],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        created_at = self._normalized_optional_string(record.get("created_at"))
        created_by = self._normalized_optional_string(record.get("created_by"))
        if created_at:
            events.append(
                {
                    "kind": "created",
                    "timestamp": created_at,
                    "actor": created_by,
                    "title": "Record created",
                    "summary": self._normalized_optional_string(record.get("summary"))
                               or self._normalized_optional_string(record.get("reason")),
                }
            )

        for link in evidence_links:
            linked_at = self._normalized_optional_string(link.get("linked_at"))
            if not linked_at:
                continue
            events.append(
                {
                    "kind": "evidence_attached",
                    "timestamp": linked_at,
                    "actor": self._normalized_optional_string(link.get("linked_by")),
                    "title": "Evidence linked",
                    "summary": self._normalized_optional_string(link.get("label"))
                               or self._normalized_optional_string(link.get("document_id")),
                    "document_id": self._normalized_optional_string(link.get("document_id")),
                }
            )

        approval_request_id = self._normalized_optional_string(approval_request.get("id"))
        approval_created_at = self._normalized_optional_string(approval_request.get("created_at"))
        if approval_request_id and approval_created_at:
            events.append(
                {
                    "kind": "approval_requested",
                    "timestamp": approval_created_at,
                    "actor": self._normalized_optional_string(
                        (approval_request.get("metadata") or {}).get("requested_by"))
                             or "main_agent",
                    "title": "Approval requested",
                    "summary": self._normalized_optional_string(approval_request.get("summary")) or approval_request_id,
                    "approval_request_id": approval_request_id,
                }
            )

        decision_at = self._normalized_optional_string(record.get("decision_at"))
        if decision_at:
            status = self._normalized_optional_string(record.get("status")) or "unknown"
            title_by_status = {
                "resolved": "Record resolved",
                "dismissed": "Record dismissed",
                "approved": "Approval approved",
                "rejected": "Approval rejected",
                "applied": "Change applied",
            }
            events.append(
                {
                    "kind": "decision",
                    "timestamp": decision_at,
                    "actor": self._normalized_optional_string(record.get("decided_by"))
                             or self._normalized_optional_string(record.get("approved_by"))
                             or self._normalized_optional_string(record.get("rejected_by")),
                    "title": title_by_status.get(status, "Decision recorded"),
                    "summary": self._normalized_optional_string(record.get("decision_reason")),
                }
            )

        updated_at = self._normalized_optional_string(record.get("updated_at"))
        if updated_at and updated_at != created_at and not decision_at and not approval_request_id:
            events.append(
                {
                    "kind": "updated",
                    "timestamp": updated_at,
                    "actor": None,
                    "title": "Record updated",
                    "summary": f"{record_kind.replace('_', ' ')} metadata changed.",
                }
            )

        events.sort(
            key=lambda item: (
                self._normalized_optional_string(item.get("timestamp")) or "",
                self._normalized_optional_string(item.get("kind")) or "",
            ),
            reverse=True,
        )
        return events[:12]

    def _governance_review_priority(
            self,
            *,
            record_kind: str,
            audit_status: str,
            needs_approval: bool,
            needs_evidence: bool,
            status: str,
    ) -> str:
        if record_kind == "improvement_proposal" and audit_status == "missing_approval_request" and needs_approval:
            return "approval"
        if audit_status in {"status_mismatch", "orphaned_reference", "missing_approval_request"}:
            return "repair"
        if needs_approval:
            return "approval"
        if needs_evidence:
            return "evidence"
        if status in self._closed_governance_statuses():
            return "resolved"
        return "review"

    def _governance_review_next_actions(
            self,
            *,
            record_kind: str,
            record: dict[str, Any],
            audit_status: str,
            needs_approval: bool,
            needs_evidence: bool,
    ) -> list[str]:
        status = self._normalized_optional_string(record.get("status")) or "unknown"
        if status in {"resolved", "dismissed"}:
            return ["reopen"]
        actions: list[str] = []
        if audit_status == "missing_approval_request":
            if record_kind == "improvement_proposal":
                actions.append("request_approval")
            else:
                actions.append("request_approval")
        elif audit_status in {"status_mismatch", "orphaned_reference"}:
            actions.append("repair_governance_link")
        if needs_evidence:
            actions.append("attach_evidence")
        if needs_approval and "request_approval" not in actions:
            actions.append("review_for_approval")
        if not actions:
            actions.append("monitor")
        return actions

    def _governance_queue_sort_key(self, item: dict[str, Any]) -> tuple[int, str, str]:
        priority_order = {
            "repair": 0,
            "approval": 1,
            "evidence": 2,
            "review": 3,
            "resolved": 4,
        }
        return (
            priority_order.get(self._normalized_optional_string(item.get("priority")) or "review", 9),
            self._normalized_optional_string(item.get("status")) or "",
            self._normalized_optional_string(item.get("record_id")) or "",
        )

    def _governance_review_recommendations(
            self,
            *,
            actionable_items: list[dict[str, Any]],
            orphaned_approvals: list[dict[str, Any]],
            remediation_preview: dict[str, Any],
    ) -> list[dict[str, Any]]:
        recommendations: list[dict[str, Any]] = []
        if remediation_preview["summary"]["planned_action_count"] > 0:
            recommendations.append(
                {
                    "action": "preview_or_apply_remediation",
                    "reason": "Deterministic governance repairs are available from current audit state.",
                    "count": remediation_preview["summary"]["planned_action_count"],
                }
            )
        approval_items = [item for item in actionable_items if item.get("priority") == "approval"]
        if approval_items:
            recommendations.append(
                {
                    "action": "route_pending_records_to_approval",
                    "reason": "Some governance records are waiting for explicit approval routing or operator review.",
                    "count": len(approval_items),
                }
            )
        evidence_items = [
            item
            for item in actionable_items
            if item.get("priority") == "evidence" or "attach_evidence" in (item.get("next_actions") or [])
        ]
        if evidence_items:
            recommendations.append(
                {
                    "action": "attach_workflow_evidence",
                    "reason": "Some governance records have no linked uploaded documents or evidence summary. Use document suggestions before attaching evidence when needed.",
                    "count": len(evidence_items),
                }
            )
        if orphaned_approvals:
            recommendations.append(
                {
                    "action": "inspect_orphaned_approvals",
                    "reason": "Some approval requests exist without an attached governance history record.",
                    "count": len(orphaned_approvals),
                }
            )
        return recommendations

    def _governance_document_suggestion(
            self,
            *,
            record_kind: str,
            record: dict[str, Any],
            document: Any,
            links: list[dict[str, Any]],
    ) -> dict[str, Any]:
        metadata = document.metadata if isinstance(document.metadata, dict) else {}
        upload_intelligence = (
            metadata.get("upload_intelligence")
            if isinstance(metadata.get("upload_intelligence"), dict)
            else {}
        )
        summary = (
                self._normalized_optional_string(upload_intelligence.get("summary"))
                or self._first_nonempty_line(document.extracted_text or "")
        )
        tags = self._nested_string_list(upload_intelligence, "recommended", "tags")
        document_text = " ".join(
            part for part in [document.filename, summary, document.extracted_text or "", " ".join(tags)] if part
        ).lower()
        record_terms = self._governance_record_search_terms(record_kind=record_kind, record=record)
        matched_terms = [term for term in record_terms if term in document_text]
        score = len(matched_terms) * 3
        linked_to_record = False
        linked_to_workflow = False
        for link in links:
            if link["document_id"] != document.id:
                continue
            if link["target_type"] == record_kind and link.get("target_id") == record["id"]:
                linked_to_record = True
            if link["target_type"] == "workflow":
                linked_to_workflow = True
        if linked_to_record:
            score += 6
        if linked_to_workflow:
            score += 2
        if self._normalized_optional_string(upload_intelligence.get("document_kind")) in {"policy_sop", "runbook",
                                                                                          "spec"}:
            score += 1
        return {
            "document": document.model_dump(mode="json"),
            "score": score,
            "matched_terms": matched_terms,
            "summary": {
                "headline": summary or document.filename,
                "document_kind": self._normalized_optional_string(upload_intelligence.get("document_kind")),
                "tags": tags,
                "linked_to_record": linked_to_record,
                "linked_to_workflow": linked_to_workflow,
            },
            "reason": self._governance_document_suggestion_reason(
                matched_terms=matched_terms,
                linked_to_record=linked_to_record,
                linked_to_workflow=linked_to_workflow,
            ),
        }

    def _governance_record_search_terms(
            self,
            *,
            record_kind: str,
            record: dict[str, Any],
    ) -> list[str]:
        values = [
            self._normalized_optional_string(record.get("title")) or "",
            self._normalized_optional_string(record.get("summary")) or "",
            self._normalized_optional_string(record.get("reason")) or "",
            self._normalized_optional_string(record.get("recommended_action")) or "",
            self._normalized_optional_string(record.get("expected_benefit")) or "",
            self._normalized_optional_string(record.get("risk")) or "",
            self._normalized_optional_string(record.get("validation_plan")) or "",
            " ".join(self._string_list(record.get("tags"))),
            record_kind.replace("_", " "),
        ]
        tokens: list[str] = []
        seen: set[str] = set()
        for value in values:
            for token in re.findall(r"[a-z0-9_-]{4,}", value.lower()):
                if token not in seen:
                    seen.add(token)
                    tokens.append(token)
        return tokens[:20]

    def _governance_document_suggestion_reason(
            self,
            *,
            matched_terms: list[str],
            linked_to_record: bool,
            linked_to_workflow: bool,
    ) -> str:
        reasons: list[str] = []
        if linked_to_record:
            reasons.append("Already linked to this governance record")
        elif linked_to_workflow:
            reasons.append("Already linked at workflow level")
        if matched_terms:
            reasons.append(f"Matched terms: {', '.join(matched_terms[:5])}")
        return "; ".join(reasons) or "Candidate document from the same workflow scope"

    async def _sync_governance_approval_metadata(
            self,
            approval: ApprovalRequest,
            *,
            workflow_id: str,
            record_kind: str,
            record_id: str,
    ) -> ApprovalRequest:
        metadata = dict(approval.metadata)
        metadata["workflow_id"] = workflow_id
        metadata["source"] = metadata.get("source") or "workflow_service"
        # Keep approval metadata aligned with the governance record so later approval
        # decisions can flow back into the correct workflow history entry automatically.
        if record_kind == "improvement_proposal":
            metadata["proposal_id"] = record_id
            metadata.pop("approval_id", None)
        else:
            metadata["approval_id"] = record_id
            metadata.pop("proposal_id", None)
        updated = await self.context.conversation_approval_repo.update(approval.id, {"metadata": metadata})
        if updated is None:
            raise ValueError(f"Approval request '{approval.id}' was not found.")
        return updated

    def _governance_patch_from_approval(self, approval: ApprovalRequest) -> dict[str, Any]:
        patch = {
            "approval_request_id": approval.id,
            "status": self._governance_record_status_from_approval(approval),
        }
        if approval.status == ApprovalStatus.PENDING:
            patch["decision_reason"] = None
            patch["decision_at"] = None
            patch["decided_by"] = None
            patch["approved_by"] = None
            patch["rejected_by"] = None
            return patch
        patch["decision_reason"] = approval.decision_reason
        patch["decision_at"] = approval.updated_at.isoformat()
        patch["decided_by"] = approval.approved_by_user_id
        patch["approved_by"] = approval.approved_by_user_id if approval.status == ApprovalStatus.APPROVED else None
        patch["rejected_by"] = approval.approved_by_user_id if approval.status == ApprovalStatus.REJECTED else None
        return patch

    def _governance_record_status_from_approval(self, approval: ApprovalRequest) -> str:
        if approval.status == ApprovalStatus.PENDING:
            return "approval_requested"
        return approval.status.value

    def _normalize_governance_queue_action(self, value: Any) -> str:
        normalized = self._normalized_optional_string(value)
        allowed = {"request_approval", "attach_evidence", "apply_remediation", "resolve", "dismiss", "reopen"}
        if normalized not in allowed:
            raise ValueError(
                f"Unsupported governance action '{value}'. Choose one of: {', '.join(sorted(allowed))}."
            )
        return normalized

    def _closed_governance_statuses(self) -> set[str]:
        return {"approved", "rejected", "applied", "resolved", "dismissed"}

    def _governance_record_transition_patch(
            self,
            *,
            record_kind: str,
            action: str,
            status: str,
            actor_user_id: str,
    ) -> dict[str, Any]:
        timestamp = utc_now().isoformat()
        default_open_status = "draft" if record_kind == "improvement_proposal" else "pending"
        if action == "reopen":
            if status not in {"resolved", "dismissed"}:
                raise ValueError("Only resolved or dismissed governance records can be reopened.")
            return {
                "status": default_open_status,
                "decision_reason": None,
                "decision_at": None,
                "decided_by": None,
                "approved_by": None,
                "rejected_by": None,
            }
        if action == "resolve":
            if status in {"resolved", "dismissed"}:
                raise ValueError("This governance record is already manually closed.")
            return {
                "status": "resolved",
                "decision_reason": "Resolved by workflow operator.",
                "decision_at": timestamp,
                "decided_by": actor_user_id,
                "approved_by": None,
                "rejected_by": None,
            }
        if action == "dismiss":
            if status in {"resolved", "dismissed"}:
                raise ValueError("This governance record is already manually closed.")
            return {
                "status": "dismissed",
                "decision_reason": "Dismissed by workflow operator.",
                "decision_at": timestamp,
                "decided_by": actor_user_id,
                "approved_by": None,
                "rejected_by": actor_user_id,
            }
        raise ValueError(f"Unsupported governance transition '{action}'.")

    def _effective_bool(self, value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        return bool(value)

    def _normalize_governance_record_kind(self, value: Any) -> str:
        normalized = self._normalized_optional_string(value)
        allowed = {"improvement_proposal", "steering_approval"}
        if normalized not in allowed:
            raise ValueError(
                f"Unsupported governance record kind '{value}'. Choose one of: {', '.join(sorted(allowed))}."
            )
        return normalized

    def _normalize_governance_repair_action(self, value: Any) -> str:
        normalized = self._normalized_optional_string(value)
        allowed = {"link_approval_request", "unlink_approval_request", "sync_status_from_approval"}
        if normalized not in allowed:
            raise ValueError(
                f"Unsupported governance repair action '{value}'. Choose one of: {', '.join(sorted(allowed))}."
            )
        return normalized

    def _workflow_governance_record_by_kind(
            self,
            workflow: WorkflowDefinition,
            record_kind: str,
            record_id: str,
    ) -> dict[str, Any]:
        if record_kind == "improvement_proposal":
            return self._workflow_improvement_proposal_by_id(workflow, record_id)
        return self._workflow_steering_approval_by_id(workflow, record_id)

    async def _update_governance_record_by_kind(
            self,
            workflow_id: str,
            record_kind: str,
            record_id: str,
            patch: dict[str, Any],
    ) -> dict[str, Any]:
        if record_kind == "improvement_proposal":
            updated = await self.update_workflow_improvement_proposal(workflow_id, record_id, patch)
            return updated["proposal"]
        updated = await self.update_workflow_steering_approval(workflow_id, record_id, patch)
        return updated["approval"]

    def _approval_audit_payload(self, approval: ApprovalRequest) -> dict[str, Any]:
        return {
            "id": approval.id,
            "status": approval.status.value,
            "approval_type": approval.approval_type.value,
            "target_type": approval.target_type.value,
            "target_id": approval.target_id,
            "conversation_id": approval.conversation_id,
            "summary": approval.summary,
            "created_at": approval.created_at.isoformat(),
            "updated_at": approval.updated_at.isoformat(),
            "metadata": dict(approval.metadata),
        }

    def _raw_history_list(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def _filter_governance_items(
            self,
            items: list[dict[str, Any]],
            *,
            item_id: str | None,
            status: str | None,
            limit: int | None,
    ) -> list[dict[str, Any]]:
        normalized_status = self._normalized_optional_string(status)
        filtered = items
        if item_id:
            filtered = [item for item in filtered if item.get("id") == item_id]
        if normalized_status:
            filtered = [item for item in filtered if str(item.get("status") or "").lower() == normalized_status]
        bounded_limit = self._bounded_limit(limit, default=20, maximum=100)
        return filtered[-bounded_limit:]

    def _new_improvement_proposal_entry(self, workflow: WorkflowDefinition, payload: dict[str, Any]) -> dict[str, Any]:
        title = self._required_string(payload.get("title"), field="title")
        summary = self._required_string(payload.get("summary"), field="summary")
        created_at = utc_now().isoformat()
        return {
            "id": str(payload.get("proposal_id") or f"proposal-{uuid4().hex[:12]}"),
            "source": "operator",
            "title": title,
            "summary": summary,
            "status": self._normalized_status(payload.get("status"), default="draft"),
            "priority": self._normalized_priority(payload.get("priority")),
            "proposal_kind": self._normalized_optional_string(payload.get("proposal_kind")) or "operator_change",
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "baseline_revision": workflow.versioning.revision,
            "expected_replacement_revision": workflow.versioning.revision + 1,
            "created_at": created_at,
            "updated_at": created_at,
            "created_by": self._normalized_optional_string(payload.get("created_by")) or "main-agent",
            "execution_id": self._normalized_optional_string(payload.get("execution_id")),
            "finding_event_id": self._normalized_optional_string(payload.get("finding_event_id")),
            "proposal_event_id": self._normalized_optional_string(payload.get("proposal_event_id")),
            "approval_request_id": self._normalized_optional_string(payload.get("approval_request_id")),
            "diagnosis": self._dict_payload(payload.get("diagnosis")),
            "proposed_change": self._dict_payload(payload.get("proposed_change")),
            "expected_benefit": self._normalized_optional_string(payload.get("expected_benefit")),
            "risk": self._normalized_optional_string(payload.get("risk")),
            "validation_plan": self._normalized_optional_string(payload.get("validation_plan")),
            "rollback_plan": self._normalized_optional_string(payload.get("rollback_plan")),
            "tags": self._string_list(payload.get("tags")),
            "metadata": self._dict_payload(payload.get("metadata")),
        }

    def _apply_improvement_proposal_patch(self, current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        updated = dict(current)
        for key, value in patch.items():
            if key in {"title", "summary"}:
                updated[key] = self._required_string(value, field=key)
            elif key == "status":
                updated[key] = self._normalized_status(value, default=str(updated.get("status") or "draft"))
            elif key == "priority":
                updated[key] = self._normalized_priority(value)
            elif key in {
                "proposal_kind",
                "created_by",
                "execution_id",
                "finding_event_id",
                "proposal_event_id",
                "approval_request_id",
                "expected_benefit",
                "risk",
                "validation_plan",
                "rollback_plan",
                "decision_reason",
                "decision_at",
                "decided_by",
                "approved_by",
                "rejected_by",
            }:
                updated[key] = self._normalized_optional_string(value)
            elif key in {"diagnosis", "proposed_change", "metadata"}:
                updated[key] = self._dict_payload(value)
            elif key == "tags":
                updated[key] = self._string_list(value)
            else:
                raise ValueError(f"Unsupported improvement proposal fields: {key}")
        updated["updated_at"] = utc_now().isoformat()
        return updated

    def _normalize_improvement_proposal_record(
            self,
            workflow: WorkflowDefinition,
            item: dict[str, Any],
            *,
            index: int,
    ) -> dict[str, Any]:
        item_id = (
                self._normalized_optional_string(item.get("id"))
                or self._normalized_optional_string(item.get("proposal_event_id"))
                or f"proposal-history-{index + 1}"
        )
        status = self._normalized_status(item.get("status"), default="proposed")
        return {
            "id": item_id,
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "title": self._normalized_optional_string(item.get("title")) or f"Improvement proposal {index + 1}",
            "summary": self._normalized_optional_string(item.get("summary"))
                       or self._normalized_optional_string(item.get("expected_benefit"))
                       or self._normalized_optional_string(item.get("risk"))
                       or "Workflow improvement proposal.",
            "status": status,
            "priority": self._normalized_priority(item.get("priority")),
            "proposal_kind": self._normalized_optional_string(item.get("proposal_kind")) or "monitor_change",
            "source": self._normalized_optional_string(item.get("source")) or "main_agent_monitor",
            "created_at": self._normalized_optional_string(item.get("created_at")),
            "updated_at": self._normalized_optional_string(item.get("updated_at"))
                          or self._normalized_optional_string(item.get("created_at")),
            "created_by": self._normalized_optional_string(item.get("created_by")),
            "baseline_revision": item.get("baseline_revision"),
            "expected_replacement_revision": item.get("expected_replacement_revision"),
            "execution_id": self._normalized_optional_string(item.get("execution_id")),
            "finding_event_id": self._normalized_optional_string(item.get("finding_event_id")),
            "proposal_event_id": self._normalized_optional_string(item.get("proposal_event_id")),
            "approval_request_id": self._normalized_optional_string(item.get("approval_request_id")),
            "decision_reason": self._normalized_optional_string(item.get("decision_reason")),
            "decision_at": self._normalized_optional_string(item.get("decision_at")),
            "decided_by": self._normalized_optional_string(item.get("decided_by")),
            "approved_by": self._normalized_optional_string(item.get("approved_by")),
            "rejected_by": self._normalized_optional_string(item.get("rejected_by")),
            "diagnosis": self._dict_payload(item.get("diagnosis")),
            "finding": self._dict_payload(item.get("finding")),
            "proposed_change": self._dict_payload(item.get("proposed_change")),
            "baseline_quality_signals": self._dict_payload(item.get("baseline_quality_signals")),
            "expected_benefit": self._normalized_optional_string(item.get("expected_benefit")),
            "risk": self._normalized_optional_string(item.get("risk")),
            "validation_plan": self._normalized_optional_string(item.get("validation_plan")),
            "rollback_plan": self._normalized_optional_string(item.get("rollback_plan")),
            "tags": self._string_list(item.get("tags")),
            "metadata": self._dict_payload(item.get("metadata")),
            "operator_actions": {
                "update": f"/workflows/{workflow.id}/improvement-proposals/{item_id}",
                "review": f"/workflows/{workflow.id}/monitoring/events",
            },
        }

    def _new_steering_approval_entry(self, workflow: WorkflowDefinition, payload: dict[str, Any]) -> dict[str, Any]:
        recommended_action = self._required_string(payload.get("recommended_action"), field="recommended_action")
        reason = self._required_string(payload.get("reason"), field="reason")
        created_at = utc_now().isoformat()
        return {
            "id": str(payload.get("approval_id") or f"steering-{uuid4().hex[:12]}"),
            "source": "operator",
            "title": self._normalized_optional_string(
                payload.get("title")) or f"Steering approval for {recommended_action}",
            "status": self._normalized_status(payload.get("status"), default="pending"),
            "recommended_action": recommended_action,
            "reason": reason,
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "baseline_revision": workflow.versioning.revision,
            "expected_replacement_revision": workflow.versioning.revision + 1,
            "created_at": created_at,
            "updated_at": created_at,
            "created_by": self._normalized_optional_string(payload.get("created_by")) or "main-agent",
            "execution_id": self._normalized_optional_string(payload.get("execution_id")),
            "finding_event_id": self._normalized_optional_string(payload.get("finding_event_id")),
            "steering_request_event_id": self._normalized_optional_string(payload.get("steering_request_event_id")),
            "approval_request_id": self._normalized_optional_string(payload.get("approval_request_id")),
            "target_task_id": self._normalized_optional_string(payload.get("target_task_id")),
            "target_agent_id": self._normalized_optional_string(payload.get("target_agent_id")),
            "operator_parameters": self._dict_payload(payload.get("operator_parameters")),
            "evidence": self._dict_payload(payload.get("evidence")),
            "policy": self._dict_payload(payload.get("policy")),
            "metadata": self._dict_payload(payload.get("metadata")),
        }

    def _normalize_document_link_record(
            self,
            workflow: WorkflowDefinition,
            item: dict[str, Any],
            *,
            index: int,
    ) -> dict[str, Any]:
        item_id = self._normalized_optional_string(item.get("id")) or f"document-link-{index + 1}"
        return {
            "id": item_id,
            "workflow_id": workflow.id,
            "document_id": self._normalized_optional_string(item.get("document_id")) or "",
            "target_type": self._normalize_document_link_target_type(item.get("target_type")),
            "target_id": self._normalized_optional_string(item.get("target_id")),
            "label": self._normalized_optional_string(item.get("label")),
            "summary": self._normalized_optional_string(item.get("summary")),
            "linked_at": self._normalized_optional_string(item.get("linked_at")),
            "linked_by": self._normalized_optional_string(item.get("linked_by")),
            "metadata": self._dict_payload(item.get("metadata")),
            "operator_actions": {
                "delete": f"/workflows/{workflow.id}/document-links/{item_id}",
                "review": f"/workflows/{workflow.id}/documents",
            },
        }

    def _normalize_document_link_target_type(self, value: Any) -> str:
        normalized = self._normalized_optional_string(value) or "workflow"
        allowed = {"workflow", "improvement_proposal", "steering_approval"}
        if normalized not in allowed:
            raise ValueError(
                f"Unsupported target_type '{value}'. Choose one of: {', '.join(sorted(allowed))}."
            )
        return normalized

    def _normalize_document_link_target_id(
            self,
            workflow: WorkflowDefinition,
            *,
            target_type: str,
            target_id: Any,
    ) -> str | None:
        normalized_target_id = self._normalized_optional_string(target_id)
        if target_type == "workflow":
            return None
        if not normalized_target_id:
            raise ValueError(f"target_id is required for target_type '{target_type}'.")
        if target_type == "improvement_proposal":
            self._workflow_improvement_proposal_by_id(workflow, normalized_target_id)
            return normalized_target_id
        self._workflow_steering_approval_by_id(workflow, normalized_target_id)
        return normalized_target_id

    def _workflow_shared_memory_config_payload(self, workflow: WorkflowDefinition) -> dict[str, Any]:
        metadata = dict(workflow.metadata)
        shared_memory = dict(metadata.get(WORKFLOW_SHARED_MEMORY_METADATA_KEY) or {})
        return {
            "enabled": shared_memory.get("enabled") is True,
            "limit_per_layer": self._dict_payload(shared_memory.get("limit_per_layer")),
            "namespace_count": len(self._workflow_shared_memory_namespaces(workflow)),
        }

    def _new_shared_memory_namespace_entry(self, workflow: WorkflowDefinition, payload: dict[str, Any]) -> dict[
        str, Any]:
        name = self._required_string(payload.get("name"), field="name")
        created_at = utc_now().isoformat()
        return {
            "id": str(payload.get("namespace_id") or f"memory-namespace-{uuid4().hex[:12]}"),
            "name": name,
            "description": self._normalized_optional_string(payload.get("description")),
            "status": self._normalized_status(payload.get("status"), default="active"),
            "target_type": self._normalized_optional_string(payload.get("target_type")) or "workflow",
            "target_id": self._normalized_optional_string(payload.get("target_id")),
            "memory_scope": self._normalized_optional_string(payload.get("memory_scope")) or "workflow",
            "tags": self._string_list(payload.get("tags")),
            "memory_ids": self._string_list(payload.get("memory_ids")),
            "metadata": self._dict_payload(payload.get("metadata")),
            "created_at": created_at,
            "updated_at": created_at,
            "created_by": self._normalized_optional_string(payload.get("created_by")) or "main-agent",
            "updated_by": self._normalized_optional_string(payload.get("created_by")) or "main-agent",
            "workflow_id": workflow.id,
        }

    def _apply_shared_memory_namespace_patch(self, current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        updated = dict(current)
        for key, value in patch.items():
            if key == "name":
                updated[key] = self._required_string(value, field=key)
            elif key in {"description", "target_type", "target_id", "memory_scope", "created_by", "updated_by"}:
                updated[key] = self._normalized_optional_string(value)
            elif key == "status":
                updated[key] = self._normalized_status(value, default=str(updated.get("status") or "active"))
            elif key in {"tags", "memory_ids"}:
                updated[key] = self._string_list(value)
            elif key == "metadata":
                updated[key] = self._dict_payload(value)
            else:
                raise ValueError(f"Unsupported shared memory namespace fields: {key}")
        updated["updated_at"] = utc_now().isoformat()
        return updated

    def _normalize_shared_memory_namespace_record(
            self,
            workflow: WorkflowDefinition,
            item: dict[str, Any],
            *,
            index: int,
    ) -> dict[str, Any]:
        item_id = self._normalized_optional_string(item.get("id")) or f"memory-namespace-{index + 1}"
        memory_ids = self._string_list(item.get("memory_ids"))
        return {
            "id": item_id,
            "workflow_id": workflow.id,
            "name": self._normalized_optional_string(item.get("name")) or f"Shared memory namespace {index + 1}",
            "description": self._normalized_optional_string(item.get("description")),
            "status": self._normalized_status(item.get("status"), default="active"),
            "target_type": self._normalized_optional_string(item.get("target_type")) or "workflow",
            "target_id": self._normalized_optional_string(item.get("target_id")),
            "memory_scope": self._normalized_optional_string(item.get("memory_scope")) or "workflow",
            "tags": self._string_list(item.get("tags")),
            "memory_ids": memory_ids,
            "memory_count": len(memory_ids),
            "metadata": self._dict_payload(item.get("metadata")),
            "created_at": self._normalized_optional_string(item.get("created_at")),
            "updated_at": self._normalized_optional_string(item.get("updated_at"))
                          or self._normalized_optional_string(item.get("created_at")),
            "created_by": self._normalized_optional_string(item.get("created_by")),
            "updated_by": self._normalized_optional_string(item.get("updated_by")),
            "operator_actions": {
                "update": f"/workflows/{workflow.id}/shared-memory/namespaces/{item_id}",
                "list_memories": f"/workflows/{workflow.id}/shared-memory/namespaces/{item_id}/memories",
            },
        }

    def _apply_steering_approval_patch(self, current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        updated = dict(current)
        for key, value in patch.items():
            if key in {"title", "recommended_action", "reason"}:
                updated[key] = self._required_string(value, field=key)
            elif key == "status":
                updated[key] = self._normalized_status(value, default=str(updated.get("status") or "pending"))
            elif key in {
                "created_by",
                "execution_id",
                "finding_event_id",
                "steering_request_event_id",
                "approval_request_id",
                "target_task_id",
                "target_agent_id",
                "decision_reason",
                "decision_at",
                "decided_by",
                "approved_by",
                "rejected_by",
            }:
                updated[key] = self._normalized_optional_string(value)
            elif key in {"operator_parameters", "evidence", "policy", "metadata"}:
                updated[key] = self._dict_payload(value)
            else:
                raise ValueError(f"Unsupported steering approval fields: {key}")
        updated["updated_at"] = utc_now().isoformat()
        return updated

    def _normalize_steering_approval_record(
            self,
            workflow: WorkflowDefinition,
            item: dict[str, Any],
            *,
            index: int,
    ) -> dict[str, Any]:
        item_id = (
                self._normalized_optional_string(item.get("id"))
                or self._normalized_optional_string(item.get("approval_request_id"))
                or f"steering-history-{index + 1}"
        )
        status = self._normalized_status(item.get("status"), default="requested")
        recommended_action = self._normalized_optional_string(item.get("recommended_action")) or "review"
        return {
            "id": item_id,
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "title": self._normalized_optional_string(item.get("title")) or f"Steering approval {index + 1}",
            "status": status,
            "source": self._normalized_optional_string(item.get("source")) or "main_agent_monitor",
            "recommended_action": recommended_action,
            "reason": self._normalized_optional_string(item.get("reason")) or "Supervisor steering approval request.",
            "created_at": self._normalized_optional_string(item.get("created_at")),
            "updated_at": self._normalized_optional_string(item.get("updated_at"))
                          or self._normalized_optional_string(item.get("created_at")),
            "created_by": self._normalized_optional_string(item.get("created_by")),
            "baseline_revision": item.get("baseline_revision"),
            "expected_replacement_revision": item.get("expected_replacement_revision"),
            "execution_id": self._normalized_optional_string(item.get("execution_id")),
            "finding_event_id": self._normalized_optional_string(item.get("finding_event_id")),
            "steering_request_event_id": self._normalized_optional_string(item.get("steering_request_event_id")),
            "approval_request_id": self._normalized_optional_string(item.get("approval_request_id")),
            "decision_reason": self._normalized_optional_string(item.get("decision_reason")),
            "decision_at": self._normalized_optional_string(item.get("decision_at")),
            "decided_by": self._normalized_optional_string(item.get("decided_by")),
            "approved_by": self._normalized_optional_string(item.get("approved_by")),
            "rejected_by": self._normalized_optional_string(item.get("rejected_by")),
            "target_task_id": self._normalized_optional_string(item.get("target_task_id")),
            "target_agent_id": self._normalized_optional_string(item.get("target_agent_id")),
            "operator_parameters": self._dict_payload(item.get("operator_parameters")),
            "evidence": self._dict_payload(item.get("evidence")),
            "policy": self._dict_payload(item.get("policy")),
            "metadata": self._dict_payload(item.get("metadata")),
            "operator_actions": {
                "update": f"/workflows/{workflow.id}/steering-approvals/{item_id}",
                "review": f"/workflows/{workflow.id}/monitoring/events",
            },
        }

    def _find_governance_item_index(
            self,
            history: list[dict[str, Any]],
            item_id: str,
            *,
            id_keys: tuple[str, ...],
    ) -> int | None:
        for index, item in enumerate(history):
            for key in id_keys:
                if self._normalized_optional_string(item.get(key)) == item_id:
                    return index
        return None

    def _normalized_status(self, value: Any, *, default: str) -> str:
        normalized = self._normalized_optional_string(value)
        return normalized or default

    def _normalized_priority(self, value: Any) -> str:
        normalized = self._normalized_optional_string(value)
        return normalized or "medium"

    def _bounded_limit(self, value: Any, *, default: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return min(max(parsed, 1), maximum)

    def _required_string(self, value: Any, *, field: str) -> str:
        normalized = self._normalized_optional_string(value)
        if not normalized:
            raise ValueError(f"{field} must be a non-empty string")
        return normalized

    def _normalized_optional_string(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    def _dict_payload(self, value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        return sorted(set(cleaned))

    def _nested_optional_string(self, payload: dict[str, Any], *path: str) -> str | None:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return self._normalized_optional_string(current)

    def _nested_string_list(self, payload: dict[str, Any], *path: str) -> list[str]:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                return []
            current = current.get(key)
        return self._string_list(current)

    def _first_nonempty_line(self, text: str) -> str | None:
        for line in text.splitlines():
            normalized = line.strip()
            if normalized:
                return normalized[:240]
        return None

    async def _monitor_proposal_dispatches(self, workflow_id: str) -> dict[str, list[dict[str, Any]]]:
        dispatches: dict[str, list[dict[str, Any]]] = {}
        for conversation in await self.context.conversation_repo.list():
            for message in await self.context.conversation_message_repo.list_by_conversation(conversation.id):
                metadata = message.metadata if isinstance(message.metadata, dict) else {}
                if metadata.get("source") != "main_agent_monitor":
                    continue
                if metadata.get("monitor_action") != "dispatch_proposal_to_main_agent":
                    continue
                proposal_event_id = metadata.get("monitor_proposal_event_id")
                if not isinstance(proposal_event_id, str) or not proposal_event_id:
                    continue
                message_workflow_id = metadata.get("page_context", {}).get("selection", {}).get(
                    "workflowId") if isinstance(metadata.get("page_context"), dict) else None
                if not isinstance(message_workflow_id, str) or message_workflow_id != workflow_id:
                    continue
                dispatches.setdefault(proposal_event_id, []).append(
                    {
                        "message_id": message.id,
                        "conversation_id": message.conversation_id,
                        "created_at": message.created_at.isoformat(),
                        "operator_note": metadata.get("operator_note"),
                    }
                )
        for items in dispatches.values():
            items.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("message_id") or "")))
        return dispatches

    async def _monitor_proposal_event(
            self,
            workflow_id: str,
            proposal_event_id: str,
    ) -> ExecutionEvent | None:
        executions = await self.context.execution_store.list_executions_by_workflow(workflow_id)
        for execution in executions:
            for event in await self.context.execution_store.list_events(execution.id):
                if (
                        event.id == proposal_event_id
                        and event.event_type == ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED
                ):
                    return event
        return None

    async def _monitor_dispatch_conversation_id(self, workflow: WorkflowDefinition) -> str | None:
        monitoring = workflow.metadata.get("main_agent_monitoring")
        monitoring = monitoring if isinstance(monitoring, dict) else {}
        conversation_id = monitoring.get("approval_conversation_id")
        if isinstance(conversation_id, str) and conversation_id.strip():
            return conversation_id.strip()

        profile = await self._active_main_agent_profile()
        profile_monitoring = (
            profile.metadata.get("main_agent_monitoring")
            if profile is not None and isinstance(profile.metadata, dict)
            else None
        )
        profile_monitoring = profile_monitoring if isinstance(profile_monitoring, dict) else {}
        conversation_id = profile_monitoring.get("approval_conversation_id")
        if isinstance(conversation_id, str) and conversation_id.strip():
            return conversation_id.strip()
        return None

    async def _ensure_monitor_dispatch_conversation(
            self,
            workflow: WorkflowDefinition,
            *,
            actor_user_id: str,
    ) -> Conversation:
        conversation_id = await self._monitor_dispatch_conversation_id(workflow)
        if conversation_id is not None:
            conversation = await self.context.conversation_repo.get(conversation_id)
            if conversation is not None:
                return conversation
            raise ValueError(f"Conversation '{conversation_id}' was not found.")

        profile = await self._active_main_agent_profile()
        # Advisory proposals still need a durable conversation target so the operator can trigger
        # implementation work even before monitor approval routing is explicitly configured.
        return await self.context.conversation_repo.create(
            Conversation(
                title=f"Main agent review · {workflow.name or workflow.id}",
                created_by_user_id=actor_user_id,
                main_agent_profile_id=profile.id if profile is not None else None,
                channel_type="api",
                metadata={
                    "source": "main_agent_monitor",
                    "workflow_id": workflow.id,
                    "workflow_name": workflow.name,
                    "conversation_purpose": "monitor_proposal_dispatch",
                },
            )
        )

    def _monitor_proposal_dispatch_text(
            self,
            workflow: WorkflowDefinition,
            proposal_event: ExecutionEvent,
            *,
            operator_note: str | None = None,
    ) -> str:
        payload = proposal_event.payload if isinstance(proposal_event.payload, dict) else {}
        proposed_change = payload.get("proposed_change") if isinstance(payload.get("proposed_change"), dict) else {}
        finding = payload.get("finding") if isinstance(payload.get("finding"), dict) else {}
        finding_evidence = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
        diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
        recommendation_items = proposed_change.get("summary")
        if not isinstance(recommendation_items, str) or not recommendation_items.strip():
            recommendation_items = payload.get("summary") if isinstance(payload.get("summary"), str) else None

        lines = [
            f"Review monitor improvement proposal `{proposal_event.id}` for workflow `{workflow.id}` ({workflow.name}).",
            "Decide whether the recommendation should be executed.",
            "If it is sound, use the normal workflow-update path to draft or apply the change with any required approvals.",
            "If it is not sound, explain why and propose a safer revision.",
            "",
        ]
        if recommendation_items:
            lines.append(f"Proposal summary: {recommendation_items}")
        if isinstance(payload.get("expected_benefit"), str) and payload["expected_benefit"].strip():
            lines.append(f"Expected benefit: {payload['expected_benefit']}")
        if isinstance(payload.get("risk"), str) and payload["risk"].strip():
            lines.append(f"Risk: {payload['risk']}")
        if isinstance(payload.get("validation_plan"), str) and payload["validation_plan"].strip():
            lines.append(f"Validation plan: {payload['validation_plan']}")
        if isinstance(payload.get("rollback_plan"), str) and payload["rollback_plan"].strip():
            lines.append(f"Rollback plan: {payload['rollback_plan']}")
        if isinstance(diagnosis.get("summary"), str) and diagnosis["summary"].strip():
            lines.append(f"Diagnosis: {diagnosis['summary']}")
        elif isinstance(payload.get("diagnosis"), str) and payload["diagnosis"].strip():
            lines.append(f"Diagnosis: {payload['diagnosis']}")
        lines.append(f"Evidence count: {len(finding_evidence)}")
        if operator_note:
            lines.extend(["", f"Operator note: {operator_note}"])
        return "\n".join(lines)

    async def _recent_main_agent_monitor_events(self, workflows: list[WorkflowDefinition]) -> dict[
        str, list[dict[str, Any]]]:
        findings: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        steering_requests: list[dict[str, Any]] = []
        workflow_names = {workflow.id: workflow.name for workflow in workflows}
        for workflow in workflows:
            executions = await self.context.execution_store.list_executions_by_workflow(workflow.id)
            for execution in executions:
                for event in await self.context.execution_store.list_events(execution.id):
                    if event.event_type not in MONITOR_EVENT_TYPES:
                        continue
                    item = {
                        **event.model_dump(mode="json"),
                        "workflow": {
                            "id": workflow.id,
                            "name": workflow_names.get(workflow.id) or workflow.id,
                        },
                    }
                    if event.event_type == ExecutionEventType.MONITOR_FINDING_CREATED:
                        findings.append(item)
                    elif event.event_type == ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED:
                        proposals.append(item)
                    elif event.event_type == ExecutionEventType.SUPERVISOR_STEERING_REQUESTED:
                        steering_requests.append(item)

        def latest(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return sorted(
                items,
                key=lambda item: (
                    str(item.get("timestamp") or ""),
                    int(item.get("sequence") or 0),
                    str(item.get("id") or ""),
                ),
                reverse=True,
            )[:20]

        return {
            "findings": latest(findings),
            "proposals": latest(proposals),
            "steering_requests": latest(steering_requests),
        }

    def _approval_repo_write_permission(self, approval: ApprovalRequest) -> dict[str, Any] | None:
        payload = approval.proposed_payload if isinstance(approval.proposed_payload, dict) else {}
        permission = payload.get("repo_write_permission")
        if isinstance(permission, dict):
            return permission
        workflow = payload.get("workflow")
        workflow_metadata = workflow.get("metadata") if isinstance(workflow, dict) else None
        workflow_metadata = workflow_metadata if isinstance(workflow_metadata, dict) else {}
        permission = workflow_metadata.get("repo_write_permission")
        return permission if isinstance(permission, dict) else None

    async def repair_stale_workflow_executions(self, workflow_id: str) -> dict[str, Any]:
        if await self.context.workflow_repo.get(workflow_id) is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' not found")
        repaired = await self.context.control_plane.repair_stale_executions(workflow_id=workflow_id)
        return {
            "workflow_id": workflow_id,
            "items": repaired,
            "repaired_count": len(repaired),
        }

    async def _persona_version_notices_for_workflow(
            self,
            workflow: WorkflowDefinition,
            *,
            persona: PersonaDefinition | None = None,
            include_current: bool,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for agent in workflow.agent_definitions:
            persona_id = self._persona_id_for_agent(agent)
            if persona_id is None:
                continue
            if persona is not None and persona_id != persona.id:
                continue
            resolved_persona = persona or await self.context.persona_repo.get(persona_id, include_deleted=True)
            if resolved_persona is None:
                continue
            item = await self._persona_version_usage_payload(workflow=workflow, agent=agent, persona=resolved_persona)
            if include_current or item["status"] == "outdated":
                items.append(item)
        return items

    async def _persona_version_usage_payload(
            self,
            *,
            workflow: WorkflowDefinition,
            agent: AgentDefinition,
            persona: PersonaDefinition,
    ) -> dict[str, Any]:
        metadata = agent.metadata if isinstance(agent.metadata, dict) else {}
        agent_version_id = self._persona_version_id_for_agent(agent)
        current_version_id = persona.current_version_id
        accepted_for = metadata.get(PERSONA_VERSION_PIN_ACCEPTED_FOR_KEY)
        if agent_version_id == current_version_id:
            status = "current"
        elif accepted_for == current_version_id:
            status = "pinned"
        else:
            status = "outdated"
        agent_version = await self._persona_version(agent_version_id)
        current_version = await self._persona_version(current_version_id)
        return {
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "agent_id": agent.id,
            "agent_name": agent.display_name or agent.name,
            "persona_id": persona.id,
            "persona_slug": persona.slug,
            "persona_name": persona.name,
            "status": status,
            "message": self._persona_version_usage_message(
                persona=persona,
                status=status,
                agent_version=agent_version,
                current_version=current_version,
            ),
            "persona_version_id": agent_version_id,
            "persona_version": agent_version.version if agent_version is not None else None,
            "current_persona_version_id": current_version_id,
            "current_persona_version": current_version.version if current_version is not None else None,
            "published_agent_id": persona.published_agent_id,
            "pin_accepted_for": accepted_for if isinstance(accepted_for, str) else None,
            "pin_decision": metadata.get(PERSONA_VERSION_PIN_DECISION_KEY),
            "operator_actions": {
                "use_latest": f"/workflows/{workflow.id}/persona-agents/{agent.id}/use-latest",
                "keep_current": f"/workflows/{workflow.id}/persona-agents/{agent.id}/keep-current",
            },
        }

    def _persona_version_usage_message(
            self,
            *,
            persona: PersonaDefinition,
            status: str,
            agent_version: PersonaVersion | None,
            current_version: PersonaVersion | None,
    ) -> str:
        current_label = current_version.version if current_version is not None else persona.current_version_id
        agent_label = agent_version.version if agent_version is not None else None
        if status == "outdated":
            return (
                f"@{persona.slug} has a newer published persona version"
                f" ({current_label}); this workflow uses {agent_label or 'an older snapshot'}."
            )
        if status == "pinned":
            return (
                f"This workflow is intentionally keeping its @{persona.slug} snapshot while"
                f" version {current_label} is current."
            )
        return f"This workflow uses the current @{persona.slug} persona version."

    async def _persona_version(self, version_id: str | None) -> PersonaVersion | None:
        if not version_id:
            return None
        return await self.context.persona_version_repo.get(version_id, include_deleted=True)

    def _workflow_agent_by_id(self, workflow: WorkflowDefinition, agent_id: str) -> AgentDefinition:
        for agent in workflow.agent_definitions:
            if agent.id == agent_id:
                return agent
        raise ValueError(f"Agent '{agent_id}' was not found in workflow '{workflow.id}'.")

    @staticmethod
    def _persona_id_for_agent(agent: AgentDefinition) -> str | None:
        metadata = agent.metadata if isinstance(agent.metadata, dict) else {}
        value = metadata.get("persona_id")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _persona_version_id_for_agent(agent: AgentDefinition) -> str | None:
        metadata = agent.metadata if isinstance(agent.metadata, dict) else {}
        value = metadata.get("persona_version_id")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _workflow_embedded_latest_persona_agent(
            self,
            *,
            latest_agent: AgentDefinition,
            embedded_agent: AgentDefinition,
            persona: PersonaDefinition,
            updated_by_user_id: str | None,
    ) -> AgentDefinition:
        metadata = dict(latest_agent.metadata)
        for key, value in embedded_agent.metadata.items():
            # Graph layout is workflow-local state; republishing a persona should not move nodes around.
            if key in PERSONA_AGENT_WORKFLOW_METADATA_KEYS or key.startswith("workflow_"):
                metadata[key] = value
        metadata.pop(PERSONA_VERSION_PIN_ACCEPTED_FOR_KEY, None)
        metadata.pop(PERSONA_VERSION_PIN_DECISION_KEY, None)
        metadata.pop(PERSONA_VERSION_PIN_ACCEPTED_AT_KEY, None)
        metadata.pop(PERSONA_VERSION_PIN_ACCEPTED_BY_KEY, None)
        metadata.update(
            {
                "persona_id": persona.id,
                "persona_slug": persona.slug,
                "persona_version_id": persona.current_version_id,
                "workflow_persona_refreshed_at": utc_now().isoformat(),
            }
        )
        if updated_by_user_id:
            metadata["workflow_persona_refreshed_by"] = updated_by_user_id
        return latest_agent.model_copy(update={"metadata": metadata})

    def _promoted_global_agent(
            self,
            *,
            workflow: WorkflowDefinition,
            embedded_agent: AgentDefinition,
            target_agent_id: str,
            promoted_by_user_id: str | None,
    ) -> AgentDefinition:
        metadata = dict(embedded_agent.metadata)
        # Preserve where this catalog agent came from so future edits can explain
        # whether they originated as a workflow-local snapshot or a canonical asset.
        metadata.update(
            {
                "promoted_from_workflow_id": workflow.id,
                "promoted_from_workflow_name": workflow.name,
                "promoted_from_workflow_agent_id": embedded_agent.id,
                "promoted_to_global_at": utc_now().isoformat(),
            }
        )
        if promoted_by_user_id:
            metadata["promoted_to_global_by"] = promoted_by_user_id
        return embedded_agent.model_copy(update={"id": target_agent_id, "metadata": metadata})

    @staticmethod
    def _is_same_promoted_workflow_agent(
            *,
            existing_global_agent: AgentDefinition,
            workflow: WorkflowDefinition,
            embedded_agent: AgentDefinition,
    ) -> bool:
        metadata = existing_global_agent.metadata if isinstance(existing_global_agent.metadata, dict) else {}
        return (
                metadata.get("promoted_from_workflow_id") == workflow.id
                and metadata.get("promoted_from_workflow_agent_id") == embedded_agent.id
        )

    async def _replace_workflow_agent(
            self,
            workflow: WorkflowDefinition,
            old_agent_id: str,
            replacement: AgentDefinition,
    ) -> WorkflowDefinition:
        agent_definitions = [
            replacement if agent.id == old_agent_id else agent
            for agent in workflow.agent_definitions
        ]
        patch: dict[str, Any] = {
            "agent_definitions": [
                agent.model_dump(mode="json")
                for agent in agent_definitions
            ]
        }
        if replacement.id != old_agent_id:
            patch["nodes"] = [
                node.model_copy(
                    update={"agent_id": replacement.id if node.agent_id == old_agent_id else node.agent_id}
                ).model_dump(mode="json")
                for node in workflow.nodes
            ]
            patch["task_definitions"] = [
                task.model_copy(
                    update={"agent_id": replacement.id if task.agent_id == old_agent_id else task.agent_id}
                ).model_dump(mode="json")
                for task in workflow.task_definitions
            ]
        updated = await self.context.workflow_repo.update(workflow.id, patch)
        if updated is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow.id}' not found")
        await self.maybe_replace_active_executions_for_revision_change(
            before=workflow,
            after=updated,
            source="workflow_persona_agent_update",
        )
        return updated

    async def _workflow_execution_payload(self, execution: Execution) -> dict[str, Any]:
        payload = execution.model_dump(mode="json")
        settings = get_settings()
        stale_classification = classify_execution_staleness(
            execution,
            stale_after_seconds=settings.main_agent_workflow_monitor_stale_after_seconds,
            idle_timeout_seconds=settings.agent_activity_idle_timeout_seconds,
            run_timeout_seconds=settings.agent_run_timeout_seconds,
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
