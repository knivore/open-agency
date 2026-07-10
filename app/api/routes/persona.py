"""Persona catalog and factory routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from typing import Any, Literal, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user, resolve_current_user_if_present
from app.domain import PersonaMemoryLayer, PersonaDistillationItemType, PersonaSource, PersonaSourceType
from app.graph.neo4j_read import Neo4jGraphReadError
from app.graph.service import GraphReadUnavailableError
from app.runtime.native.errors import WorkflowNotFoundError
from app.services.persona_factory import PersonaDistillationError, PersonaFactoryService, PersonaPublishError
from app.services.persona_graph_context import PersonaGraphContextService
from app.services.personas import PersonaConflictError, PersonaNotFoundError, PersonaService
from app.services.workflows import WorkflowService


class PersonaCreateRequest(BaseModel):
    name: str
    slug: str | None = None
    description: str | None = None
    workspace_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonaUpdateRequest(BaseModel):
    patch: dict[str, Any] = Field(default_factory=dict)


class PersonaImportRequest(BaseModel):
    name: str | None = None
    slug: str | None = None
    description: str | None = None
    version: str | None = None
    workspace_id: str | None = None
    format: str = "skill_markdown"
    files: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonaSourceCreateRequest(BaseModel):
    source_type: PersonaSourceType
    source_id: str | None = None
    filename: str | None = None
    content_sha256: str | None = None
    storage_uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonaDistillRequest(BaseModel):
    persona_id: str | None = None
    name: str | None = None
    description: str | None = None
    source_memory_ids: list[str] = Field(default_factory=list)
    distillation_mode: Literal["deterministic", "llm", "hybrid"] | None = None
    llm_model_source: Literal["main_agent", "model_profile", "model"] | None = None
    model_profile_id: str | None = None
    llm_model_provider: str | None = None
    llm_model: str | None = None
    persona_type: str | None = None
    capability_mode: str | None = None
    consent_status: str | None = None
    source_basis: str | None = None
    sensitivity_level: str | None = None
    visibility: str | None = None

    def governance_labels(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "persona_type": self.persona_type,
                "capability_mode": self.capability_mode,
                "consent_status": self.consent_status,
                "source_basis": self.source_basis,
                "sensitivity_level": self.sensitivity_level,
                "visibility": self.visibility,
            }.items()
            if isinstance(value, str) and value.strip()
        }


class PersonaPackagePatchRequest(BaseModel):
    package: dict[str, Any]


class PersonaSynthesizePackageRequest(BaseModel):
    package_synthesis_mode: Literal["reviewed_items", "llm_polished"] = "reviewed_items"
    llm_polishing_model_profile_id: str | None = None


class PersonaDistillationItemPatchRequest(BaseModel):
    patch: dict[str, Any] = Field(default_factory=dict)


class PersonaApproveRequest(BaseModel):
    version: str | None = None


class PersonaRejectItemRequest(BaseModel):
    reason: str | None = None


class PersonaBulkReviewItemsRequest(BaseModel):
    item_ids: list[str] = Field(default_factory=list)
    action: Literal["approve", "reject"]
    reason: str | None = None


class PersonaRunItemBulkReviewFilters(BaseModel):
    source_key: str | None = None
    item_type: str | None = None
    memory_layer: str | None = None
    review_status: str | None = None
    needs_review: bool | None = None
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    max_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    extraction_source: str | None = None
    distiller: str | None = None
    review_flag: str | None = None
    conflict_group_id: str | None = None


class PersonaRunBulkReviewItemsRequest(BaseModel):
    action: Literal["approve", "reject"]
    reason: str | None = None
    filters: PersonaRunItemBulkReviewFilters = Field(default_factory=PersonaRunItemBulkReviewFilters)
    limit: int = Field(default=250, ge=1, le=250)


class PersonaRunReviewActionRequest(BaseModel):
    action: Literal["prefer_llm", "prefer_deterministic", "merge_manually", "mark_evidence_insufficient"]
    item_ids: list[str] = Field(default_factory=list)
    conflict_group_id: str | None = None
    reason: str | None = None
    patch: dict[str, Any] = Field(default_factory=dict)


class PersonaRunSourceClassificationRequest(BaseModel):
    classification: str | None = None
    document_kind: str | None = None
    content_roles: list[str] | None = None
    extraction_targets: list[str] | None = None
    memory_layers: list[str] | None = None
    vector_tags: list[str] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str | None = None


class PersonaRunSourceRedistillRequest(BaseModel):
    limit: int = Field(default=250, ge=1, le=250)


class PersonaFeedbackRequest(BaseModel):
    persona_id: str
    title: str | None = None
    content: str
    item_type: PersonaDistillationItemType = PersonaDistillationItemType.DOMAIN_KNOWLEDGE
    memory_layer: PersonaMemoryLayer = PersonaMemoryLayer.SEMANTIC
    feedback_type: str = "correction"
    confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    source_memory_id: str | None = None
    accepted_edit_of_item_id: str | None = None
    source_conversation_id: str | None = None
    source_message_id: str | None = None
    source_run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def create_persona_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(tags=["Personas"])

    @router.get("/persona", summary="List Personas")
    async def list_personas(request: Request, include_archived: bool = False):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        items = await PersonaService(context).list_personas(include_archived=include_archived)
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.post("/persona", summary="Create Persona")
    async def create_persona(payload: PersonaCreateRequest, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            persona = await PersonaService(context).create_persona(payload.model_dump(), current_user=current_user)
        except PersonaConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return persona.model_dump(mode="json")

    @router.post("/persona/import", summary="Import Persona Package")
    async def import_persona(payload: PersonaImportRequest, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            return await PersonaService(context).import_persona(payload.model_dump(), current_user=current_user)
        except PersonaConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except PersonaNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.get("/persona/{persona_id}", summary="Get Persona")
    async def get_persona(persona_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        persona = await PersonaService(context).get_persona(persona_id)
        if persona is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Persona '{persona_id}' not found")
        return persona.model_dump(mode="json")

    @router.get("/persona/{persona_id}/workflow-usages", summary="List Workflows Using Persona")
    async def list_persona_workflow_usages(persona_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read", "workflows:read"])
        try:
            return await WorkflowService(context).persona_workflow_usages(persona_id)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.patch("/persona/{persona_id}", summary="Update Persona")
    async def update_persona(persona_id: str, payload: PersonaUpdateRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            persona = await PersonaService(context).update_persona(persona_id, payload.patch)
        except PersonaConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        if persona is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Persona '{persona_id}' not found")
        return persona.model_dump(mode="json")

    @router.delete("/persona/{persona_id}", summary="Archive Persona")
    async def archive_persona(persona_id: str, request: Request):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        deleted = await PersonaService(context).archive_persona(persona_id)
        return {"deleted": deleted, "id": persona_id}

    @router.get("/persona/{persona_id}/versions", summary="List Persona Versions")
    async def list_persona_versions(persona_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        if await context.persona_repo.get(persona_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Persona '{persona_id}' not found")
        return {"items": await PersonaService(context).list_versions(persona_id)}

    @router.post("/persona/{persona_id}/versions/{version_id}/rollback", summary="Rollback Persona Version")
    async def rollback_persona_version(persona_id: str, version_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            return await PersonaFactoryService(context).rollback_to_version(
                persona_id=persona_id,
                version_id=version_id,
                current_user=current_user,
            )
        except PersonaNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except (PersonaDistillationError, PersonaPublishError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.get("/persona/{persona_id}/sources", summary="List Persona Sources")
    async def list_persona_sources(persona_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        if await context.persona_repo.get(persona_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Persona '{persona_id}' not found")
        return {"items": await PersonaService(context).list_sources(persona_id)}

    @router.get("/persona/{persona_id}/export", summary="Export Persona Package")
    async def export_persona(
            persona_id: str,
            request: Request,
            format: str = Query(default="json",
                                pattern="^(json|persona_json|persona_package_json|skill_markdown|markdown|skill)$"),
            version_id: str | None = None,
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        try:
            return await PersonaService(context).export_persona(
                persona_id,
                version_id=version_id,
                export_format=format,
            )
        except PersonaNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/persona/{persona_id}/graph-context", summary="Get Persona Graph Context")
    async def get_persona_graph_context(
            persona_id: str,
            request: Request,
            query: str | None = None,
            preset: str = Query(
                default="persona_lineage",
                pattern="^(persona_lineage|persona-lineage|persona_capability_map|persona-capability-map)$",
            ),
            limit: int = Query(default=24, ge=1, le=100),
    ):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        try:
            return await PersonaGraphContextService(context).build_context(
                persona_id,
                query=query,
                preset=preset,
                limit=limit,
                current_user=current_user,
            )
        except PersonaNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except GraphReadUnavailableError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        except Neo4jGraphReadError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post("/persona/{persona_id}/sources", summary="Add Persona Source")
    async def create_persona_source(persona_id: str, payload: PersonaSourceCreateRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        if await context.persona_repo.get(persona_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Persona '{persona_id}' not found")
        source = await context.persona_source_repo.create(
            PersonaSource.model_validate({"persona_id": persona_id, **payload.model_dump()})
        )
        return source.model_dump(mode="json")

    @router.get("/persona-factory/governance-labels", summary="Get Persona Governance Labels")
    async def get_persona_governance_labels(request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        return PersonaFactoryService.governance_label_catalog()

    @router.get("/persona-factory/item-types", summary="List Persona Distillation Item Types")
    async def get_persona_item_types(request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        return await PersonaFactoryService(context).item_catalog()

    @router.post("/persona-factory/distill", summary="Distill Persona Draft")
    async def distill_persona(payload: PersonaDistillRequest, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            return await PersonaFactoryService(context).distill_from_memories(
                persona_id=payload.persona_id,
                name=payload.name,
                description=payload.description,
                source_memory_ids=payload.source_memory_ids,
                distillation_mode=payload.distillation_mode,
                llm_model_source=payload.llm_model_source,
                model_profile_id=payload.model_profile_id,
                llm_model_provider=payload.llm_model_provider,
                llm_model=payload.llm_model,
                governance_labels=payload.governance_labels(),
                current_user=current_user,
            )
        except (PersonaDistillationError, PersonaNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        except PersonaConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.post("/persona-factory/feedback", summary="Capture Persona Feedback")
    async def capture_persona_feedback(payload: PersonaFeedbackRequest, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            return await PersonaFactoryService(context).capture_feedback(
                persona_id=payload.persona_id,
                title=payload.title,
                content=payload.content,
                item_type=payload.item_type.value,
                memory_layer=payload.memory_layer.value,
                feedback_type=payload.feedback_type,
                confidence=payload.confidence,
                source_memory_id=payload.source_memory_id,
                accepted_edit_of_item_id=payload.accepted_edit_of_item_id,
                source_conversation_id=payload.source_conversation_id,
                source_message_id=payload.source_message_id,
                source_run_id=payload.source_run_id,
                metadata=payload.metadata,
                current_user=current_user,
            )
        except PersonaNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except PersonaDistillationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.get("/persona-factory/runs", summary="List Persona Distillation Runs")
    async def list_persona_runs(
            request: Request,
            persona_id: str | None = None,
            status_filter: str | None = Query(default=None, alias="status"),
            created_by_user_id: str | None = None,
            workspace_id: str | None = None,
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        try:
            return await PersonaFactoryService(context).list_runs(
                persona_id=persona_id,
                status=status_filter,
                created_by_user_id=created_by_user_id,
                workspace_id=workspace_id,
            )
        except PersonaDistillationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.get("/persona-factory/runs/{run_id}", summary="Get Persona Distillation Run")
    async def get_persona_run(run_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        try:
            return await PersonaFactoryService(context).get_run(run_id)
        except PersonaDistillationError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/persona-factory/runs/{run_id}/review-summary", summary="Get Persona Distillation Review Summary")
    async def get_persona_run_review_summary(run_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        try:
            return await PersonaFactoryService(context).build_run_review_summary(run_id)
        except PersonaDistillationError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/persona-factory/runs/{run_id}/items", summary="List Persona Distillation Items")
    async def list_persona_run_items(
            run_id: str,
            request: Request,
            source_key: str | None = None,
            item_type: str | None = None,
            memory_layer: str | None = None,
            review_status: str | None = None,
            needs_review: bool | None = None,
            min_confidence: float | None = Query(default=None, ge=0.0, le=1.0),
            max_confidence: float | None = Query(default=None, ge=0.0, le=1.0),
            extraction_source: str | None = None,
            distiller: str | None = None,
            review_flag: str | None = None,
            conflict_group_id: str | None = None,
            limit: int = Query(default=100, ge=1, le=250),
            offset: int = Query(default=0, ge=0),
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        try:
            return await PersonaFactoryService(context).list_run_items(
                run_id,
                source_key=source_key,
                item_type=item_type,
                memory_layer=memory_layer,
                review_status=review_status,
                needs_review=needs_review,
                min_confidence=min_confidence,
                max_confidence=max_confidence,
                extraction_source=extraction_source,
                distiller=distiller,
                review_flag=review_flag,
                conflict_group_id=conflict_group_id,
                limit=limit,
                offset=offset,
            )
        except PersonaDistillationError as exc:
            response_status = (
                status.HTTP_404_NOT_FOUND
                if "not found" in str(exc).lower()
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc

    @router.get("/persona-factory/runs/{run_id}/source-map", summary="Map Persona Distillation Sources")
    async def get_persona_run_source_map(run_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        try:
            return await PersonaFactoryService(context).build_run_source_map(run_id)
        except PersonaDistillationError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/persona-factory/runs/{run_id}/sources/{source_key}", summary="Get Persona Distillation Source")
    async def get_persona_run_source(
            run_id: str,
            source_key: str,
            request: Request,
            item_type: str | None = None,
            memory_layer: str | None = None,
            review_status: str | None = None,
            needs_review: bool | None = None,
            min_confidence: float | None = Query(default=None, ge=0.0, le=1.0),
            max_confidence: float | None = Query(default=None, ge=0.0, le=1.0),
            extraction_source: str | None = None,
            distiller: str | None = None,
            review_flag: str | None = None,
            conflict_group_id: str | None = None,
            limit: int = Query(default=50, ge=1, le=250),
            offset: int = Query(default=0, ge=0),
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        try:
            return await PersonaFactoryService(context).get_run_source_detail(
                run_id,
                source_key,
                item_type=item_type,
                memory_layer=memory_layer,
                review_status=review_status,
                needs_review=needs_review,
                min_confidence=min_confidence,
                max_confidence=max_confidence,
                extraction_source=extraction_source,
                distiller=distiller,
                review_flag=review_flag,
                conflict_group_id=conflict_group_id,
                limit=limit,
                offset=offset,
            )
        except PersonaDistillationError as exc:
            response_status = (
                status.HTTP_404_NOT_FOUND
                if "not found" in str(exc).lower()
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc

    @router.patch(
        "/persona-factory/runs/{run_id}/sources/{source_key}/classification",
        summary="Update Persona Source Classification",
    )
    async def update_persona_run_source_classification(
            run_id: str,
            source_key: str,
            payload: PersonaRunSourceClassificationRequest,
            request: Request,
    ):
        current_user = await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            return await PersonaFactoryService(context).update_run_source_classification(
                run_id,
                source_key,
                classification=payload.classification,
                document_kind=payload.document_kind,
                content_roles=payload.content_roles,
                extraction_targets=payload.extraction_targets,
                memory_layers=payload.memory_layers,
                vector_tags=payload.vector_tags,
                confidence=payload.confidence,
                rationale=payload.rationale,
                current_user=current_user,
            )
        except PersonaDistillationError as exc:
            response_status = (
                status.HTTP_404_NOT_FOUND
                if "not found" in str(exc).lower()
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc

    @router.post(
        "/persona-factory/runs/{run_id}/sources/{source_key}/redistill",
        summary="Re-Distill Persona Source",
    )
    async def redistill_persona_run_source(
            run_id: str,
            source_key: str,
            payload: PersonaRunSourceRedistillRequest,
            request: Request,
    ):
        current_user = await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            return await PersonaFactoryService(context).redistill_run_source(
                run_id,
                source_key,
                limit=payload.limit,
                current_user=current_user,
            )
        except PersonaNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except PersonaDistillationError as exc:
            response_status = (
                status.HTTP_404_NOT_FOUND
                if "not found" in str(exc).lower()
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc

    @router.patch("/persona-factory/items/{item_id}", summary="Update Persona Distillation Item")
    async def update_persona_item(item_id: str, payload: PersonaDistillationItemPatchRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            item = await PersonaFactoryService(context).update_item(item_id, payload.patch)
        except PersonaDistillationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return item.model_dump(mode="json")

    @router.post("/persona-factory/items/{item_id}/approve", summary="Approve Persona Distillation Item")
    async def approve_persona_item(item_id: str, request: Request):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            item = await PersonaFactoryService(context).approve_item(item_id)
        except PersonaDistillationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return item.model_dump(mode="json")

    @router.post("/persona-factory/items/{item_id}/reject", summary="Reject Persona Distillation Item")
    async def reject_persona_item(item_id: str, payload: PersonaRejectItemRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            item = await PersonaFactoryService(context).reject_item(item_id, reason=payload.reason)
        except PersonaDistillationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return item.model_dump(mode="json")

    @router.post("/persona-factory/items/bulk-review", summary="Bulk Review Persona Distillation Items")
    async def bulk_review_persona_items(payload: PersonaBulkReviewItemsRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            items = await PersonaFactoryService(context).bulk_review_items(
                item_ids=payload.item_ids,
                action=payload.action,
                reason=payload.reason,
            )
        except PersonaDistillationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return {
            "action": payload.action,
            "count": len(items),
            "items": [item.model_dump(mode="json") for item in items],
        }

    @router.post("/persona-factory/runs/{run_id}/items/bulk-review", summary="Bulk Review Filtered Persona Items")
    async def bulk_review_persona_run_items(run_id: str, payload: PersonaRunBulkReviewItemsRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            result = await PersonaFactoryService(context).bulk_review_run_items(
                run_id,
                action=payload.action,
                reason=payload.reason,
                source_key=payload.filters.source_key,
                item_type=payload.filters.item_type,
                memory_layer=payload.filters.memory_layer,
                review_status=payload.filters.review_status,
                needs_review=payload.filters.needs_review,
                min_confidence=payload.filters.min_confidence,
                max_confidence=payload.filters.max_confidence,
                extraction_source=payload.filters.extraction_source,
                distiller=payload.filters.distiller,
                review_flag=payload.filters.review_flag,
                conflict_group_id=payload.filters.conflict_group_id,
                limit=payload.limit,
            )
        except PersonaDistillationError as exc:
            response_status = (
                status.HTTP_404_NOT_FOUND
                if "not found" in str(exc).lower()
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc
        return {
            **result,
            "items": [item.model_dump(mode="json") for item in result["items"]],
        }

    @router.post(
        "/persona-factory/runs/{run_id}/items/bulk-review/preview",
        summary="Preview Filtered Persona Item Bulk Review",
    )
    async def preview_bulk_review_persona_run_items(
            run_id: str,
            payload: PersonaRunBulkReviewItemsRequest,
            request: Request,
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["personas:read"])
        try:
            result = await PersonaFactoryService(context).preview_bulk_review_run_items(
                run_id,
                action=payload.action,
                source_key=payload.filters.source_key,
                item_type=payload.filters.item_type,
                memory_layer=payload.filters.memory_layer,
                review_status=payload.filters.review_status,
                needs_review=payload.filters.needs_review,
                min_confidence=payload.filters.min_confidence,
                max_confidence=payload.filters.max_confidence,
                extraction_source=payload.filters.extraction_source,
                distiller=payload.filters.distiller,
                review_flag=payload.filters.review_flag,
                conflict_group_id=payload.filters.conflict_group_id,
                limit=payload.limit,
            )
        except PersonaDistillationError as exc:
            response_status = (
                status.HTTP_404_NOT_FOUND
                if "not found" in str(exc).lower()
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc
        return {
            **result,
            "items": [item.model_dump(mode="json") for item in result["items"]],
        }

    @router.post("/persona-factory/runs/{run_id}/review-actions", summary="Apply Persona Review Action")
    async def apply_persona_run_review_action(
            run_id: str,
            payload: PersonaRunReviewActionRequest,
            request: Request,
    ):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            return await PersonaFactoryService(context).apply_run_review_action(
                run_id,
                action=payload.action,
                item_ids=payload.item_ids,
                conflict_group_id=payload.conflict_group_id,
                reason=payload.reason,
                patch=payload.patch,
            )
        except PersonaDistillationError as exc:
            response_status = (
                status.HTTP_404_NOT_FOUND
                if "not found" in str(exc).lower()
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc

    @router.post("/persona-factory/runs/{run_id}/synthesize-package", summary="Synthesize Persona Package")
    async def synthesize_persona_package(
            run_id: str,
            request: Request,
            payload: PersonaSynthesizePackageRequest | None = Body(default=None),
    ):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        payload = payload or PersonaSynthesizePackageRequest()
        try:
            return await PersonaFactoryService(context).synthesize_package_from_items(
                run_id,
                package_synthesis_mode=payload.package_synthesis_mode,
                llm_polishing_model_profile_id=payload.llm_polishing_model_profile_id,
            )
        except (PersonaDistillationError, PersonaNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("/persona-factory/runs/{run_id}/normalize", summary="Normalize Persona Distillation Items")
    async def normalize_persona_items(run_id: str, request: Request):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            return await PersonaFactoryService(context).normalize_run_items(run_id)
        except (PersonaDistillationError, PersonaNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.patch("/persona-factory/runs/{run_id}/package", summary="Update Persona Draft Package")
    async def update_persona_package(run_id: str, payload: PersonaPackagePatchRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            run = await PersonaFactoryService(context).update_run_package(run_id, payload.package)
        except PersonaDistillationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return run.model_dump(mode="json")

    @router.post("/persona-factory/runs/{run_id}/approve", summary="Approve Persona Draft")
    async def approve_persona_run(run_id: str, payload: PersonaApproveRequest, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            return await PersonaFactoryService(context).approve_run(
                run_id,
                current_user=current_user,
                version=payload.version,
            )
        except (PersonaDistillationError, PersonaNotFoundError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("/persona-factory/runs/{run_id}/publish", summary="Publish Persona Draft")
    async def publish_persona_run(run_id: str, request: Request):
        current_user = await resolve_current_user(request, context, required_scopes=["personas:write"])
        try:
            return await PersonaFactoryService(context).publish_run(run_id, current_user=current_user)
        except (PersonaDistillationError, PersonaNotFoundError, PersonaPublishError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    return router


__all__ = ["create_persona_router"]
