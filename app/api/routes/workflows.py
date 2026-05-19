from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from fastapi import APIRouter, Body, HTTPException, Request, status
from pydantic import BaseModel, Field
from typing import Any, Optional
from uuid import uuid4

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user, resolve_current_user_if_present
from app.domain import UserDefinition, WorkflowDefinition
from app.runtime.native.errors import WorkflowNotFoundError
from app.services import WorkflowService


SHARED_MEMORY_LIMIT_KEYS = {
    "decisions",
    "commitments",
    "facts_and_preferences",
    "recent_summaries",
    "semantic_fallback",
}


class WorkflowSharedMemoryPatch(BaseModel):
    enabled: bool | None = None
    limit_per_layer: dict[str, int] | None = None
    apply_to_agents: bool = False
    agent_scope: str = Field(default="workflow")


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
    return payload


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
    async def validate_workflow(payload: WorkflowDefinition, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["workflows:write"])
        return await service.validate_workflow(payload)

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

    @router.get("/{workflow_id}/monitoring/events", summary="List Workflow Monitor Findings And Proposals")
    async def list_workflow_monitoring_events(workflow_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["executions:read"])
        try:
            return await service.workflow_monitoring_events(workflow_id)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/{workflow_id}/stale-executions/repair", summary="Repair Stale Executions For Workflow")
    async def repair_stale_workflow_executions(workflow_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["executions:write"])
        try:
            return await service.repair_stale_workflow_executions(workflow_id)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return router
