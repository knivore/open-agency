"""Workflow catalog, ownership, marketplace, monitoring, and version routes."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from typing import Any, Optional
from uuid import uuid4

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user, resolve_current_user_if_present
from app.core.config import get_settings
from app.domain import GraphProjectionEvent, UserDefinition, WorkflowDefinition
from app.runtime.native.errors import WorkflowNotFoundError
from app.services.memory import MemoryPermissionError, MemoryService
from app.services.workflows import WorkflowAgentPromotionConflictError
from app.services.workflows import WorkflowService

SHARED_MEMORY_LIMIT_KEYS = {
    "decisions",
    "commitments",
    "facts_and_preferences",
    "recent_summaries",
    "semantic_fallback",
}

WORKFLOW_MEMORY_LINK_TARGET_TYPES = {"workflow", "agent", "task"}
WORKFLOW_MEMORY_LINK_REF_TYPES = {"memory", "memory_collection"}
WORKFLOW_MEMORY_LINK_ACCESS_MODES = {"read", "read_write"}
WORKFLOW_MEMORY_LINK_METADATA_KEY = "memory_links"
logger = logging.getLogger(__name__)


class WorkflowSharedMemoryPatch(BaseModel):
    enabled: bool | None = None
    limit_per_layer: dict[str, int] | None = None
    apply_to_agents: bool = False
    agent_scope: str = Field(default="workflow")


class WorkflowMemoryLinkRequest(BaseModel):
    target_type: str = Field(validation_alias=AliasChoices("targetType", "target_type"))
    target_id: str | None = Field(default=None, validation_alias=AliasChoices("targetId", "target_id"))
    ref_type: str = Field(validation_alias=AliasChoices("refType", "ref_type"))
    ref_id: str = Field(validation_alias=AliasChoices("refId", "ref_id"))
    access_mode: str = Field(default="read", validation_alias=AliasChoices("accessMode", "access_mode"))
    label: str | None = None


class WorkflowRuntimeTokenBudgetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_total_tokens: int | None = Field(
        default=None,
        validation_alias=AliasChoices("runTotalTokens", "run_total_tokens"),
    )
    workflow_total_tokens: int | None = Field(
        default=None,
        validation_alias=AliasChoices("workflowTotalTokens", "workflow_total_tokens"),
    )
    agent_total_tokens: int | None = Field(
        default=None,
        validation_alias=AliasChoices("agentTotalTokens", "agent_total_tokens"),
    )
    warn_ratio: float | None = Field(default=None, validation_alias=AliasChoices("warnRatio", "warn_ratio"))
    hard_ratio: float | None = Field(default=None, validation_alias=AliasChoices("hardRatio", "hard_ratio"))
    action: str | None = None


class WorkflowRuntimeContextCompactionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    persist_context_pack: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("persistContextPack", "persist_context_pack"),
    )
    preserve_recent_messages: int | None = Field(
        default=None,
        validation_alias=AliasChoices("preserveRecentMessages", "preserve_recent_messages"),
    )
    oversized_message_tokens: int | None = Field(
        default=None,
        validation_alias=AliasChoices("oversizedMessageTokens", "oversized_message_tokens"),
    )
    min_estimated_tokens_saved: int | None = Field(
        default=None,
        validation_alias=AliasChoices("minEstimatedTokensSaved", "min_estimated_tokens_saved"),
    )
    max_summary_chars: int | None = Field(
        default=None,
        validation_alias=AliasChoices("maxSummaryChars", "max_summary_chars"),
    )


class WorkflowRuntimeExecutionPolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_runtime_seconds: int | None = Field(
        default=None,
        validation_alias=AliasChoices("maxRuntimeSeconds", "max_runtime_seconds"),
    )
    max_retries: int | None = Field(
        default=None,
        validation_alias=AliasChoices("maxRetries", "max_retries"),
    )
    concurrency_limit: int | None = Field(
        default=None,
        validation_alias=AliasChoices("concurrencyLimit", "concurrency_limit"),
    )
    approval_mode: str | None = Field(
        default=None,
        validation_alias=AliasChoices("approvalMode", "approval_mode"),
    )


class WorkflowRuntimeGovernancePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_budget: WorkflowRuntimeTokenBudgetPatch | None = Field(
        default=None,
        validation_alias=AliasChoices("tokenBudget", "token_budget"),
    )
    context_compaction: WorkflowRuntimeContextCompactionPatch | None = Field(
        default=None,
        validation_alias=AliasChoices("contextCompaction", "context_compaction"),
    )
    execution_policy: WorkflowRuntimeExecutionPolicyPatch | None = Field(
        default=None,
        validation_alias=AliasChoices("executionPolicy", "execution_policy"),
    )


class WorkflowSteeringApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended_action: str = Field(validation_alias=AliasChoices("recommendedAction", "recommended_action"))
    reason: str
    title: str | None = None
    execution_id: str | None = Field(default=None, validation_alias=AliasChoices("executionId", "execution_id"))
    target_task_id: str | None = Field(default=None, validation_alias=AliasChoices("targetTaskId", "target_task_id"))
    target_agent_id: str | None = Field(default=None, validation_alias=AliasChoices("targetAgentId", "target_agent_id"))
    operator_parameters: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("operatorParameters", "operator_parameters"),
    )
    evidence: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    request_approval: bool = Field(default=True, validation_alias=AliasChoices("requestApproval", "request_approval"))


class WorkflowAgentPromotionRequest(BaseModel):
    global_agent_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("globalAgentId", "global_agent_id"),
    )
    replace_workflow_agent: bool = Field(
        default=False,
        validation_alias=AliasChoices("replaceWorkflowAgent", "replace_workflow_agent"),
    )


class WorkflowGovernanceBundleRequest(BaseModel):
    attach_top_suggestion: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("attachTopSuggestion", "attach_top_suggestion"),
    )
    request_approval: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("requestApproval", "request_approval"),
    )
    document_limit: int | None = Field(
        default=None,
        validation_alias=AliasChoices("documentLimit", "document_limit"),
    )
    evidence_label: str | None = Field(
        default=None,
        validation_alias=AliasChoices("evidenceLabel", "evidence_label"),
    )
    evidence_summary: str | None = Field(
        default=None,
        validation_alias=AliasChoices("evidenceSummary", "evidence_summary"),
    )
    metadata: dict[str, Any] | None = None
    dry_run: bool | None = Field(default=None, validation_alias=AliasChoices("dryRun", "dry_run"))


class WorkflowGovernanceAttachEvidenceRequest(BaseModel):
    document_id: str = Field(validation_alias=AliasChoices("documentId", "document_id"))
    label: str | None = None
    summary: str | None = None
    linked_by: str | None = Field(default=None, validation_alias=AliasChoices("linkedBy", "linked_by"))
    metadata: dict[str, Any] | None = None


def _owner_ids(workflow: WorkflowDefinition) -> list[str]:
    value = workflow.metadata.get("owner_ids")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _is_owner_or_admin(workflow: WorkflowDefinition, user: UserDefinition) -> bool:
    if "admin" in user.roles:
        return True
    owner_ids = _owner_ids(workflow)
    created_by = workflow.metadata.get("created_by")
    return user.id in owner_ids or created_by == user.id


def _require_owner_or_admin(workflow: WorkflowDefinition, user: UserDefinition) -> None:
    if not _is_owner_or_admin(workflow, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workflow owner access is required")


def _has_explicit_owner(workflow: WorkflowDefinition) -> bool:
    return bool(_owner_ids(workflow) or workflow.metadata.get("created_by"))


def _claimed_workflow_metadata(workflow: WorkflowDefinition, user: UserDefinition) -> dict[str, Any]:
    metadata = dict(workflow.metadata)
    existing_owner_ids = _owner_ids(workflow)
    metadata["created_by"] = (
        workflow.metadata.get("created_by")
        if isinstance(workflow.metadata.get("created_by"), str) and workflow.metadata.get("created_by")
        else user.id
    )
    metadata["owner_ids"] = list(dict.fromkeys([*existing_owner_ids, user.id]))
    return metadata


async def _ensure_owner_or_admin(
        workflow_id: str,
        workflow: WorkflowDefinition,
        user: UserDefinition,
        context: ApiContext,
) -> WorkflowDefinition:
    if _is_owner_or_admin(workflow, user):
        return workflow
    if _has_explicit_owner(workflow):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workflow owner access is required")
    updated = await context.workflow_repo.update(workflow_id, {"metadata": _claimed_workflow_metadata(workflow, user)})
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
    return updated


def _parse_owner_ids(payload: Any) -> list[str]:
    value = payload.get("owner_ids") if isinstance(payload, dict) else payload
    if not isinstance(value, list):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="owner_ids must be a list")
    owner_ids = [item for item in value if isinstance(item, str) and item.strip()]
    if not owner_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one owner id is required")
    return owner_ids


def _clone_workflow_definition(source: WorkflowDefinition, user: UserDefinition) -> WorkflowDefinition:
    payload = deepcopy(source.model_dump(mode="json"))
    payload["id"] = str(uuid4())
    payload["name"] = f"Copy of {source.name}"
    metadata = dict(payload.get("metadata") or {})
    metadata.update(
        {
            "owner_ids": [user.id],
            "created_by": user.id,
            "cloned_by": user.id,
            "cloned_from_workflow_id": source.id,
            "cloned_at": datetime.now(timezone.utc).isoformat(),
            "marketplace_status": "draft",
        }
    )
    payload["metadata"] = metadata
    versioning = dict(payload.get("versioning") or {})
    versioning["is_published"] = False
    payload["versioning"] = versioning
    return WorkflowDefinition.model_validate(payload)


def _workflow_response_payload(
        workflow: WorkflowDefinition,
        context: ApiContext,
        *,
        main_agent_default_workflow_id: str | None = None,
) -> dict[str, Any]:
    payload = workflow.model_dump(mode="json")
    payload["monitoring"] = WorkflowService(context).monitoring_operator_payload(
        workflow,
        main_agent_default_workflow_id=main_agent_default_workflow_id,
    )
    payload["runtime_governance"] = WorkflowService(context).runtime_governance_operator_payload(workflow)
    return payload


def _workflow_validate_payload(payload: dict[str, Any]) -> WorkflowDefinition:
    # The workflow detail route adds operator-facing summaries that are not part of
    # the canonical workflow contract. Strip those fields so validate accepts a
    # round-tripped detail payload from the UI without weakening the base model.
    sanitized = dict(payload)
    sanitized.pop("monitoring", None)
    sanitized.pop("runtime_governance", None)
    return WorkflowDefinition.model_validate(sanitized)


def _normalize_shared_memory_limits(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    limits: dict[str, int] = {}
    for key, value in raw.items():
        if key not in SHARED_MEMORY_LIMIT_KEYS:
            continue
        try:
            limit = int(value)
        except (TypeError, ValueError):
            continue
        limits[key] = max(min(limit, 50), 0)
    return limits


def _shared_memory_operator_payload(workflow: WorkflowDefinition) -> dict[str, Any]:
    config = workflow.metadata.get("shared_memory")
    config = config if isinstance(config, dict) else {}
    agent_states = [
        {
            "agent_id": agent.id,
            "name": agent.name,
            "enabled": agent.memory.enabled,
            "scope": agent.memory.scope,
        }
        for agent in workflow.agent_definitions
    ]
    return {
        "workflow_id": workflow.id,
        "enabled": bool(config.get("enabled", False)),
        "limit_per_layer": _normalize_shared_memory_limits(config.get("limit_per_layer")),
        "agent_states": agent_states,
        "memory_filters": {
            "workflow": {"scope": "workflow", "workflow_id": workflow.id},
            "workspace": {
                "scope": "workspace",
                "workspace_id": workflow.metadata.get("workspace_id"),
            },
        },
    }


def _update_agent_shared_memory(workflow: WorkflowDefinition, *, enabled: bool, scope: str) -> list:
    normalized_scope = scope if scope in {"agent", "workflow", "user"} else "workflow"
    updated_agents = []
    for agent in workflow.agent_definitions:
        memory = agent.memory.model_copy(
            update={
                "enabled": enabled,
                "strategy": "shared" if enabled else agent.memory.strategy,
                "scope": normalized_scope if enabled else agent.memory.scope,
            }
        )
        updated_agents.append(agent.model_copy(update={"memory": memory}))
    return updated_agents


def _workflow_memory_links(workflow: WorkflowDefinition) -> list[dict[str, Any]]:
    raw_links = workflow.metadata.get(WORKFLOW_MEMORY_LINK_METADATA_KEY)
    if not isinstance(raw_links, list):
        return []
    links: list[dict[str, Any]] = []
    for raw in raw_links:
        if isinstance(raw, dict):
            links.append(dict(raw))
    return links


def _serialize_workflow_memory_link(workflow_id: str, link: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(link.get("id") or ""),
        "workflowId": workflow_id,
        "targetType": str(link.get("target_type") or ""),
        "targetId": link.get("target_id"),
        "refType": str(link.get("ref_type") or ""),
        "refId": str(link.get("ref_id") or ""),
        "memoryIds": link.get("memory_ids") if isinstance(link.get("memory_ids"), list) else [],
        "accessMode": str(link.get("access_mode") or "read"),
        "label": link.get("label"),
        "createdAt": link.get("created_at"),
        "createdBy": link.get("created_by"),
        "updatedAt": link.get("updated_at"),
        "updatedBy": link.get("updated_by"),
    }


def _normalize_memory_link_value(value: str, allowed: set[str], field_name: str) -> str:
    normalized = value.strip().lower()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unsupported {field_name} '{value}'. Choose one of: {choices}.",
        )
    return normalized


def _validate_memory_link_target(workflow: WorkflowDefinition, target_type: str, target_id: str | None) -> None:
    if target_type == "workflow":
        return
    if not target_id or not target_id.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="target_id is required for agent and task memory links.",
        )
    if target_type == "agent" and target_id not in {agent.id for agent in workflow.agent_definitions}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent '{target_id}' not found in workflow")
    if target_type == "task" and target_id not in {task.id for task in workflow.task_definitions}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task '{target_id}' not found in workflow")


def _document_id_from_memory_metadata(item: Any) -> str | None:
    metadata = getattr(item, "metadata", None)
    value = metadata.get("document_id") if isinstance(metadata, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _resolve_memory_link_ref(
        *,
        context: ApiContext,
        current_user: UserDefinition,
        ref_type: str,
        ref_id: str,
) -> tuple[list[str], str | None]:
    memory_service = MemoryService(context)
    if ref_type == "memory":
        try:
            memory = await memory_service.get_memory(ref_id, current_user=current_user)
        except MemoryPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        if memory is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Memory '{ref_id}' not found")
        return [memory.id], memory.summary or memory.content[:80] or memory.id

    candidates = [
        item
        for item in await context.memory_repo.list()
        if _document_id_from_memory_metadata(item) == ref_id
           and await memory_service.can_read(item, current_user=current_user)
    ]
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document memory collection '{ref_id}' not found",
        )
    candidates.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
    representative = candidates[0]
    filename = representative.metadata.get("filename") if isinstance(representative.metadata, dict) else None
    label = filename if isinstance(filename, str) and filename.strip() else f"Document {ref_id}"
    return [item.id for item in candidates], label


def _memory_link_identity(link: dict[str, Any]) -> tuple[str, str | None, str, str, str]:
    return (
        str(link.get("target_type") or ""),
        link.get("target_id") if isinstance(link.get("target_id"), str) else None,
        str(link.get("ref_type") or ""),
        str(link.get("ref_id") or ""),
        str(link.get("access_mode") or "read"),
    )


async def _emit_workflow_memory_link_projection_event(
        *,
        context: ApiContext,
        workflow_id: str,
        event_type: str,
        link: dict[str, Any],
        user_id: str | None,
) -> None:
    if not get_settings().graph_projection_enabled:
        return
    repo = getattr(context, "graph_projection_event_repo", None)
    if repo is None:
        return
    try:
        await repo.append(
            GraphProjectionEvent(
                event_type=event_type,
                aggregate_type="workflow_memory_link",
                aggregate_id=str(link.get("id") or f"{workflow_id}:{link.get('ref_id') or 'unknown'}"),
                user_id=user_id,
                payload={
                    "workflow_id": workflow_id,
                    "link": _serialize_workflow_memory_link(workflow_id, link),
                },
                source="workflow_memory_links",
            )
        )
    except Exception:
        logger.exception("Failed to append workflow memory link graph projection event")


def create_workflows_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/workflows", tags=["Workflows"])
    service = WorkflowService(context)

    @router.post("", summary="Create Workflow")
    async def create_workflow(payload: WorkflowDefinition, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        payload = payload.model_copy(update={"metadata": _claimed_workflow_metadata(payload, current_user)})
        created = await context.workflow_repo.create(payload)
        return created.model_dump(mode="json")

    @router.get("", summary="List Workflows")
    async def list_workflows(request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["workflows:read"])
        items = await context.workflow_repo.list()
        default_workflow_id = await service.main_agent_default_workflow_id()
        return {
            "items": [
                _workflow_response_payload(
                    item,
                    context,
                    main_agent_default_workflow_id=default_workflow_id,
                )
                for item in items
            ]
        }

    @router.get("/{workflow_id}/shared-memory", summary="Get Workflow Shared Memory Settings")
    async def get_workflow_shared_memory(workflow_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:read"])
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        workflow = await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        return _shared_memory_operator_payload(workflow)

    @router.patch("/{workflow_id}/shared-memory", summary="Update Workflow Shared Memory Settings")
    async def update_workflow_shared_memory(
            workflow_id: str,
            patch: WorkflowSharedMemoryPatch,
            request: Request,
    ):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        workflow = await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        metadata = dict(workflow.metadata)
        shared_memory = metadata.get("shared_memory")
        shared_memory = dict(shared_memory) if isinstance(shared_memory, dict) else {}
        if "enabled" in patch.model_fields_set and patch.enabled is not None:
            shared_memory["enabled"] = patch.enabled
        if "limit_per_layer" in patch.model_fields_set:
            if patch.limit_per_layer:
                shared_memory["limit_per_layer"] = _normalize_shared_memory_limits(patch.limit_per_layer)
            else:
                shared_memory.pop("limit_per_layer", None)
        metadata["shared_memory"] = shared_memory
        workflow_patch: dict[str, Any] = {"metadata": metadata}
        if patch.apply_to_agents and patch.enabled is not None:
            workflow_patch["agent_definitions"] = [
                agent.model_dump(mode="json")
                for agent in _update_agent_shared_memory(
                    workflow,
                    enabled=patch.enabled,
                    scope=patch.agent_scope,
                )
            ]
        updated = await context.workflow_repo.update(workflow_id, workflow_patch)
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        await service.maybe_replace_active_executions_for_revision_change(
            before=workflow,
            after=updated,
            source="workflow_shared_memory_update",
        )
        return {"workflow": updated.model_dump(mode="json"), "shared_memory": _shared_memory_operator_payload(updated)}

    @router.get("/{workflow_id}/memory-links", summary="List Workflow Memory Links")
    async def list_workflow_memory_links(workflow_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:read", "memory:read"])
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        workflow = await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        links = [_serialize_workflow_memory_link(workflow_id, link) for link in _workflow_memory_links(workflow)]
        return {"workflowId": workflow_id, "items": links}

    @router.post("/{workflow_id}/memory-links", summary="Link Memory Resource To Workflow")
    async def add_workflow_memory_link(
            workflow_id: str,
            payload: WorkflowMemoryLinkRequest,
            request: Request,
    ):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write", "memory:read"])
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        workflow = await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)

        target_type = _normalize_memory_link_value(
            payload.target_type,
            WORKFLOW_MEMORY_LINK_TARGET_TYPES,
            "target_type",
        )
        ref_type = _normalize_memory_link_value(payload.ref_type, WORKFLOW_MEMORY_LINK_REF_TYPES, "ref_type")
        access_mode = _normalize_memory_link_value(
            payload.access_mode,
            WORKFLOW_MEMORY_LINK_ACCESS_MODES,
            "access_mode",
        )
        target_id = payload.target_id.strip() if isinstance(payload.target_id,
                                                            str) and payload.target_id.strip() else None
        if target_type == "workflow":
            target_id = None
        _validate_memory_link_target(workflow, target_type, target_id)
        ref_id = payload.ref_id.strip()
        if not ref_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="ref_id is required for workflow memory links.",
            )
        memory_ids, default_label = await _resolve_memory_link_ref(
            context=context,
            current_user=current_user,
            ref_type=ref_type,
            ref_id=ref_id,
        )

        now = datetime.now(timezone.utc).isoformat()
        metadata = dict(workflow.metadata)
        links = _workflow_memory_links(workflow)
        link_identity = (target_type, target_id, ref_type, ref_id, access_mode)
        existing = next((link for link in links if _memory_link_identity(link) == link_identity), None)
        created_link = existing is None
        if existing is None:
            existing = {
                "id": f"workflow-memory-link-{uuid4().hex[:12]}",
                "created_at": now,
                "created_by": current_user.id,
            }
            links.append(existing)
        existing.update(
            {
                "target_type": target_type,
                "target_id": target_id,
                "ref_type": ref_type,
                "ref_id": ref_id,
                "memory_ids": memory_ids,
                "access_mode": access_mode,
                "label": payload.label.strip() if isinstance(payload.label,
                                                             str) and payload.label.strip() else default_label,
                "updated_at": now,
                "updated_by": current_user.id,
            }
        )
        metadata[WORKFLOW_MEMORY_LINK_METADATA_KEY] = links
        updated = await context.workflow_repo.update(workflow_id, {"metadata": metadata})
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        await service.maybe_replace_active_executions_for_revision_change(
            before=workflow,
            after=updated,
            source="workflow_memory_link_update",
        )
        await _emit_workflow_memory_link_projection_event(
            context=context,
            workflow_id=workflow_id,
            event_type="workflow_memory_link.created" if created_link else "workflow_memory_link.updated",
            link=existing,
            user_id=current_user.id,
        )
        return {
            "workflow": updated.model_dump(mode="json"),
            "link": _serialize_workflow_memory_link(workflow_id, existing),
            "items": [
                _serialize_workflow_memory_link(workflow_id, link)
                for link in _workflow_memory_links(updated)
            ],
        }

    @router.delete("/{workflow_id}/memory-links/{link_id}", summary="Remove Workflow Memory Link")
    async def delete_workflow_memory_link(workflow_id: str, link_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        workflow = await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        links = _workflow_memory_links(workflow)
        deleted_link = next((link for link in links if link.get("id") == link_id), None)
        remaining = [link for link in links if link.get("id") != link_id]
        if len(remaining) == len(links):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Workflow memory link '{link_id}' not found")
        metadata = dict(workflow.metadata)
        metadata[WORKFLOW_MEMORY_LINK_METADATA_KEY] = remaining
        updated = await context.workflow_repo.update(workflow_id, {"metadata": metadata})
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        await service.maybe_replace_active_executions_for_revision_change(
            before=workflow,
            after=updated,
            source="workflow_memory_link_delete",
        )
        if deleted_link is not None:
            await _emit_workflow_memory_link_projection_event(
                context=context,
                workflow_id=workflow_id,
                event_type="workflow_memory_link.deleted",
                link=deleted_link,
                user_id=current_user.id,
            )
        return {"deleted": True, "workflowId": workflow_id, "linkId": link_id}

    @router.get("/{workflow_id}", summary="Get Workflow By Id")
    async def get_workflow(workflow_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["workflows:read"])
        item = await context.workflow_repo.get(workflow_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        return _workflow_response_payload(
            item,
            context,
            main_agent_default_workflow_id=await service.main_agent_default_workflow_id(),
        )

    @router.get("/{workflow_id}/persona-version-notices", summary="List Workflow Persona Version Notices")
    async def list_workflow_persona_version_notices(workflow_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["workflows:read", "personas:read"])
        try:
            return await service.workflow_persona_version_notices(workflow_id)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/{workflow_id}/persona-agents/{agent_id}/use-latest", summary="Use Latest Persona Agent In Workflow")
    async def use_latest_persona_agent(workflow_id: str, agent_id: str, request: Request):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context,
                                                  required_scopes=["workflows:write", "personas:read"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.use_latest_persona_agent(
                workflow_id,
                agent_id,
                updated_by_user_id=current_user.id,
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("/{workflow_id}/persona-agents/{agent_id}/keep-current",
                 summary="Keep Workflow Persona Agent Snapshot")
    async def keep_persona_agent_version(workflow_id: str, agent_id: str, request: Request):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context,
                                                  required_scopes=["workflows:write", "personas:read"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.keep_persona_agent_version(
                workflow_id,
                agent_id,
                updated_by_user_id=current_user.id,
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("/{workflow_id}/agents/{agent_id}/promote", summary="Promote Workflow Agent To Global Catalog")
    async def promote_workflow_agent(
            workflow_id: str,
            agent_id: str,
            payload: WorkflowAgentPromotionRequest,
            request: Request,
    ):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write", "agents:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.promote_workflow_agent(
                workflow_id,
                agent_id,
                global_agent_id=payload.global_agent_id,
                replace_workflow_agent=payload.replace_workflow_agent,
                promoted_by_user_id=current_user.id,
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except WorkflowAgentPromotionConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.get("/{workflow_id}/versions", summary="List Workflow Versions")
    async def list_workflow_versions(workflow_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["workflows:read"])
        try:
            return await service.list_workflow_versions(workflow_id)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/{workflow_id}/versions/{revision}", summary="Get Workflow Version")
    async def get_workflow_version(workflow_id: str, revision: int, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["workflows:read"])
        try:
            return await service.get_workflow_version(workflow_id, revision)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.put("/{workflow_id}", summary="Update Workflow")
    async def update_workflow(workflow_id: str, patch: dict[str, Any], request: Request):
        existing = await context.workflow_repo.get(workflow_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, existing, current_user, context)
        item = await context.workflow_repo.update(workflow_id, patch)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        await service.maybe_replace_active_executions_for_revision_change(
            before=existing,
            after=item,
            source="workflow_update",
        )
        return item.model_dump(mode="json")

    @router.delete("/{workflow_id}", summary="Soft Delete Workflow")
    async def delete_workflow(workflow_id: str, request: Request):
        existing = await context.workflow_repo.get(workflow_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, existing, current_user, context)
        deleted = await context.workflow_repo.soft_delete(workflow_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        return {"deleted": True, "id": workflow_id}

    @router.get("/{workflow_id}/owners", summary="List Workflow Owners")
    async def list_workflow_owners(workflow_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:read"])
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        workflow = await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        owners = []
        for owner_id in _owner_ids(workflow):
            owner = await context.user_repo.get(owner_id)
            if owner is not None:
                owners.append(owner)
        return {"items": [owner.model_dump(mode="json") for owner in owners]}

    @router.post("/{workflow_id}/owners", summary="Add Workflow Owners")
    async def add_workflow_owners(workflow_id: str, request: Request, payload: Any = Body(...)):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        workflow = await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        owner_ids = _parse_owner_ids(payload)
        metadata = dict(workflow.metadata)
        next_owner_ids = list(dict.fromkeys([*_owner_ids(workflow), *owner_ids]))
        metadata["owner_ids"] = next_owner_ids
        updated = await context.workflow_repo.update(workflow_id, {"metadata": metadata})
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        return {"owner_ids": next_owner_ids, "workflow": updated.model_dump(mode="json")}

    @router.delete("/{workflow_id}/owners", summary="Remove Workflow Owner")
    async def remove_workflow_owner(workflow_id: str, request: Request, payload: dict[str, Any] = Body(...)):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        workflow = await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        owner_id = payload.get("user_id") or payload.get("owner_id")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="owner_id is required")
        metadata = dict(workflow.metadata)
        metadata["owner_ids"] = [item for item in _owner_ids(workflow) if item != owner_id]
        updated = await context.workflow_repo.update(workflow_id, {"metadata": metadata})
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        return {"owner_ids": metadata["owner_ids"], "workflow": updated.model_dump(mode="json")}

    @router.post("/{workflow_id}/publish", summary="Publish Workflow Version")
    async def publish_workflow(workflow_id: str, request: Request, payload: dict[str, Any] | None = None):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.publish_workflow(workflow_id, payload)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/{workflow_id}/unpublish", summary="Unpublish Workflow Version")
    async def unpublish_workflow(workflow_id: str, request: Request, payload: dict[str, Any] | None = None):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.unpublish_workflow(workflow_id, payload)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/{workflow_id}/clone", summary="Clone Workflow")
    async def clone_workflow(workflow_id: str, request: Request):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        workflow = await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        cloned = _clone_workflow_definition(workflow, current_user)
        created = await context.workflow_repo.create(cloned)
        return created.model_dump(mode="json")

    @router.post("/validate", summary="Validate Workflow")
    async def validate_workflow(payload: dict[str, Any] = Body(...), request: Request = None):
        await resolve_current_user_if_present(request, context, required_scopes=["workflows:write"])
        workflow = _workflow_validate_payload(payload)
        return await service.validate_workflow(workflow)

    @router.get("/{workflow_id}/executions", summary="List Executions For Workflow")
    async def list_workflow_executions(workflow_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["executions:read"])
        return await service.list_workflow_executions(workflow_id)

    @router.get("/{workflow_id}/monitoring", summary="Get Workflow Monitoring Operator Controls")
    async def get_workflow_monitoring(workflow_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["workflows:read"])
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        return service.monitoring_operator_payload(
            workflow,
            main_agent_default_workflow_id=await service.main_agent_default_workflow_id(),
        )

    @router.patch("/{workflow_id}/monitoring", summary="Update Workflow Monitoring Operator Controls")
    async def update_workflow_monitoring(workflow_id: str, patch: dict[str, Any], request: Request):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.update_monitoring_controls(workflow_id, patch)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.get("/{workflow_id}/runtime-governance", summary="Get Workflow Runtime Governance Controls")
    async def get_workflow_runtime_governance(workflow_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["workflows:read"])
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        return service.runtime_governance_operator_payload(workflow)

    @router.patch("/{workflow_id}/runtime-governance", summary="Update Workflow Runtime Governance Controls")
    async def update_workflow_runtime_governance(
            workflow_id: str,
            patch: WorkflowRuntimeGovernancePatch,
            request: Request,
    ):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.update_runtime_governance_controls(
                workflow_id,
                patch.model_dump(exclude_unset=True),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.get("/{workflow_id}/monitoring/events", summary="List Workflow Monitor Findings And Proposals")
    async def list_workflow_monitoring_events(workflow_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["executions:read"])
        try:
            return await service.workflow_monitoring_events(workflow_id)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/{workflow_id}/steering-approvals", summary="Create Workflow Steering Approval")
    async def create_workflow_steering_approval_route(
            workflow_id: str,
            payload: WorkflowSteeringApprovalRequest,
            request: Request,
    ):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            payload_dict = payload.model_dump(exclude_unset=True)
            request_approval = bool(payload_dict.pop("request_approval", True))
            payload_dict["created_by"] = current_user.id
            created = await service.create_workflow_steering_approval(workflow_id, payload_dict)
            if not request_approval:
                return created
            approval_id = str(created["approval"]["id"])
            requested = await service.request_workflow_steering_approval(
                workflow_id,
                approval_id,
                actor_user_id=current_user.id,
            )
            return {
                **requested,
                "workflow": requested.get("workflow") or created["workflow"],
                "approval": requested.get("approval") or created["approval"],
            }
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post(
        "/{workflow_id}/monitoring/proposals/{proposal_event_id}/dispatch",
        summary="Send Workflow Monitor Proposal To Main Agent",
    )
    async def dispatch_workflow_monitor_proposal(
            workflow_id: str,
            proposal_event_id: str,
            request: Request,
    ):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            payload = await request.json() if request.headers.get("content-length") not in {None, "0"} else {}
            return await service.dispatch_monitor_proposal_to_main_agent(
                workflow_id,
                proposal_event_id,
                actor_user_id=current_user.id,
                operator_note=payload.get("operator_note") if isinstance(payload, dict) else None,
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.get("/{workflow_id}/governance/review-queue", summary="Get Workflow Governance Review Queue")
    async def get_workflow_governance_review_queue(
            workflow_id: str,
            request: Request,
            limit: int | None = Query(default=None, ge=1, le=100),
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["workflows:read"])
        try:
            return await service.workflow_governance_review_queue(workflow_id, limit=limit)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/{workflow_id}/governance/document-suggest", summary="Suggest Workflow Governance Documents")
    async def suggest_workflow_governance_documents(
            workflow_id: str,
            request: Request,
            record_kind: str = Query(...),
            record_id: str = Query(...),
            limit: int | None = Query(default=None, ge=1, le=20),
    ):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:read"])
        try:
            return await service.suggest_workflow_governance_documents(
                workflow_id,
                actor_user_id=current_user.id,
                record_kind=record_kind,
                record_id=record_id,
                limit=limit,
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post("/{workflow_id}/governance/bundle/{record_kind}/{record_id}",
                 summary="Execute Workflow Governance Bundle")
    async def execute_workflow_governance_bundle(
            workflow_id: str,
            record_kind: str,
            record_id: str,
            payload: WorkflowGovernanceBundleRequest,
            request: Request,
    ):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.execute_workflow_governance_bundle(
                workflow_id,
                actor_user_id=current_user.id,
                record_kind=record_kind,
                record_id=record_id,
                **payload.model_dump(exclude_unset=True),
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post(
        "/{workflow_id}/governance/action/{record_kind}/{record_id}/attach-evidence",
        summary="Attach Evidence To Workflow Governance Record",
    )
    async def attach_workflow_governance_evidence(
            workflow_id: str,
            record_kind: str,
            record_id: str,
            payload: WorkflowGovernanceAttachEvidenceRequest,
            request: Request,
    ):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.execute_workflow_governance_action(
                workflow_id,
                action="attach_evidence",
                actor_user_id=current_user.id,
                record_kind=record_kind,
                record_id=record_id,
                **payload.model_dump(exclude_unset=True),
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post(
        "/{workflow_id}/governance/action/{record_kind}/{record_id}/request-approval",
        summary="Request Approval For Workflow Governance Record",
    )
    async def request_workflow_governance_record_approval(
            workflow_id: str,
            record_kind: str,
            record_id: str,
            request: Request,
    ):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.execute_workflow_governance_action(
                workflow_id,
                action="request_approval",
                actor_user_id=current_user.id,
                record_kind=record_kind,
                record_id=record_id,
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post(
        "/{workflow_id}/governance/action/{record_kind}/{record_id}/resolve",
        summary="Resolve Workflow Governance Record",
    )
    async def resolve_workflow_governance_record(
            workflow_id: str,
            record_kind: str,
            record_id: str,
            request: Request,
    ):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.execute_workflow_governance_action(
                workflow_id,
                action="resolve",
                actor_user_id=current_user.id,
                record_kind=record_kind,
                record_id=record_id,
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post(
        "/{workflow_id}/governance/action/{record_kind}/{record_id}/dismiss",
        summary="Dismiss Workflow Governance Record",
    )
    async def dismiss_workflow_governance_record(
            workflow_id: str,
            record_kind: str,
            record_id: str,
            request: Request,
    ):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.execute_workflow_governance_action(
                workflow_id,
                action="dismiss",
                actor_user_id=current_user.id,
                record_kind=record_kind,
                record_id=record_id,
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post(
        "/{workflow_id}/governance/action/{record_kind}/{record_id}/reopen",
        summary="Reopen Workflow Governance Record",
    )
    async def reopen_workflow_governance_record(
            workflow_id: str,
            record_kind: str,
            record_id: str,
            request: Request,
    ):
        workflow = await context.workflow_repo.get(workflow_id)
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Workflow '{workflow_id}' not found")
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        await _ensure_owner_or_admin(workflow_id, workflow, current_user, context)
        try:
            return await service.execute_workflow_governance_action(
                workflow_id,
                action="reopen",
                actor_user_id=current_user.id,
                record_kind=record_kind,
                record_id=record_id,
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post("/{workflow_id}/stale-executions/repair", summary="Repair Stale Executions For Workflow")
    async def repair_stale_workflow_executions(workflow_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["executions:write"])
        try:
            return await service.repair_stale_workflow_executions(workflow_id)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return router
