from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Request, status
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.graph.neo4j_read import GraphReadDocument, Neo4jGraphReadError
from app.graph.service import (
    GRAPH_NEIGHBORHOOD_PRESETS,
    GraphReadUnavailableError,
    close_graph_reader_if_needed,
    graph_document_payload,
    resolve_graph_reader,
)
from app.observability.service import ObservabilityService
from app.runtime.native.errors import ExecutionNotFoundError

OBSERVABILITY_GRAPH_MAX_DEPTH = 2
OBSERVABILITY_GRAPH_MAX_LIMIT = 250
GRAPH_UNAVAILABLE_REASON = "Neo4j graph read API is disabled or unavailable"


def create_observability_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = ObservabilityService(context)
    router = APIRouter(prefix="/observability", tags=["Observability"])

    async def _get_projection_graph(
            *,
            request: Request,
            preset: str,
            node_id: str,
            query_name: str,
            depth: int,
            limit: int,
            include_deleted: bool,
    ):
        await resolve_current_user(request, context, required_scopes=["executions:read"])
        preset_config = GRAPH_NEIGHBORHOOD_PRESETS[preset]
        try:
            reader, close_after = resolve_graph_reader(context)
        except GraphReadUnavailableError:
            return {
                "available": False,
                "reason": GRAPH_UNAVAILABLE_REASON,
                "graph": graph_document_payload(
                    GraphReadDocument(nodes=[], edges=[]),
                    query_meta={
                        "query": query_name,
                        "preset": preset,
                        "node_id": node_id,
                        "projection_available": False,
                    },
                    limit=limit,
                ),
            }
        try:
            document = await reader.get_neighborhood(
                node_id,
                labels=preset_config["labels"],
                relationship_types=preset_config["relationship_types"],
                depth=depth,
                limit=limit,
                include_deleted=include_deleted,
            )
            return {
                "available": True,
                "reason": None,
                "graph": graph_document_payload(
                    document,
                    query_meta={
                        "query": query_name,
                        "preset": preset,
                        "node_id": node_id,
                        "depth": depth,
                        "relationship_types": preset_config["relationship_types"],
                        "labels": preset_config["labels"],
                        "include_deleted": include_deleted,
                        "projection_available": True,
                    },
                    limit=limit,
                ),
            }
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except Neo4jGraphReadError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        finally:
            await close_graph_reader_if_needed(reader, close_after)

    @router.get("/executions/{execution_id}/timeline", summary="Get Execution Timeline")
    async def get_execution_timeline(execution_id: str):
        try:
            return await service.get_execution_timeline(execution_id)
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.get("/executions/{execution_id}/graph", summary="Get Execution Projection Graph")
    async def get_execution_projection_graph(
            execution_id: str,
            request: Request,
            depth: int = Query(default=1, ge=1, le=OBSERVABILITY_GRAPH_MAX_DEPTH),
            limit: int = Query(default=100, ge=1, le=OBSERVABILITY_GRAPH_MAX_LIMIT),
            include_deleted: bool = Query(default=False),
    ):
        return await _get_projection_graph(
            request=request,
            preset="workflow_run",
            node_id=execution_id,
            query_name="observability_execution_graph",
            depth=depth,
            limit=limit,
            include_deleted=include_deleted,
        )

    @router.get("/agents/{agent_id}/metrics", summary="Get Agent Metrics")
    async def get_agent_metrics(agent_id: str):
        return await service.get_agent_metrics(agent_id)

    @router.get("/workflows/{workflow_id}/metrics", summary="Get Workflow Metrics")
    async def get_workflow_metrics(workflow_id: str):
        return await service.get_workflow_metrics(workflow_id)

    @router.get("/workflows/{workflow_id}/graph", summary="Get Workflow Projection Graph")
    async def get_workflow_projection_graph(
            workflow_id: str,
            request: Request,
            depth: int = Query(default=1, ge=1, le=OBSERVABILITY_GRAPH_MAX_DEPTH),
            limit: int = Query(default=100, ge=1, le=OBSERVABILITY_GRAPH_MAX_LIMIT),
            include_deleted: bool = Query(default=False),
    ):
        return await _get_projection_graph(
            request=request,
            preset="workflow",
            node_id=workflow_id,
            query_name="observability_workflow_graph",
            depth=depth,
            limit=limit,
            include_deleted=include_deleted,
        )

    @router.get("/models/usage", summary="Get Model Usage")
    async def get_model_usage(
            workflow_id: str | None = Query(default=None),
            agent_id: str | None = Query(default=None),
            execution_id: str | None = Query(default=None),
            provider: str | None = Query(default=None),
            model: str | None = Query(default=None),
    ):
        return await service.get_model_usage(
            workflow_id=workflow_id,
            agent_id=agent_id,
            execution_id=execution_id,
            provider=provider,
            model=model,
        )

    @router.get("/connectors/history", summary="Get Connector Health History")
    async def get_connector_history(
            request: Request,
            limit: int = Query(default=20, ge=1, le=100),
            offset: int = Query(default=0, ge=0),
            status_filter: str | None = Query(default=None, alias="status"),
            started_after: datetime | None = Query(default=None, alias="started_after"),
            started_before: datetime | None = Query(default=None, alias="started_before"),
            provider: str | None = Query(default=None),
    ):
        current_user = await resolve_current_user(request, context)
        return await service.get_connector_history(
            current_user.id,
            limit=limit,
            offset=offset,
            status=status_filter,
            started_after=started_after,
            started_before=started_before,
            provider=provider,
        )

    @router.get("/connectors/retention", summary="Get Connector Retention Status")
    async def get_connector_retention_status(request: Request):
        await resolve_current_user(request, context)
        return service.get_connector_retention_status()

    @router.get("/api-tokens/activity", summary="Get API Token Activity")
    async def get_api_token_activity(
            request: Request,
            limit: int = Query(default=20, ge=1, le=100),
            token_id: str | None = Query(default=None),
    ):
        current_user = await resolve_current_user(request, context)
        return service.get_api_token_activity(
            current_user.id,
            limit=limit,
            token_id=token_id,
        )

    return router
