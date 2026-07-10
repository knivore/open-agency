"""Durable memory CRUD, embedding backfill, document, and daily-summary routes."""

from __future__ import annotations

from datetime import date, timedelta
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import AliasChoices, BaseModel, Field, ValidationError
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user_if_present
from app.core.config import get_settings
from app.domain import MemoryType, MemoryScope, MemoryStatus
from app.services.conversation_compact import (
    ConversationCompactService,
    SUPPORTED_COMPACT_MODES,
    SUPPORTED_COMPACT_SCOPES,
    SUPPORTED_COMPACT_SOURCE_RANGES,
    SUPPORTED_COMPACT_STRATEGIES,
)
from app.services.memory import MemoryPermissionError, MemoryPolicyError, MemoryService
from app.services.source_intelligence import SourceIntelligenceError, SourceIntelligenceService
from ._crud import serializable_validation_errors


class MemoryWriteRequest(BaseModel):
    memory: dict[str, Any]
    confirmed: bool = False


class MemoryUpdateRequest(BaseModel):
    patch: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False


class MemoryEmbeddingBackfillRequest(BaseModel):
    limit: int = Field(default=100, ge=0, le=1000)
    force: bool = False


class DailySummaryRunRequest(BaseModel):
    target_date: date | None = None
    timezone: str | None = None
    conversation_id: str | None = None
    dry_run: bool = False


class DailySummaryBackfillRequest(BaseModel):
    start_date: date
    end_date: date
    timezone: str | None = None
    conversation_id: str | None = None
    dry_run: bool = False


class CompactBackfillRequest(BaseModel):
    conversation_id: str | None = None
    user_id: str | None = None
    workspace_id: str | None = None
    workflow_id: str | None = None
    mode: str = "handoff"
    strategy: str = "deterministic"
    token_budget: int = Field(default=1200, ge=100, le=8000)
    source_range: str = "full"
    recent_message_limit: int = Field(default=8, ge=0, le=200)
    scope: str = "conversation"
    limit: int = Field(default=50, ge=0, le=500)
    dry_run: bool = False
    confirmed: bool = False
    skip_existing: bool = True
    supersede_previous: bool = True
    idempotency_key: str | None = None
    model_profile_id: str | None = None
    custom_keep: list[str] | None = None
    custom_drop: list[str] | None = None


class MemoryExclusionRequest(BaseModel):
    target_type: str = Field(validation_alias=AliasChoices("targetType", "target_type"))
    target_id: str | None = Field(default=None, validation_alias=AliasChoices("targetId", "target_id"))
    reason: str | None = None


class MemorySourceIntelligenceAnalyzeRequest(BaseModel):
    memory_ids: list[str] = Field(default_factory=list, validation_alias=AliasChoices("memoryIds", "memory_ids"))
    model_profile_id: str | None = Field(default=None,
                                         validation_alias=AliasChoices("modelProfileId", "model_profile_id"))
    persist: bool = True


class MemorySourceIntelligenceReviewRequest(BaseModel):
    source_intelligence: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("sourceIntelligence", "source_intelligence"),
    )
    graph_hints: dict[str, Any] | None = Field(default=None, validation_alias=AliasChoices("graphHints", "graph_hints"))
    source_intelligence_review_status: str | None = Field(
        default=None,
        validation_alias=AliasChoices("sourceIntelligenceReviewStatus", "source_intelligence_review_status"),
    )
    graph_hints_review_status: str | None = Field(
        default=None,
        validation_alias=AliasChoices("graphHintsReviewStatus", "graph_hints_review_status"),
    )
    review_note: str | None = Field(default=None, validation_alias=AliasChoices("reviewNote", "review_note"))


class MemoryDocumentDeleteResponse(BaseModel):
    deleted: bool
    document_id: str
    memory_ids: list[str]
    deleted_count: int


def _normalize_query_tags(*tag_groups: list[str] | None) -> list[str] | None:
    tags: list[str] = []
    for group in tag_groups:
        for value in group or []:
            tags.extend(item.strip() for item in value.split(",") if item.strip())
    deduped = list(dict.fromkeys(tags))
    return deduped or None


def create_memory_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = MemoryService(context)
    router = APIRouter(prefix="/memories", tags=["Memory"])

    @router.get("", summary="List Memories")
    async def list_memories(
            request: Request,
            scope: str | None = None,
            user_id: str | None = None,
            workspace_id: str | None = None,
            conversation_id: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            source: str | None = None,
            memory_type: list[str] | None = Query(default=None),
            tag: list[str] | None = Query(default=None),
            tags: list[str] | None = Query(default=None),
            status_filter: list[str] | None = Query(default=None, alias="status"),
            source_conversation_id: str | None = None,
            source_execution_id: str | None = None,
            summary_date_from: date | None = None,
            summary_date_to: date | None = None,
            q: str | None = None,
            limit: int = Query(50, ge=0, le=200),
    ):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:read"])
        items = await service.list_memories(
            scope=scope,
            user_id=user_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            source=source,
            memory_types=memory_type,
            tags=_normalize_query_tags(tag, tags),
            statuses=status_filter,
            source_conversation_id=source_conversation_id,
            source_execution_id=source_execution_id,
            summary_date_from=summary_date_from,
            summary_date_to=summary_date_to,
            q=q,
            limit=limit,
            current_user=current_user,
        )
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.get("/catalog", summary="List Linkable Memory Catalog")
    async def list_memory_catalog(
            request: Request,
            scope: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            conversation_id: str | None = None,
            target_type: str | None = None,
            target_id: str | None = None,
            q: str | None = None,
            include_sensitive: bool = False,
            status_filter: list[str] | None = Query(default=None, alias="status"),
            limit_per_group: int = Query(20, ge=0, le=100),
    ):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:read"])
        try:
            return await service.list_memory_catalog(
                scope=scope,
                workflow_id=workflow_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                target_type=target_type,
                target_id=target_id,
                q=q,
                include_sensitive=include_sensitive,
                statuses=status_filter,
                limit_per_group=limit_per_group,
                current_user=current_user,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.get("/exclusions", summary="List Memory Exclusions")
    async def list_memory_exclusions(
            request: Request,
            memory_id: str | None = None,
            target_type: str | None = None,
            target_id: str | None = None,
    ):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:read"])
        try:
            items = await service.list_memory_exclusions(
                memory_id=memory_id,
                target_type=target_type,
                target_id=target_id,
                current_user=current_user,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return {"items": items}

    @router.post("/embeddings/backfill", summary="Backfill Memory Embeddings")
    async def backfill_memory_embeddings(payload: MemoryEmbeddingBackfillRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        return await service.backfill_embeddings(
            limit=payload.limit,
            force=payload.force,
            current_user=current_user,
        )

    @router.get("/source-intelligence/catalog", summary="List Memory Source Intelligence Catalog")
    async def list_memory_source_intelligence_catalog(request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["memory:read"])
        return SourceIntelligenceService.catalog()

    @router.post("/source-intelligence/analyze", summary="Analyze Memory Source Intelligence")
    async def analyze_memory_source_intelligence(payload: MemorySourceIntelligenceAnalyzeRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        if not payload.memory_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Select at least one memory to analyze.",
            )
        try:
            return await SourceIntelligenceService(context).analyze_memories(
                memory_ids=payload.memory_ids,
                model_profile_id=payload.model_profile_id,
                persist=payload.persist,
                current_user=current_user,
            )
        except MemoryPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except SourceIntelligenceError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.post("/daily-summaries/run", summary="Run Daily Conversation Summary")
    async def run_daily_summary(payload: DailySummaryRunRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        if current_user is None or "admin" not in current_user.roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access is required.")
        from app.services.conversation_daily_summary import ConversationDailySummaryService

        return await ConversationDailySummaryService(context).summarize_day(
            target_date=payload.target_date,
            timezone_name=payload.timezone,
            conversation_id=payload.conversation_id,
            dry_run=payload.dry_run,
        )

    @router.post("/daily-summaries/backfill", summary="Backfill Daily Conversation Summaries")
    async def backfill_daily_summaries(payload: DailySummaryBackfillRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        if current_user is None or "admin" not in current_user.roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access is required.")
        if payload.end_date < payload.start_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="end_date must be on or after start_date",
            )
        from app.services.conversation_daily_summary import ConversationDailySummaryService

        summary_service = ConversationDailySummaryService(context)
        day = payload.start_date
        runs: list[dict[str, Any]] = []
        created = 0
        processed = 0
        skipped = 0
        failed = 0
        while day <= payload.end_date:
            result = await summary_service.summarize_day(
                target_date=day,
                timezone_name=payload.timezone,
                conversation_id=payload.conversation_id,
                dry_run=payload.dry_run,
            )
            runs.append(result)
            created += int(result.get("created", 0))
            processed += int(result.get("processed", 0))
            skipped += int(result.get("skipped", 0))
            failed += int(result.get("failed", 0))
            day += timedelta(days=1)
        return {
            "status": "dry_run" if payload.dry_run else "ok",
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
            "created": created,
            "processed": processed,
            "skipped": skipped,
            "failed": failed,
            "runs": runs,
        }

    @router.post("/compact/backfill", summary="Backfill Conversation Compact Packs")
    async def backfill_compact_packs(payload: CompactBackfillRequest, request: Request):
        if not get_settings().memory_context_pack_enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Conversation compact packs are disabled.",
            )
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        if current_user is None or "admin" not in current_user.roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access is required.")
        mode = payload.mode.strip().lower()
        strategy = payload.strategy.strip().lower()
        source_range = payload.source_range.strip().lower()
        compact_scope = payload.scope.strip().lower()
        if mode not in SUPPORTED_COMPACT_MODES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_MODES))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported compact mode '{payload.mode}'. Choose one of: {allowed}.",
            )
        if strategy not in SUPPORTED_COMPACT_STRATEGIES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_STRATEGIES))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported compact strategy '{payload.strategy}'. Choose one of: {allowed}.",
            )
        if source_range not in SUPPORTED_COMPACT_SOURCE_RANGES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_SOURCE_RANGES))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported compact source_range '{payload.source_range}'. Choose one of: {allowed}.",
            )
        if compact_scope not in SUPPORTED_COMPACT_SCOPES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_SCOPES))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported compact scope '{payload.scope}'. Choose one of: {allowed}.",
            )

        filtered_out_conversation_id: str | None = None
        if payload.conversation_id is not None:
            conversation = await context.conversation_repo.get(payload.conversation_id)
            if conversation is None:
                conversations = []
            elif _conversation_matches_compact_backfill_filters(conversation, payload):
                conversations = [conversation]
            else:
                filtered_out_conversation_id = conversation.id
                conversations = []
        else:
            conversations = await context.conversation_repo.list()
            conversations = [
                conversation
                for conversation in conversations
                if _conversation_matches_compact_backfill_filters(conversation, payload)
            ]
        conversations = conversations[:payload.limit]

        compact_service = ConversationCompactService(context)
        results: list[dict[str, Any]] = []
        progress_events: list[dict[str, Any]] = [{
            "step": "select_conversations",
            "status": "completed",
            "message": "Conversations selected for compact backfill.",
            "metadata": {"conversation_count": len(conversations)},
        }]
        created = 0
        skipped = 0
        failed = 0
        processed = 0

        for conversation in conversations:
            if conversation is None:
                continue
            processed += 1
            progress_events.append({
                "step": "process_conversation",
                "status": "started",
                "message": "Compact backfill started for conversation.",
                "metadata": {"conversation_id": conversation.id, "index": processed},
            })
            try:
                messages = await context.conversation_message_repo.list_by_conversation(conversation.id)
                if not messages:
                    skipped += 1
                    results.append({
                        "conversation_id": conversation.id,
                        "status": "skipped",
                        "reason": "no_messages",
                    })
                    progress_events.append({
                        "step": "process_conversation",
                        "status": "skipped",
                        "message": "Conversation skipped because it has no messages.",
                        "metadata": {"conversation_id": conversation.id},
                    })
                    continue
                if (
                        payload.skip_existing
                        and not payload.idempotency_key
                        and source_range != "since_last_compact"
                        and await _active_context_pack_exists(
                    context,
                    conversation,
                    mode,
                    scope=compact_scope,
                    workflow_id=payload.workflow_id,
                )
                ):
                    skipped += 1
                    results.append({
                        "conversation_id": conversation.id,
                        "status": "skipped",
                        "reason": "existing_active_context_pack",
                    })
                    progress_events.append({
                        "step": "process_conversation",
                        "status": "skipped",
                        "message": "Conversation skipped because an active context pack already exists.",
                        "metadata": {"conversation_id": conversation.id},
                    })
                    continue
                if payload.dry_run:
                    preview = await compact_service.compact_conversation(
                        conversation.id,
                        mode=mode,
                        token_budget=payload.token_budget,
                        source_range=source_range,
                        recent_message_limit=payload.recent_message_limit,
                        scope=compact_scope,
                        workflow_id=payload.workflow_id,
                        persist=False,
                        confirmed=payload.confirmed,
                        idempotency_key=payload.idempotency_key,
                        strategy="deterministic",
                        model_profile_id=payload.model_profile_id,
                        custom_keep=payload.custom_keep,
                        custom_drop=payload.custom_drop,
                    )
                    results.append({
                        "conversation_id": conversation.id,
                        "status": "would_create",
                        "mode": mode,
                        "scope": compact_scope,
                        "source_range": source_range,
                        "idempotency_key": payload.idempotency_key,
                        "source_message_count": preview["source_message_count"],
                        "estimated_source_tokens": preview["estimated_source_tokens"],
                        "estimated_compact_tokens": preview["estimated_compact_tokens"],
                        "sensitive": preview["sensitive"],
                        "progress": preview["progress"],
                    })
                    progress_events.append({
                        "step": "process_conversation",
                        "status": "completed",
                        "message": "Dry-run compact preview completed for conversation.",
                        "metadata": {"conversation_id": conversation.id, "result_status": "would_create"},
                    })
                    continue
                result = await compact_service.compact_conversation(
                    conversation.id,
                    mode=mode,
                    token_budget=payload.token_budget,
                    source_range=source_range,
                    recent_message_limit=payload.recent_message_limit,
                    scope=compact_scope,
                    workflow_id=payload.workflow_id,
                    persist=True,
                    confirmed=payload.confirmed,
                    supersede_previous=payload.supersede_previous,
                    idempotency_key=payload.idempotency_key,
                    strategy=strategy,
                    model_profile_id=payload.model_profile_id,
                    custom_keep=payload.custom_keep,
                    custom_drop=payload.custom_drop,
                )
                created += 1
                if result["status"] == "existing":
                    created -= 1
                results.append({
                    "conversation_id": conversation.id,
                    "status": result["status"],
                    "memory_id": result["memory_id"],
                    "mode": mode,
                    "scope": result["scope"],
                    "source_range": source_range,
                    "idempotency_key": payload.idempotency_key,
                    "source_message_count": result["source_message_count"],
                    "estimated_source_tokens": result["estimated_source_tokens"],
                    "estimated_compact_tokens": result["estimated_compact_tokens"],
                    "sensitive": result["sensitive"],
                    "warnings": result["warnings"],
                    "progress": result["progress"],
                })
                progress_events.append({
                    "step": "process_conversation",
                    "status": "completed",
                    "message": "Compact backfill completed for conversation.",
                    "metadata": {"conversation_id": conversation.id, "result_status": result["status"]},
                })
            except Exception as exc:
                failed += 1
                results.append({
                    "conversation_id": conversation.id,
                    "status": "failed",
                    "reason": str(exc),
                })
                progress_events.append({
                    "step": "process_conversation",
                    "status": "failed",
                    "message": "Compact backfill failed for conversation.",
                    "metadata": {"conversation_id": conversation.id, "reason": str(exc)},
                })

        if payload.conversation_id is not None and not conversations:
            if filtered_out_conversation_id:
                skipped = 1
                results.append({
                    "conversation_id": filtered_out_conversation_id,
                    "status": "skipped",
                    "reason": "conversation_filtered_out",
                })
                progress_events.append({
                    "step": "process_conversation",
                    "status": "skipped",
                    "message": "Conversation was filtered out by backfill filters.",
                    "metadata": {"conversation_id": filtered_out_conversation_id},
                })
            else:
                failed = 1
                results.append({
                    "conversation_id": payload.conversation_id,
                    "status": "failed",
                    "reason": "conversation_not_found",
                })
                progress_events.append({
                    "step": "process_conversation",
                    "status": "failed",
                    "message": "Requested conversation was not found.",
                    "metadata": {"conversation_id": payload.conversation_id},
                })
        progress_events.append({
            "step": "finish",
            "status": "completed" if failed == 0 else "failed",
            "message": "Compact backfill finished.",
            "metadata": {
                "processed": processed,
                "created": created,
                "skipped": skipped,
                "failed": failed,
            },
        })

        return {
            "status": (
                "dry_run"
                if payload.dry_run
                else ("partial" if failed and (created or skipped) else ("error" if failed else "ok"))
            ),
            "mode": mode,
            "strategy": strategy,
            "scope": compact_scope,
            "source_range": source_range,
            "filters": {
                "conversation_id": payload.conversation_id,
                "user_id": payload.user_id,
                "workspace_id": payload.workspace_id,
                "workflow_id": payload.workflow_id,
            },
            "processed": processed,
            "created": created,
            "skipped": skipped,
            "failed": failed,
            "results": results,
            "progress": {
                "completed_steps": sum(1 for event in progress_events if event["status"] in {"completed", "skipped"}),
                "failed_steps": sum(1 for event in progress_events if event["status"] == "failed"),
                "events": progress_events,
            },
        }

    @router.post("", summary="Create Memory")
    async def create_memory(payload: MemoryWriteRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        try:
            created = await service.create_memory(payload.memory, confirmed=payload.confirmed,
                                                  current_user=current_user)
        except MemoryPolicyError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except MemoryPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        return created.model_dump(mode="json")

    @router.delete("/documents/{document_id}", summary="Delete Uploaded Document Memories")
    async def delete_document_memories(
            document_id: str,
            request: Request,
            scope: str | None = None,
            user_id: str | None = None,
            workspace_id: str | None = None,
            conversation_id: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            tag: list[str] | None = Query(default=None),
            tags: list[str] | None = Query(default=None),
    ):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        try:
            memory_ids = await service.delete_document_memories(
                document_id,
                scope=scope,
                user_id=user_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                tags=_normalize_query_tags(tag, tags),
                current_user=current_user,
            )
        except MemoryPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        if not memory_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Document memories for '{document_id}' not found",
            )
        return MemoryDocumentDeleteResponse(
            deleted=True,
            document_id=document_id,
            memory_ids=memory_ids,
            deleted_count=len(memory_ids),
        ).model_dump(mode="json")

    @router.post("/{memory_id}/exclusions", summary="Exclude Memory From Target")
    async def add_memory_exclusion(memory_id: str, payload: MemoryExclusionRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        try:
            return await service.add_memory_exclusion(
                memory_id,
                target_type=payload.target_type,
                target_id=payload.target_id,
                reason=payload.reason,
                current_user=current_user,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Memory '{memory_id}' not found") from exc
        except MemoryPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @router.delete("/{memory_id}/exclusions/{exclusion_id}", summary="Remove Memory Exclusion")
    async def delete_memory_exclusion(memory_id: str, exclusion_id: str, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        try:
            deleted = await service.delete_memory_exclusion(
                memory_id,
                exclusion_id,
                current_user=current_user,
            )
        except MemoryPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Memory exclusion '{exclusion_id}' not found",
            )
        return {"deleted": True, "memory_id": memory_id, "exclusion_id": exclusion_id}

    @router.patch("/{memory_id}/source-intelligence", summary="Review Memory Source Intelligence")
    async def review_memory_source_intelligence(
            memory_id: str,
            payload: MemorySourceIntelligenceReviewRequest,
            request: Request,
    ):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        try:
            item = await SourceIntelligenceService(context).review_memory_source_intelligence(
                memory_id=memory_id,
                source_intelligence=payload.source_intelligence,
                graph_hints=payload.graph_hints,
                source_intelligence_review_status=payload.source_intelligence_review_status,
                graph_hints_review_status=payload.graph_hints_review_status,
                review_note=payload.review_note,
                current_user=current_user,
            )
        except MemoryPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except SourceIntelligenceError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return item.model_dump(mode="json")

    @router.get("/{memory_id}", summary="Get Memory By Id")
    async def get_memory(memory_id: str, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:read"])
        try:
            item = await service.get_memory(memory_id, current_user=current_user)
        except MemoryPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Memory '{memory_id}' not found")
        return item.model_dump(mode="json")

    @router.patch("/{memory_id}", summary="Update Memory")
    async def update_memory(memory_id: str, payload: MemoryUpdateRequest, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        try:
            item = await service.update_memory(
                memory_id,
                payload.patch,
                confirmed=payload.confirmed,
                current_user=current_user,
            )
        except MemoryPolicyError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except MemoryPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=serializable_validation_errors(exc),
            ) from exc
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Memory '{memory_id}' not found")
        return item.model_dump(mode="json")

    @router.delete("/{memory_id}", summary="Delete Memory")
    async def delete_memory(memory_id: str, request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["memory:write"])
        try:
            deleted = await service.delete_memory(memory_id, current_user=current_user)
        except MemoryPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Memory '{memory_id}' not found")
        return {"deleted": True, "id": memory_id}

    return router


async def _active_context_pack_exists(
        context: ApiContext,
        conversation: Any,
        mode: str,
        *,
        scope: str,
        workflow_id: str | None,
) -> bool:
    filters: dict[str, Any] = {
        "scopes": [scope],
        "source_conversation_id": conversation.id,
    }
    if scope == MemoryScope.CONVERSATION.value:
        filters["conversation_id"] = conversation.id
    elif scope == MemoryScope.USER.value:
        filters["user_id"] = conversation.created_by_user_id
    elif scope == MemoryScope.WORKSPACE.value:
        filters["workspace_id"] = conversation.workspace_id
    elif scope == MemoryScope.WORKFLOW.value:
        filters["workflow_id"] = workflow_id
    items = await context.memory_repo.query(
        **filters,
        source="compact_tool",
        memory_types=[MemoryType.CONTEXT_PACK.value],
        tags=[mode],
        statuses=[MemoryStatus.ACTIVE.value],
        limit=1,
    )
    return bool(items)


def _conversation_matches_compact_backfill_filters(conversation: Any, payload: CompactBackfillRequest) -> bool:
    if payload.user_id is not None and conversation.created_by_user_id != payload.user_id:
        return False
    if payload.workspace_id is not None and conversation.workspace_id != payload.workspace_id:
        return False
    return True
