from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user_if_present
from app.tools.contracts.validator import ToolContractValidationError
from app.tools.runtime import ToolRuntimeExecutor


def create_tool_runtime_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    executor = ToolRuntimeExecutor(context=context)
    router = APIRouter(prefix="/tools", tags=["Tool Runtime"])

    @router.post("/{tool_name}/run", summary="Run Tool")
    async def run_tool(tool_name: str, payload: dict[str, Any], request: Request):
        current_user = await resolve_current_user_if_present(request, context, required_scopes=["tools:write"])
        actor = getattr(current_user, "id", None) if current_user is not None else None
        try:
            response = await executor.run_async(tool_name, payload, actor=actor)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ToolContractValidationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return response.model_dump(mode="json")

    return router
