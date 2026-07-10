from __future__ import annotations

import hashlib
import httpx
from copy import deepcopy
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, status
from ipaddress import ip_address
from pydantic import BaseModel, Field
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import uuid4

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.domain import UserDefinition, WorkflowDefinition
from app.services.workflow_validation import WorkflowValidationService
from app.tools.risk import ordered_risk_labels, risk_labels_for_tool_definition

MAX_MARKETPLACE_WORKFLOW_BYTES = 1_000_000


class MarketplaceWorkflowImportRequest(BaseModel):
    workflow: dict[str, Any] | None = None
    source_url: str | None = None
    source_id: str | None = None
    source_version: str | None = None
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    approve_high_risk: bool = False


class MarketplaceWorkflowPreview(BaseModel):
    workflow: dict[str, Any]
    validation_errors: list[dict[str, Any]]
    validation_warnings: list[dict[str, Any]]
    risk_labels: list[str]
    high_risk: bool
    high_risk_labels: list[str]
    requires_import_approval: bool
    source_id: str | None = None
    source_version: str | None = None
    source_sha256: str | None = None


def _marketplace_status(workflow: WorkflowDefinition) -> str:
    value = workflow.metadata.get("marketplace_status")
    return value if isinstance(value, str) else "draft"


def _is_marketplace_visible(workflow: WorkflowDefinition) -> bool:
    status_value = _marketplace_status(workflow)
    return status_value == "approved" or workflow.versioning.is_published


def _clone_workflow_definition(source: WorkflowDefinition) -> WorkflowDefinition:
    payload = deepcopy(source.model_dump(mode="json"))
    payload["id"] = f"workflow-{uuid4().hex[:12]}"
    payload["name"] = f"[CLONE] {source.name}"
    metadata = dict(source.metadata)
    metadata["marketplace_status"] = "draft"
    metadata["cloned_from_workflow_id"] = source.id
    payload["metadata"] = metadata
    versioning = dict(payload.get("versioning") or {})
    versioning["revision"] = 1
    versioning["version"] = "1.0.0"
    versioning["is_published"] = False
    payload["versioning"] = versioning
    return WorkflowDefinition.model_validate(payload)


def _require_public_https_url(source_url: str) -> None:
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Marketplace source_url must be an HTTPS URL",
        )
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Marketplace source_url cannot target local hosts",
        )
    try:
        host_ip = ip_address(hostname)
    except ValueError:
        return
    if host_ip.is_private or host_ip.is_loopback or host_ip.is_link_local or host_ip.is_multicast:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Marketplace source_url cannot target private or local IP addresses",
        )


def _extract_workflow_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("workflow"), dict):
        return payload["workflow"]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict) and isinstance(
            payload["data"].get("workflow"), dict):
        return payload["data"]["workflow"]
    if isinstance(payload, dict) and {"name", "entrypoint"}.issubset(payload.keys()):
        return payload
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Marketplace payload must contain a workflow definition",
    )


def _remote_source_value(payload: Any, key: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    source = payload.get("source")
    if isinstance(source, dict):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _fetch_marketplace_workflow_payload(
        request_payload: MarketplaceWorkflowImportRequest,
) -> tuple[dict[str, Any], str | None, str | None, str | None]:
    if request_payload.workflow is not None:
        return (
            request_payload.workflow,
            request_payload.source_id,
            request_payload.source_version,
            None,
        )
    if not request_payload.source_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either workflow or source_url is required",
        )

    _require_public_https_url(request_payload.source_url)

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(request_payload.source_url, headers={"Accept": "application/json"})
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Marketplace source returned HTTP {exc.response.status_code}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Marketplace source could not be fetched",
        ) from exc

    content = response.content
    if len(content) > MAX_MARKETPLACE_WORKFLOW_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Marketplace workflow payload is too large",
        )

    source_sha256 = hashlib.sha256(content).hexdigest()
    if request_payload.expected_sha256 and source_sha256.lower() != request_payload.expected_sha256.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Marketplace workflow checksum mismatch",
        )

    try:
        remote_payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Marketplace source did not return JSON",
        ) from exc

    return (
        _extract_workflow_payload(remote_payload),
        request_payload.source_id or _remote_source_value(remote_payload, "source_id") or _remote_source_value(
            remote_payload, "id"),
        request_payload.source_version or _remote_source_value(remote_payload,
                                                               "source_version") or _remote_source_value(remote_payload,
                                                                                                         "version"),
        source_sha256,
    )


def _assign_import_provenance(
        workflow: WorkflowDefinition,
        user: UserDefinition,
        *,
        source_url: str | None,
        source_id: str | None,
        source_version: str | None,
        source_sha256: str | None,
) -> WorkflowDefinition:
    source_workflow_id = workflow.id
    payload = deepcopy(workflow.model_dump(mode="json"))
    payload["id"] = f"workflow-{uuid4().hex[:12]}"
    metadata = dict(payload.get("metadata") or {})
    metadata.update(
        {
            "created_by": user.id,
            "owner_ids": [user.id],
            "marketplace_status": "draft",
            "marketplace_imported_at": datetime.now(timezone.utc).isoformat(),
            "marketplace_imported_by": user.id,
            "marketplace_source_workflow_id": source_workflow_id,
            "marketplace_untrusted_until_reviewed": True,
        }
    )
    if source_url:
        metadata["marketplace_source_url"] = source_url
    if source_id:
        metadata["marketplace_source_id"] = source_id
    if source_version:
        metadata["marketplace_source_version"] = source_version
    if source_sha256:
        metadata["marketplace_source_sha256"] = source_sha256
    payload["metadata"] = metadata
    versioning = dict(payload.get("versioning") or {})
    versioning["revision"] = 1
    versioning["is_published"] = False
    payload["versioning"] = versioning
    return WorkflowDefinition.model_validate(payload)


HIGH_RISK_IMPORT_LABELS = {
    "shell",
    "filesystem",
    "browser",
    "network",
    "mcp",
    "credentials",
    "dangerous",
    "local_privileged_execution",
}


def _workflow_risk_metadata(workflow: WorkflowDefinition) -> dict[str, Any]:
    labels: set[str] = set()
    for tool in workflow.tool_definitions:
        labels.update(risk_labels_for_tool_definition(tool))
    if any(task.human_approval_required for task in workflow.task_definitions):
        labels.add("requires_approval")
    if any(node.node_type.value == "approval" for node in workflow.nodes):
        labels.add("requires_approval")
    if workflow.metadata.get("protected_execution"):
        labels.update({"requires_approval", "mutation"})
    ordered_labels = ordered_risk_labels(labels)
    high_risk_labels = [label for label in ordered_labels if label in HIGH_RISK_IMPORT_LABELS]
    return {
        "risk_labels": ordered_labels,
        "high_risk": bool(high_risk_labels),
        "high_risk_labels": high_risk_labels,
        "requires_import_approval": bool(high_risk_labels),
    }


async def _preview_marketplace_workflow(
        payload: MarketplaceWorkflowImportRequest,
        context: ApiContext,
) -> tuple[WorkflowDefinition, MarketplaceWorkflowPreview]:
    workflow_payload, source_id, source_version, source_sha256 = await _fetch_marketplace_workflow_payload(payload)
    try:
        workflow = WorkflowDefinition.model_validate(workflow_payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Marketplace workflow definition is invalid", "errors": str(exc)},
        ) from exc

    validation = await WorkflowValidationService(context).validate(workflow)
    risk_metadata = _workflow_risk_metadata(workflow)
    preview = MarketplaceWorkflowPreview(
        workflow=workflow.model_dump(mode="json"),
        validation_errors=validation.validation_errors,
        validation_warnings=validation.validation_warnings,
        source_id=source_id,
        source_version=source_version,
        source_sha256=source_sha256,
        **risk_metadata,
    )
    return workflow, preview


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


def _assign_clone_owner(cloned: WorkflowDefinition, source: WorkflowDefinition,
                        user: UserDefinition) -> WorkflowDefinition:
    payload = cloned.model_dump(mode="json")
    metadata = dict(payload.get("metadata") or {})
    metadata.update(
        {
            "owner_ids": [user.id],
            "created_by": user.id,
            "cloned_by": user.id,
            "cloned_from_workflow_id": source.id,
            "marketplace_imported_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    payload["metadata"] = metadata
    return WorkflowDefinition.model_validate(payload)


def create_marketplace_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/marketplace/workflows", tags=["Marketplace"])

    @router.get("", summary="List Marketplace Workflows")
    async def list_marketplace_workflows():
        items = await context.workflow_repo.list()
        visible = [item for item in items if _is_marketplace_visible(item)]
        return {"items": [item.model_dump(mode="json") for item in visible]}

    @router.get("/{workflow_id}", summary="Get Marketplace Workflow")
    async def get_marketplace_workflow(workflow_id: str):
        item = await context.workflow_repo.get(workflow_id)
        if item is None or not _is_marketplace_visible(item):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Marketplace workflow '{workflow_id}' not found",
            )
        return item.model_dump(mode="json")

    @router.post("/{workflow_id}/submit", summary="Submit Workflow To Marketplace")
    async def submit_marketplace_workflow(workflow_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        item = await context.workflow_repo.get(workflow_id)
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found",
            )
        _require_owner_or_admin(item, current_user)
        metadata = dict(item.metadata)
        metadata["marketplace_status"] = "pending"
        metadata["marketplace_submitted_by"] = current_user.id
        metadata["marketplace_submitted_at"] = datetime.now(timezone.utc).isoformat()
        updated = await context.workflow_repo.update(workflow_id, {"metadata": metadata})
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found",
            )
        return updated.model_dump(mode="json")

    @router.post("/{workflow_id}/clone", summary="Clone Marketplace Workflow")
    async def clone_marketplace_workflow(workflow_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        item = await context.workflow_repo.get(workflow_id)
        if item is None or not _is_marketplace_visible(item):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Marketplace workflow '{workflow_id}' not found",
            )
        cloned = _assign_clone_owner(_clone_workflow_definition(item), item, current_user)
        created = await context.workflow_repo.create(cloned)
        return created.model_dump(mode="json")

    @router.post("/preview", summary="Preview Marketplace Workflow Import")
    async def preview_marketplace_workflow(payload: MarketplaceWorkflowImportRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["workflows:write"])
        _, preview = await _preview_marketplace_workflow(payload, context)
        return preview.model_dump(mode="json")

    @router.post("/import", summary="Import Marketplace Workflow")
    async def import_marketplace_workflow(payload: MarketplaceWorkflowImportRequest, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        workflow, preview = await _preview_marketplace_workflow(payload, context)
        if preview.validation_errors:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "Marketplace workflow failed validation",
                    "validation_errors": preview.validation_errors,
                    "validation_warnings": preview.validation_warnings,
                },
            )
        if preview.requires_import_approval and not payload.approve_high_risk:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "Marketplace workflow requires explicit high-risk import approval",
                    "risk_labels": preview.risk_labels,
                    "high_risk_labels": preview.high_risk_labels,
                    "preview": preview.model_dump(mode="json"),
                },
            )

        imported = _assign_import_provenance(
            workflow,
            current_user,
            source_url=payload.source_url,
            source_id=preview.source_id,
            source_version=preview.source_version,
            source_sha256=preview.source_sha256,
        )
        created = await context.workflow_repo.create(imported)
        return {
            "workflow": created.model_dump(mode="json"),
            "validation_warnings": preview.validation_warnings,
            "risk_labels": preview.risk_labels,
            "high_risk": preview.high_risk,
            "high_risk_labels": preview.high_risk_labels,
        }

    @router.post("/{workflow_id}/approve", summary="Approve Workflow For Marketplace")
    async def approve_marketplace_workflow(workflow_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        if "admin" not in current_user.roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access is required")
        item = await context.workflow_repo.get(workflow_id)
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found",
            )
        metadata = dict(item.metadata)
        metadata["marketplace_status"] = "approved"
        metadata["marketplace_reviewed_by"] = current_user.id
        metadata["marketplace_reviewed_at"] = datetime.now(timezone.utc).isoformat()
        updated = await context.workflow_repo.update(workflow_id, {"metadata": metadata})
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found",
            )
        return updated.model_dump(mode="json")

    @router.post("/{workflow_id}/reject", summary="Reject Workflow From Marketplace")
    async def reject_marketplace_workflow(workflow_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["workflows:write"])
        if "admin" not in current_user.roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access is required")
        item = await context.workflow_repo.get(workflow_id)
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found",
            )
        metadata = dict(item.metadata)
        metadata["marketplace_status"] = "rejected"
        metadata["marketplace_reviewed_by"] = current_user.id
        metadata["marketplace_reviewed_at"] = datetime.now(timezone.utc).isoformat()
        updated = await context.workflow_repo.update(workflow_id, {"metadata": metadata})
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found",
            )
        return updated.model_dump(mode="json")

    return router
