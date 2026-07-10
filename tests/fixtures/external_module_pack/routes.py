from __future__ import annotations

from fastapi import APIRouter


def create_router(context=None) -> APIRouter:
    router = APIRouter(prefix="/api/external-example", tags=["external-example"])

    @router.get("/status")
    async def status() -> dict[str, object]:
        return {"module": "external_example_pack", "available": True}

    return router

