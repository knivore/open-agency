"""Read-only Agency Graph traversal API for visualization and observability views."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.core.config import get_settings
from app.graph.neo4j_read import GraphReadDocument, Neo4jGraphReadError
from app.graph.operational_coverage import enrich_with_operational_coverage
from app.graph.service import (
    GRAPH_NEIGHBORHOOD_MODES,
    GRAPH_NEIGHBORHOOD_PRESETS,
    GRAPH_QUERY_PRESETS,
    GraphReadUnavailableError,
    close_graph_reader_if_needed as _close_reader_if_needed,
    graph_document_payload as _document_payload,
    graph_neighbors_payload as _neighbors_payload,
    resolve_graph_reader,
)

GRAPH_EXPANSION_MAX_DEPTH = 2
GRAPH_EXPANSION_MAX_LIMIT = 250
GRAPH_PATH_MAX_DEPTH = 4
GRAPH_PATH_MAX_LIMIT = 100
GRAPH_OPERATIONAL_RECENT_RUN_LIMIT = 40
GRAPH_OPERATIONAL_WORKFLOW_RUN_LIMIT = 24
GRAPH_OPERATIONAL_INCIDENT_LIMIT = 12


def _split_csv(values: str | None) -> list[str]:
    if not values:
        return []
    return [value.strip() for value in values.split(",") if value.strip()]


def _bounded_int(value: int, *, minimum: int, maximum: int) -> int:
    return max(min(int(value), maximum), minimum)


def _preset_or_400(preset: str) -> dict[str, list[str]]:
    try:
        return GRAPH_NEIGHBORHOOD_PRESETS[preset]
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown graph neighborhood preset: {preset}",
        ) from exc


def _mode_or_400(mode: str) -> dict[str, list[str]]:
    try:
        return GRAPH_NEIGHBORHOOD_MODES[mode]
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown graph neighborhood mode: {mode}",
        ) from exc


def _query_preset_or_400(preset: str) -> str:
    preset_key = preset.lower().strip().replace("-", "_")
    if preset_key not in GRAPH_QUERY_PRESETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown graph preset: {preset}",
        )
    return preset_key


def _root_type_from_labels(labels: list[str]) -> str:
    normalized = {label.lower() for label in labels}
    if "workflow" in normalized:
        return "workflow"
    if "workflowrun" in normalized or "run" in normalized:
        return "workflow_run"
    if "agent" in normalized:
        return "agent"
    return "node"


def _graph_read_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Neo4j graph read API is disabled or unavailable",
    )


def _resolve_graph_reader(context: ApiContext):
    try:
        return resolve_graph_reader(context)
    except GraphReadUnavailableError as exc:
        raise _graph_read_unavailable() from exc


async def _resolve_authorized_graph_reader(request: Request, context: ApiContext):
    current_user = await _resolve_graph_read_user(request, context)
    reader, close_after = _resolve_graph_reader(context)
    _attach_reader_access(reader, current_user)
    return reader, close_after


async def _resolve_graph_read_user(request: Request, context: ApiContext):
    return await resolve_current_user(request, context, required_scopes=["executions:read"])


def _attach_reader_access(reader, current_user) -> None:
    if getattr(current_user, "id", None):
        setattr(reader, "access_user_id", current_user.id)


async def _operational_fallback_document(
        *,
        context: ApiContext,
        root_type: str,
        root_id: str,
        recent_run_limit: int,
        workflow_run_limit: int,
        incident_limit: int,
        unavailable_reason: str,
) -> GraphReadDocument:
    document = GraphReadDocument(
        nodes=[],
        edges=[],
        meta={
            "projection_available": False,
            "projection_fallback": "operational_coverage",
            "projection_unavailable_reason": unavailable_reason,
        },
    )
    return await enrich_with_operational_coverage(
        document,
        execution_store=context.execution_store,
        root_type=root_type,
        root_id=root_id,
        include=True,
        recent_run_limit=recent_run_limit,
        workflow_run_limit=workflow_run_limit,
        incident_limit=incident_limit,
    )


def create_graph_read_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/graph/read", tags=["Graph Read"])

    @router.get("/status", summary="Get Graph Read API Status")
    async def get_graph_read_status(request: Request):
        await resolve_current_user(request, context, required_scopes=["executions:read"])
        settings = get_settings()
        if getattr(context, "graph_read_service", None) is not None:
            return {"enabled": True, "available": True, "source": "injected"}
        if not settings.neo4j_enabled:
            return {"enabled": False, "available": False, "source": "neo4j"}
        reader, close_after = _resolve_graph_reader(context)
        try:
            return {"enabled": True, "available": await reader.ping(), "source": "neo4j"}
        except Neo4jGraphReadError as exc:
            return {"enabled": True, "available": False, "source": "neo4j", "error": str(exc)}
        finally:
            await _close_reader_if_needed(reader, close_after)

    @router.get("/nodes/{node_id}", summary="Get Graph Node")
    async def get_graph_node(
            node_id: str,
            request: Request,
            labels: str | None = Query(default=None, description="Comma-separated label allow-list."),
    ):
        reader, close_after = await _resolve_authorized_graph_reader(request, context)
        try:
            return _document_payload(await reader.get_node(node_id, labels=_split_csv(labels)), limit=1, max_edges=0)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except Neo4jGraphReadError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        finally:
            await _close_reader_if_needed(reader, close_after)

    @router.get("/nodes/{node_id}/neighborhood", summary="Get Graph Node Neighborhood")
    async def get_graph_node_neighborhood(
            node_id: str,
            request: Request,
            labels: str | None = Query(default=None, description="Comma-separated center-node label allow-list."),
            relationship_types: str | None = Query(default=None,
                                                   description="Comma-separated relationship type allow-list."),
            depth: int = Query(default=1, ge=1, le=4),
            limit: int = Query(default=200, ge=1, le=1000),
            include_deleted: bool = Query(default=False),
            include_operational_coverage: bool = Query(default=False),
            recent_run_limit: int = Query(default=GRAPH_OPERATIONAL_RECENT_RUN_LIMIT, ge=1, le=200),
            workflow_run_limit: int = Query(default=GRAPH_OPERATIONAL_WORKFLOW_RUN_LIMIT, ge=1, le=100),
            incident_limit: int = Query(default=GRAPH_OPERATIONAL_INCIDENT_LIMIT, ge=1, le=50),
    ):
        reader, close_after = await _resolve_authorized_graph_reader(request, context)
        try:
            resolved_labels = _split_csv(labels)
            document = await reader.get_neighborhood(
                node_id,
                labels=resolved_labels,
                relationship_types=_split_csv(relationship_types),
                depth=depth,
                limit=limit,
                include_deleted=include_deleted,
            )
            document = await enrich_with_operational_coverage(
                document,
                execution_store=context.execution_store,
                root_type=_root_type_from_labels(resolved_labels),
                root_id=node_id,
                include=include_operational_coverage,
                recent_run_limit=recent_run_limit,
                workflow_run_limit=workflow_run_limit,
                incident_limit=incident_limit,
            )
            return _document_payload(
                document,
                query_meta={
                    "include_operational_coverage": include_operational_coverage,
                    "recent_run_limit": recent_run_limit,
                    "workflow_run_limit": workflow_run_limit,
                    "incident_limit": incident_limit,
                },
                limit=limit,
                max_edges=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except Neo4jGraphReadError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        finally:
            await _close_reader_if_needed(reader, close_after)

    @router.get("/nodes/{node_id}/expand", summary="Expand Graph Node")
    async def expand_graph_node(
            node_id: str,
            request: Request,
            preset: str | None = Query(default=None, description="Optional domain preset."),
            mode: str | None = Query(default=None, description="Optional graph neighborhood mode."),
            labels: str | None = Query(default=None, description="Comma-separated center-node label allow-list."),
            relationship_types: str | None = Query(default=None,
                                                   description="Comma-separated relationship type allow-list."),
            depth: int = Query(default=1, ge=1, le=GRAPH_EXPANSION_MAX_DEPTH),
            limit: int = Query(default=100, ge=1, le=GRAPH_EXPANSION_MAX_LIMIT),
            include_deleted: bool = Query(default=False),
    ):
        current_user = await _resolve_graph_read_user(request, context)
        preset_config = _preset_or_400(preset) if preset else None
        mode_config = _mode_or_400(mode) if mode else None
        base_config = preset_config or mode_config
        resolved_labels = _split_csv(labels) or (base_config["labels"] if base_config else [])
        resolved_relationship_types = _split_csv(relationship_types) or (
            base_config["relationship_types"] if base_config else []
        )
        bounded_depth = _bounded_int(depth, minimum=1, maximum=GRAPH_EXPANSION_MAX_DEPTH)
        bounded_limit = _bounded_int(limit, minimum=1, maximum=GRAPH_EXPANSION_MAX_LIMIT)
        reader, close_after = _resolve_graph_reader(context)
        _attach_reader_access(reader, current_user)
        try:
            document = await reader.get_neighborhood(
                node_id,
                labels=resolved_labels,
                relationship_types=resolved_relationship_types,
                depth=bounded_depth,
                limit=bounded_limit,
                include_deleted=include_deleted,
            )
            return _document_payload(
                document,
                query_meta={
                    "query": "expand",
                    "node_id": node_id,
                    "preset": preset,
                    "mode": mode,
                    "depth": bounded_depth,
                    "relationship_types": resolved_relationship_types,
                    "labels": resolved_labels,
                    "include_deleted": include_deleted,
                },
                limit=bounded_limit,
                max_edges=bounded_limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except Neo4jGraphReadError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        finally:
            await _close_reader_if_needed(reader, close_after)

    @router.get("/nodes/{node_id}/neighbors", summary="Get Grouped Graph Node Neighbors")
    async def get_graph_node_neighbors(
            node_id: str,
            request: Request,
            preset: str | None = Query(default=None, description="Optional domain preset."),
            mode: str | None = Query(default=None, description="Optional graph neighborhood mode."),
            labels: str | None = Query(default=None, description="Comma-separated center-node label allow-list."),
            relationship_types: str | None = Query(default=None,
                                                   description="Comma-separated relationship type allow-list."),
            limit: int = Query(default=50, ge=1, le=GRAPH_EXPANSION_MAX_LIMIT),
            include_deleted: bool = Query(default=False),
    ):
        current_user = await _resolve_graph_read_user(request, context)
        preset_config = _preset_or_400(preset) if preset else None
        mode_config = _mode_or_400(mode) if mode else None
        base_config = preset_config or mode_config
        resolved_labels = _split_csv(labels) or (base_config["labels"] if base_config else [])
        resolved_relationship_types = _split_csv(relationship_types) or (
            base_config["relationship_types"] if base_config else []
        )
        bounded_limit = _bounded_int(limit, minimum=1, maximum=GRAPH_EXPANSION_MAX_LIMIT)
        reader, close_after = _resolve_graph_reader(context)
        _attach_reader_access(reader, current_user)
        try:
            document = await reader.get_neighborhood(
                node_id,
                labels=resolved_labels,
                relationship_types=resolved_relationship_types,
                depth=1,
                limit=bounded_limit,
                include_deleted=include_deleted,
            )
            return _neighbors_payload(
                document,
                center_id=node_id,
                query_meta={
                    "query": "neighbors",
                    "node_id": node_id,
                    "preset": preset,
                    "mode": mode,
                    "depth": 1,
                    "relationship_types": resolved_relationship_types,
                    "labels": resolved_labels,
                    "include_deleted": include_deleted,
                },
                limit=bounded_limit,
                max_edges=bounded_limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except Neo4jGraphReadError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        finally:
            await _close_reader_if_needed(reader, close_after)

    async def _get_preset_neighborhood(
            *,
            preset: str,
            node_id: str,
            request: Request,
            depth: int,
            limit: int,
            include_deleted: bool,
            include_operational_coverage: bool,
            recent_run_limit: int,
            workflow_run_limit: int,
            incident_limit: int,
    ) -> dict:
        current_user = await _resolve_graph_read_user(request, context)
        preset_config = _preset_or_400(preset)
        bounded_depth = _bounded_int(depth, minimum=1, maximum=GRAPH_EXPANSION_MAX_DEPTH)
        bounded_limit = _bounded_int(limit, minimum=1, maximum=GRAPH_EXPANSION_MAX_LIMIT)
        reader = None
        close_after = False
        try:
            try:
                reader, close_after = _resolve_graph_reader(context)
                _attach_reader_access(reader, current_user)
                document = await reader.get_neighborhood(
                    node_id,
                    labels=preset_config["labels"],
                    relationship_types=preset_config["relationship_types"],
                    depth=bounded_depth,
                    limit=bounded_limit,
                    include_deleted=include_deleted,
                )
                document = await enrich_with_operational_coverage(
                    document,
                    execution_store=context.execution_store,
                    root_type=preset,
                    root_id=node_id,
                    include=include_operational_coverage,
                    recent_run_limit=recent_run_limit,
                    workflow_run_limit=workflow_run_limit,
                    incident_limit=incident_limit,
                )
            except HTTPException as exc:
                if not include_operational_coverage or exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
                    raise
                # Operational coverage is backed by the execution store, so it can
                # still keep Agency Graph useful while Neo4j projection reads recover.
                document = await _operational_fallback_document(
                    context=context,
                    root_type=preset,
                    root_id=node_id,
                    recent_run_limit=recent_run_limit,
                    workflow_run_limit=workflow_run_limit,
                    incident_limit=incident_limit,
                    unavailable_reason=str(exc.detail),
                )
            except Neo4jGraphReadError as exc:
                if not include_operational_coverage:
                    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
                document = await _operational_fallback_document(
                    context=context,
                    root_type=preset,
                    root_id=node_id,
                    recent_run_limit=recent_run_limit,
                    workflow_run_limit=workflow_run_limit,
                    incident_limit=incident_limit,
                    unavailable_reason=str(exc),
                )
            return _document_payload(
                document,
                query_meta={
                    "query": "preset_neighborhood",
                    "preset": preset,
                    "node_id": node_id,
                    "depth": bounded_depth,
                    "relationship_types": preset_config["relationship_types"],
                    "labels": preset_config["labels"],
                    "include_deleted": include_deleted,
                    "include_operational_coverage": include_operational_coverage,
                    "recent_run_limit": recent_run_limit,
                    "workflow_run_limit": workflow_run_limit,
                    "incident_limit": incident_limit,
                },
                limit=bounded_limit,
                max_edges=bounded_limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        finally:
            if reader is not None:
                await _close_reader_if_needed(reader, close_after)

    @router.get("/workflows/{workflow_id}/neighborhood", summary="Get Workflow Graph Neighborhood")
    async def get_workflow_neighborhood(
            workflow_id: str,
            request: Request,
            depth: int = Query(default=1, ge=1, le=GRAPH_EXPANSION_MAX_DEPTH),
            limit: int = Query(default=100, ge=1, le=GRAPH_EXPANSION_MAX_LIMIT),
            include_deleted: bool = Query(default=False),
            include_operational_coverage: bool = Query(default=False),
            recent_run_limit: int = Query(default=GRAPH_OPERATIONAL_RECENT_RUN_LIMIT, ge=1, le=200),
            workflow_run_limit: int = Query(default=GRAPH_OPERATIONAL_WORKFLOW_RUN_LIMIT, ge=1, le=100),
            incident_limit: int = Query(default=GRAPH_OPERATIONAL_INCIDENT_LIMIT, ge=1, le=50),
    ):
        return await _get_preset_neighborhood(
            preset="workflow",
            node_id=workflow_id,
            request=request,
            depth=depth,
            limit=limit,
            include_deleted=include_deleted,
            include_operational_coverage=include_operational_coverage,
            recent_run_limit=recent_run_limit,
            workflow_run_limit=workflow_run_limit,
            incident_limit=incident_limit,
        )

    @router.get("/runs/{run_id}/neighborhood", summary="Get Workflow Run Graph Neighborhood")
    async def get_workflow_run_neighborhood(
            run_id: str,
            request: Request,
            depth: int = Query(default=1, ge=1, le=GRAPH_EXPANSION_MAX_DEPTH),
            limit: int = Query(default=100, ge=1, le=GRAPH_EXPANSION_MAX_LIMIT),
            include_deleted: bool = Query(default=False),
            include_operational_coverage: bool = Query(default=False),
            recent_run_limit: int = Query(default=GRAPH_OPERATIONAL_RECENT_RUN_LIMIT, ge=1, le=200),
            workflow_run_limit: int = Query(default=GRAPH_OPERATIONAL_WORKFLOW_RUN_LIMIT, ge=1, le=100),
            incident_limit: int = Query(default=GRAPH_OPERATIONAL_INCIDENT_LIMIT, ge=1, le=50),
    ):
        return await _get_preset_neighborhood(
            preset="workflow_run",
            node_id=run_id,
            request=request,
            depth=depth,
            limit=limit,
            include_deleted=include_deleted,
            include_operational_coverage=include_operational_coverage,
            recent_run_limit=recent_run_limit,
            workflow_run_limit=workflow_run_limit,
            incident_limit=incident_limit,
        )

    @router.get("/agents/{agent_id}/neighborhood", summary="Get Agent Graph Neighborhood")
    async def get_agent_neighborhood(
            agent_id: str,
            request: Request,
            depth: int = Query(default=1, ge=1, le=GRAPH_EXPANSION_MAX_DEPTH),
            limit: int = Query(default=100, ge=1, le=GRAPH_EXPANSION_MAX_LIMIT),
            include_deleted: bool = Query(default=False),
            include_operational_coverage: bool = Query(default=False),
            recent_run_limit: int = Query(default=GRAPH_OPERATIONAL_RECENT_RUN_LIMIT, ge=1, le=200),
            workflow_run_limit: int = Query(default=GRAPH_OPERATIONAL_WORKFLOW_RUN_LIMIT, ge=1, le=100),
            incident_limit: int = Query(default=GRAPH_OPERATIONAL_INCIDENT_LIMIT, ge=1, le=50),
    ):
        return await _get_preset_neighborhood(
            preset="agent",
            node_id=agent_id,
            request=request,
            depth=depth,
            limit=limit,
            include_deleted=include_deleted,
            include_operational_coverage=include_operational_coverage,
            recent_run_limit=recent_run_limit,
            workflow_run_limit=workflow_run_limit,
            incident_limit=incident_limit,
        )

    @router.get("/tools/{tool_id}/neighborhood", summary="Get Tool Graph Neighborhood")
    async def get_tool_neighborhood(
            tool_id: str,
            request: Request,
            depth: int = Query(default=1, ge=1, le=GRAPH_EXPANSION_MAX_DEPTH),
            limit: int = Query(default=100, ge=1, le=GRAPH_EXPANSION_MAX_LIMIT),
            include_deleted: bool = Query(default=False),
            include_operational_coverage: bool = Query(default=False),
            recent_run_limit: int = Query(default=GRAPH_OPERATIONAL_RECENT_RUN_LIMIT, ge=1, le=200),
            workflow_run_limit: int = Query(default=GRAPH_OPERATIONAL_WORKFLOW_RUN_LIMIT, ge=1, le=100),
            incident_limit: int = Query(default=GRAPH_OPERATIONAL_INCIDENT_LIMIT, ge=1, le=50),
    ):
        return await _get_preset_neighborhood(
            preset="tool",
            node_id=tool_id,
            request=request,
            depth=depth,
            limit=limit,
            include_deleted=include_deleted,
            include_operational_coverage=include_operational_coverage,
            recent_run_limit=recent_run_limit,
            workflow_run_limit=workflow_run_limit,
            incident_limit=incident_limit,
        )

    @router.get("/memories/{memory_id}/neighborhood", summary="Get Agency Graph Memory Neighborhood")
    async def get_memory_neighborhood(
            memory_id: str,
            request: Request,
            depth: int = Query(default=1, ge=1, le=GRAPH_EXPANSION_MAX_DEPTH),
            limit: int = Query(default=100, ge=1, le=GRAPH_EXPANSION_MAX_LIMIT),
            include_deleted: bool = Query(default=False),
            include_operational_coverage: bool = Query(default=False),
            recent_run_limit: int = Query(default=GRAPH_OPERATIONAL_RECENT_RUN_LIMIT, ge=1, le=200),
            workflow_run_limit: int = Query(default=GRAPH_OPERATIONAL_WORKFLOW_RUN_LIMIT, ge=1, le=100),
            incident_limit: int = Query(default=GRAPH_OPERATIONAL_INCIDENT_LIMIT, ge=1, le=50),
    ):
        return await _get_preset_neighborhood(
            preset="memory",
            node_id=memory_id,
            request=request,
            depth=depth,
            limit=limit,
            include_deleted=include_deleted,
            include_operational_coverage=include_operational_coverage,
            recent_run_limit=recent_run_limit,
            workflow_run_limit=workflow_run_limit,
            incident_limit=incident_limit,
        )

    @router.get("/entities/{entity_id}/neighborhood", summary="Get Entity Graph Neighborhood")
    async def get_entity_neighborhood(
            entity_id: str,
            request: Request,
            depth: int = Query(default=1, ge=1, le=GRAPH_EXPANSION_MAX_DEPTH),
            limit: int = Query(default=100, ge=1, le=GRAPH_EXPANSION_MAX_LIMIT),
            include_deleted: bool = Query(default=False),
            include_operational_coverage: bool = Query(default=False),
            recent_run_limit: int = Query(default=GRAPH_OPERATIONAL_RECENT_RUN_LIMIT, ge=1, le=200),
            workflow_run_limit: int = Query(default=GRAPH_OPERATIONAL_WORKFLOW_RUN_LIMIT, ge=1, le=100),
            incident_limit: int = Query(default=GRAPH_OPERATIONAL_INCIDENT_LIMIT, ge=1, le=50),
    ):
        return await _get_preset_neighborhood(
            preset="entity",
            node_id=entity_id,
            request=request,
            depth=depth,
            limit=limit,
            include_deleted=include_deleted,
            include_operational_coverage=include_operational_coverage,
            recent_run_limit=recent_run_limit,
            workflow_run_limit=workflow_run_limit,
            incident_limit=incident_limit,
        )

    @router.get("/tasks/{task_id}/neighborhood", summary="Get Task Graph Neighborhood")
    async def get_task_neighborhood(
            task_id: str,
            request: Request,
            depth: int = Query(default=1, ge=1, le=GRAPH_EXPANSION_MAX_DEPTH),
            limit: int = Query(default=100, ge=1, le=GRAPH_EXPANSION_MAX_LIMIT),
            include_deleted: bool = Query(default=False),
            include_operational_coverage: bool = Query(default=False),
            recent_run_limit: int = Query(default=GRAPH_OPERATIONAL_RECENT_RUN_LIMIT, ge=1, le=200),
            workflow_run_limit: int = Query(default=GRAPH_OPERATIONAL_WORKFLOW_RUN_LIMIT, ge=1, le=100),
            incident_limit: int = Query(default=GRAPH_OPERATIONAL_INCIDENT_LIMIT, ge=1, le=50),
    ):
        return await _get_preset_neighborhood(
            preset="task",
            node_id=task_id,
            request=request,
            depth=depth,
            limit=limit,
            include_deleted=include_deleted,
            include_operational_coverage=include_operational_coverage,
            recent_run_limit=recent_run_limit,
            workflow_run_limit=workflow_run_limit,
            incident_limit=incident_limit,
        )

    @router.get("/search", summary="Search Graph Nodes")
    async def search_graph_nodes(
            request: Request,
            q: str | None = Query(default=None, min_length=1),
            labels: str | None = Query(default=None, description="Comma-separated label allow-list."),
            node_types: str | None = Query(default=None, description="Comma-separated canonical node type allow-list."),
            workflow_id: str | None = Query(default=None, description="Limit results to a workflow scope."),
            agent_id: str | None = Query(default=None, description="Limit results to an agent scope."),
            tool_id: str | None = Query(default=None, description="Limit results to a tool scope."),
            document_id: str | None = Query(default=None, description="Limit results to a document scope."),
            entity_id: str | None = Query(default=None, description="Limit results to an entity scope."),
            error_text: str | None = Query(default=None, min_length=1, description="Search error/status text."),
            limit: int = Query(default=50, ge=1, le=1000),
    ):
        reader, close_after = await _resolve_authorized_graph_reader(request, context)
        try:
            return _document_payload(
                await reader.search_nodes(
                    q,
                    labels=_split_csv(labels),
                    node_types=_split_csv(node_types),
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    tool_id=tool_id,
                    document_id=document_id,
                    entity_id=entity_id,
                    error_text=error_text,
                    limit=limit,
                ),
                limit=limit,
                max_edges=0,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except Neo4jGraphReadError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        finally:
            await _close_reader_if_needed(reader, close_after)

    async def _run_path_query(request: Request, method_name: str, call_kwargs: dict, *, limit: int) -> dict:
        reader, close_after = await _resolve_authorized_graph_reader(request, context)
        try:
            method = getattr(reader, method_name)
            document = await method(**call_kwargs)
            return _document_payload(document, limit=limit, max_edges=limit)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except Neo4jGraphReadError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        finally:
            await _close_reader_if_needed(reader, close_after)

    @router.get("/paths/shortest", summary="Get Shortest Graph Path")
    async def get_shortest_graph_path(
            request: Request,
            source_id: str = Query(min_length=1),
            target_id: str = Query(min_length=1),
            relationship_types: str | None = Query(default=None,
                                                   description="Comma-separated relationship type allow-list."),
            max_depth: int = Query(default=4, ge=1, le=GRAPH_PATH_MAX_DEPTH),
            limit: int = Query(default=1, ge=1, le=GRAPH_PATH_MAX_LIMIT),
    ):
        bounded_depth = _bounded_int(max_depth, minimum=1, maximum=GRAPH_PATH_MAX_DEPTH)
        bounded_limit = _bounded_int(limit, minimum=1, maximum=GRAPH_PATH_MAX_LIMIT)
        return await _run_path_query(
            request,
            "get_shortest_path",
            {
                "source_id": source_id,
                "target_id": target_id,
                "relationship_types": _split_csv(relationship_types),
                "max_depth": bounded_depth,
                "limit": bounded_limit,
            },
            limit=bounded_limit,
        )

    @router.get("/paths/memory-source-run", summary="Get Memory To Source Run Path")
    async def get_memory_source_run_graph_path(
            request: Request,
            memory_id: str = Query(min_length=1),
            run_id: str | None = Query(default=None),
            max_depth: int = Query(default=4, ge=1, le=GRAPH_PATH_MAX_DEPTH),
            limit: int = Query(default=25, ge=1, le=GRAPH_PATH_MAX_LIMIT),
    ):
        bounded_depth = _bounded_int(max_depth, minimum=1, maximum=GRAPH_PATH_MAX_DEPTH)
        bounded_limit = _bounded_int(limit, minimum=1, maximum=GRAPH_PATH_MAX_LIMIT)
        return await _run_path_query(
            request,
            "get_memory_source_run_path",
            {"memory_id": memory_id, "run_id": run_id, "max_depth": bounded_depth, "limit": bounded_limit},
            limit=bounded_limit,
        )

    @router.get("/paths/failed-run-root-cause", summary="Get Failed Run Root Cause Paths")
    async def get_failed_run_root_cause_graph_path(
            request: Request,
            run_id: str = Query(min_length=1),
            max_depth: int = Query(default=3, ge=1, le=GRAPH_PATH_MAX_DEPTH),
            limit: int = Query(default=25, ge=1, le=GRAPH_PATH_MAX_LIMIT),
    ):
        bounded_depth = _bounded_int(max_depth, minimum=1, maximum=GRAPH_PATH_MAX_DEPTH)
        bounded_limit = _bounded_int(limit, minimum=1, maximum=GRAPH_PATH_MAX_LIMIT)
        return await _run_path_query(
            request,
            "get_failed_run_root_cause_path",
            {"run_id": run_id, "max_depth": bounded_depth, "limit": bounded_limit},
            limit=bounded_limit,
        )

    @router.get("/paths/influence", summary="Get Document Or Entity To Workflow Influence Paths")
    async def get_influence_graph_path(
            request: Request,
            anchor_type: str = Query(description="'document' or 'entity'."),
            anchor_id: str = Query(min_length=1),
            workflow_id: str | None = Query(default=None),
            max_depth: int = Query(default=4, ge=1, le=GRAPH_PATH_MAX_DEPTH),
            limit: int = Query(default=25, ge=1, le=GRAPH_PATH_MAX_LIMIT),
    ):
        bounded_depth = _bounded_int(max_depth, minimum=1, maximum=GRAPH_PATH_MAX_DEPTH)
        bounded_limit = _bounded_int(limit, minimum=1, maximum=GRAPH_PATH_MAX_LIMIT)
        return await _run_path_query(
            request,
            "get_influence_path",
            {
                "anchor_type": anchor_type,
                "anchor_id": anchor_id,
                "workflow_id": workflow_id,
                "max_depth": bounded_depth,
                "limit": bounded_limit,
            },
            limit=bounded_limit,
        )

    @router.get("/paths/agent-prior-runs", summary="Get Agent To Prior Run Paths")
    async def get_agent_prior_runs_graph_path(
            request: Request,
            agent_id: str = Query(min_length=1),
            run_id: str | None = Query(default=None),
            max_depth: int = Query(default=3, ge=1, le=GRAPH_PATH_MAX_DEPTH),
            limit: int = Query(default=25, ge=1, le=GRAPH_PATH_MAX_LIMIT),
    ):
        bounded_depth = _bounded_int(max_depth, minimum=1, maximum=GRAPH_PATH_MAX_DEPTH)
        bounded_limit = _bounded_int(limit, minimum=1, maximum=GRAPH_PATH_MAX_LIMIT)
        return await _run_path_query(
            request,
            "get_agent_prior_runs_path",
            {"agent_id": agent_id, "run_id": run_id, "max_depth": bounded_depth, "limit": bounded_limit},
            limit=bounded_limit,
        )

    @router.get("/presets/{preset}", summary="Run Graph Query Preset")
    async def get_graph_query_preset(
            preset: str,
            request: Request,
            workflow_id: str | None = Query(default=None),
            run_id: str | None = Query(default=None),
            memory_id: str | None = Query(default=None),
            agent_id: str | None = Query(default=None),
            tool_id: str | None = Query(default=None),
            persona_id: str | None = Query(default=None),
            device_id: str | None = Query(default=None),
            room: str | None = Query(default=None),
            limit: int = Query(default=50, ge=1, le=GRAPH_PATH_MAX_LIMIT),
    ):
        preset_key = _query_preset_or_400(preset)
        bounded_limit = _bounded_int(limit, minimum=1, maximum=GRAPH_PATH_MAX_LIMIT)
        reader, close_after = await _resolve_authorized_graph_reader(request, context)
        try:
            document = await reader.get_graph_preset(
                preset_key,
                workflow_id=workflow_id,
                run_id=run_id,
                memory_id=memory_id,
                agent_id=agent_id,
                tool_id=tool_id,
                persona_id=persona_id,
                device_id=device_id,
                room=room,
                limit=bounded_limit,
            )
            return _document_payload(document, query_meta={"preset": preset_key}, limit=bounded_limit,
                                     max_edges=bounded_limit)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except Neo4jGraphReadError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        finally:
            await _close_reader_if_needed(reader, close_after)

    @router.get("/workflows/{workflow_id}/lineage", summary="Get Workflow Graph Lineage")
    async def get_workflow_graph_lineage(
            workflow_id: str,
            request: Request,
            limit: int = Query(default=300, ge=1, le=1000),
    ):
        reader, close_after = await _resolve_authorized_graph_reader(request, context)
        try:
            return _document_payload(await reader.get_workflow_lineage(workflow_id, limit=limit), limit=limit,
                                     max_edges=limit)
        except Neo4jGraphReadError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        finally:
            await _close_reader_if_needed(reader, close_after)

    return router


__all__ = ["create_graph_read_router"]
