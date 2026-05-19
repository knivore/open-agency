from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user_if_present
from app.tools.contracts import get_default_contract_registry


def create_tool_contracts_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    registry = get_default_contract_registry()
    router = APIRouter(prefix="/tools/contracts", tags=["Tool Contracts"])

    @router.get("", summary="List Tool Contracts")
    async def list_tool_contracts(request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["tools:read"])
        return {"items": [contract.model_dump(mode="json", by_alias=True) for contract in registry.list_contracts()]}

    @router.get("/{tool_name}", summary="Get Tool Contract")
    async def get_tool_contract(tool_name: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["tools:read"])
        contract = registry.get_contract(tool_name)
        if contract is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Tool contract '{tool_name}' not found")
        return contract.model_dump(mode="json", by_alias=True)

    return router
