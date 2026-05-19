from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from typing import Any, Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user_if_present
from app.domain import ToolDefinition
from app.runtime.native.errors import ToolExecutionError
from ._crud import build_crud_router


class ToolTestRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class ToolValidateRequest(BaseModel):
    tool_definition: ToolDefinition = Field(alias="toolDefinition")


def create_tools_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter()

    router.include_router(
        build_crud_router(
            prefix="/tools",
            tag="Tools",
            summary_name="Tool",
            repo=context.tool_repo,
            model_cls=ToolDefinition,
            context=context,
            read_scopes=["tools:read"],
            write_scopes=["tools:write"],
            before_list=context.ensure_builtin_tool_seed_data,
        )
    )

    @router.post("/tools/validate", tags=["Tools"], summary="Validate Tool")
    async def validate_tool(payload: ToolValidateRequest, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["tools:write"])
        result = context.tool_service.validate_definition(payload.tool_definition)
        return {
            "tool": payload.tool_definition.model_dump(mode="json"),
            "valid": result.valid,
            "validation_errors": result.validation_errors,
            "validation_warnings": result.validation_warnings,
        }

    @router.post("/tools/{tool_id}/test", tags=["Tools"], summary="Test Tool")
    async def test_tool(tool_id: str, payload: ToolTestRequest, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["tools:write"])
        tool = await context.tool_repo.get(tool_id)
        if tool is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Tool '{tool_id}' not found")

        validation = context.tool_service.validate_definition(tool)
        if not validation.valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "validation_errors": validation.validation_errors,
                    "validation_warnings": validation.validation_warnings,
                },
            )

        try:
            return await context.tool_service.test_tool(tool, payload.input)
        except ToolExecutionError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return router
