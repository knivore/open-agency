from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.domain import RuntimeAdapterDefinition
from app.services import ModelCatalogService


def create_runtime_adapters_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = ModelCatalogService(context)
    router = APIRouter(prefix="/runtime-adapters", tags=["Runtime Adapters"])

    @router.post("", summary="Create Runtime Adapter")
    async def create_runtime_adapter(payload: RuntimeAdapterDefinition):
        await service.ensure_runtime_adapters_seeded()
        created = await context.runtime_adapter_repo.create(payload)
        return created.model_dump(mode="json")

    @router.get("", summary="List Runtime Adapters")
    async def list_runtime_adapters():
        await service.ensure_runtime_adapters_seeded()
        items = await context.runtime_adapter_repo.list()
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.get("/{item_id}", summary="Get Runtime Adapter By Id")
    async def get_runtime_adapter(item_id: str):
        await service.ensure_runtime_adapters_seeded()
        item = await context.runtime_adapter_repo.get(item_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Runtime Adapter '{item_id}' not found")
        return item.model_dump(mode="json")

    @router.put("/{item_id}", summary="Update Runtime Adapter")
    async def update_runtime_adapter(item_id: str, patch: dict[str, Any]):
        await service.ensure_runtime_adapters_seeded()
        if item_id in {"native", "crewai"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Runtime Adapter '{item_id}' is protected")
        item = await context.runtime_adapter_repo.update(item_id, patch)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Runtime Adapter '{item_id}' not found")
        return item.model_dump(mode="json")

    @router.delete("/{item_id}", summary="Soft Delete Runtime Adapter")
    async def delete_runtime_adapter(item_id: str):
        await service.ensure_runtime_adapters_seeded()
        if item_id in {"native", "crewai"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Runtime Adapter '{item_id}' is protected")
        deleted = await context.runtime_adapter_repo.soft_delete(item_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Runtime Adapter '{item_id}' not found")
        return {"deleted": True, "id": item_id}

    return router
